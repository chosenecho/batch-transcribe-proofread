"""Exercise the new offline boundary with real media and the retained database."""
import json
from pathlib import Path
import sqlite3
import wave

import pytest

from bili_asr.cli import main
from bili_asr.cli.local_input import import_manifest
from bili_asr.services.archive_migration import initialize_archive


@pytest.fixture
def inputs(tmp_path):
    root = tmp_path / "archive"
    initialize_archive(root)
    media = tmp_path / "input.wav"
    with wave.open(str(media), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 16000)
    manifest = tmp_path / "inputs.json"
    items = [{"platform": "youtube", "external_id": value, "title": f"Input {value}",
              "audio": "input.wav"} for value in ("abcdefghijk", "Abcdefghijk")]
    manifest.write_text(json.dumps({"version": 1, "items": items}), encoding="utf-8")
    return root, media, manifest, items


def test_import_registers_verified_media_and_is_idempotent(inputs):
    root, media, manifest, items = inputs
    first = import_manifest(manifest, root)
    assert len(set(first["part_ids"])) == 2
    assert import_manifest(manifest, root) == first
    with sqlite3.connect(root / "archive.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM audio_objects").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM part_audio_objects").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM workflow_jobs").fetchone()[0] == 0
        key = connection.execute("SELECT storage_key FROM audio_objects").fetchone()[0]
    assert (root / key).read_bytes() == media.read_bytes()


def test_import_plans_asr_without_collection_jobs(inputs, capsys):
    root, _, manifest, _ = inputs
    report = import_manifest(manifest, root)
    command = ["workflow", "plan", "--archive-root", str(root), "--model", "offline",
               "--device", "cpu", "--proofread"]
    for part_id in report["part_ids"]:
        command.extend(["--part-id", str(part_id)])
    assert main(command) == 0, capsys.readouterr().err
    assert main(command) == 0
    with sqlite3.connect(root / "archive.db") as connection:
        assert connection.execute("SELECT kind,COUNT(*) FROM workflow_jobs GROUP BY kind ORDER BY kind").fetchall() == [
            ("asr", 2), ("proofread", 2), ("render_document", 2)]
        # ASR has no prerequisite collection jobs; editorial still follows ASR.
        assert connection.execute("SELECT COUNT(*) FROM workflow_job_dependencies").fetchone()[0] == 4


def test_bad_item_does_not_partially_register_batch(inputs):
    root, _, manifest, items = inputs
    items[1]["sha256"] = "0" * 64
    manifest.write_text(json.dumps({"version": 1, "items": items}), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        import_manifest(manifest, root)
    with sqlite3.connect(root / "archive.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM video_parts").fetchone()[0] == 0


def test_changed_input_does_not_replace_frozen_audio(inputs):
    root, media, manifest, _ = inputs
    import_manifest(manifest, root)
    original = next((root / "audio").glob("*.wav")).read_bytes()
    with wave.open(str(media), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\1\0" * 16000)
    with pytest.raises(ValueError, match="different audio"):
        import_manifest(manifest, root)
    assert next((root / "audio").glob("*.wav")).read_bytes() == original


def test_processing_runtime_has_no_collection_handlers(inputs):
    from bili_asr.archive_modes import ArchiveAccessMode
    from bili_asr.archive_session import ArchiveSession
    from bili_asr.workflow_runtime import ArchiveWorkflowHandlers
    from bili_asr.storage.workflow import WorkflowRepository
    root, _, _, _ = inputs
    with ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session:
        handlers = ArchiveWorkflowHandlers(session.connection, WorkflowRepository(session.connection),
                                           archive_root=root, sessdata=None)
        assert {kind.value for kind in handlers.handlers()} == {"asr", "publish"}
        handlers.close()


def test_import_asr_proofread_render_and_transcript_bundle(inputs):
    from bili_asr.archive_modes import ArchiveAccessMode
    from bili_asr.archive_session import ArchiveSession
    from bili_asr.editorial import EditorialConfig
    from bili_asr.editorial_runtime import EditorialWorkflowHandlers
    from bili_asr.storage.editorial import EditorialRepository
    from bili_asr.storage.workflow import WorkflowRepository
    from bili_asr.workflow import WorkflowExecutor
    from bili_asr.workflow_models import AsrPolicy, AsrProfile, JobKind
    from bili_asr.workflow_runtime import ArchiveWorkflowHandlers
    from tests.test_ai_editorial import FakeClient
    from tests.support.asr_quality import coverage, cue

    root, _, manifest, _ = inputs
    report = import_manifest(manifest, root)
    with ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session:
        workflow = WorkflowRepository(session.connection)
        workflow.plan_with_profile(part_ids=report["part_ids"], policy=AsrPolicy.ALL,
            profile=AsrProfile("offline-e2e", "offline-model", device="cuda"),
            editorial_config=EditorialConfig().to_dict())

        def inference(config, path, *, diagnostics_sink, **kwargs):
            diagnostics_sink.update({"quality": {"status": "no-known-risk", "flags": []}})
            return [cue(0, 1, "本地批量转录测试内容")], {"language": "Chinese", "model": config.model_name}, coverage(1, 1)

        archive = ArchiveWorkflowHandlers(session.connection, workflow, archive_root=root,
            sessdata=None, timeout_transcriber=inference)
        editorial = EditorialWorkflowHandlers(EditorialRepository(session.connection), workflow,
            archive_root=root, client=FakeClient())
        try:
            asr = WorkflowExecutor(workflow, worker_id="e2e-asr", handlers=archive.handlers(), kinds=(JobKind.ASR,)).run()
            assert (asr.succeeded, asr.failed) == (2, 0)
            ai = WorkflowExecutor(workflow, worker_id="e2e-ai", handlers=editorial.handlers(),
                                  kinds=(JobKind.PROOFREAD, JobKind.RENDER_DOCUMENT)).run()
            assert (ai.succeeded, ai.failed) == (4, 0)
            bundle = WorkflowExecutor(workflow, worker_id="e2e-publish", handlers=archive.handlers(),
                                      kinds=(JobKind.PUBLISH,)).run()
            assert (bundle.succeeded, bundle.failed) == (2, 0)
            assert session.connection.execute("SELECT COUNT(*) FROM editorial_revisions").fetchone()[0] == 2
            assert session.connection.execute("SELECT COUNT(*) FROM workflow_jobs WHERE status<>'succeeded'").fetchone()[0] == 0
            assert len(list(root.rglob("*.srt"))) >= 2
            assert len(list(root.rglob("*.vtt"))) >= 2
            assert len(list(root.rglob("*.md"))) >= 4
        finally:
            archive.close()
            editorial.close()


@pytest.mark.parametrize("kind", ["audio", "subtitle"])
def test_recovery_does_not_offer_removed_collectors(kind):
    from bili_asr.services.archive_recovery import RuntimeProbe, _requirements
    probe = RuntimeProbe(frozenset(), frozenset(), frozenset(), frozenset())
    assert _requirements(kind, "bilibili", probe) == ["handler:collection-removed"]
    assert _requirements(kind, "youtube", probe) == ["handler:collection-removed"]


@pytest.mark.parametrize("command", ["fetch-meta", "fetch-tags", "source", "user-content"])
def test_collection_commands_are_absent(command):
    with pytest.raises(SystemExit):
        main([command, "--help"])
