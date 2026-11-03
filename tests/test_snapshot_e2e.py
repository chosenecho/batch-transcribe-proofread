"""Offline CLI evidence for carrying an archive to another device."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from bili_asr import archive
from bili_asr.artifacts import BUNDLE_SCHEMA_V3, REQUIRED_ARTIFACT_KEYS, SOURCE_METADATA_KEY
from bili_asr.cli.main import main
from bili_asr.storage import (
    AsrPolicy,
    AsrProfile,
    JobKind,
    WorkflowRepository,
    open_database,
)

AUDIO_KEY = "audio/BVsnapshot.p0.m4a"
AUDIO_BYTES = b"portable audio content for an offline transcription fixture"
TRANSCRIPT_TEXT = "The restored worker uses the downloaded audio."
PORTABLE_PROFILE = AsrProfile(
    profile_key="portable",
    model_name="offline-model",
    model_revision="portable-model-revision",
    aligner_name="offline-aligner",
    aligner_revision="portable-aligner-revision",
    device="cpu",
    language="en",
    chunk_seconds=60.0,
    inference_timeout_seconds=120.0,
    hotwords=("portable archive",),
    offline=True,
    tokens_per_second=6.0,
    min_new_tokens=128,
    second_pass_use_cache=True,
)


def _seed_archive(root: Path, *, artifact_root: Path | None = None) -> dict[str, str]:
    media_root = root if artifact_root is None else artifact_root
    audio_file = media_root / AUDIO_KEY
    audio_file.parent.mkdir(parents=True)
    audio_file.write_bytes(AUDIO_BYTES)
    digest = hashlib.sha256(AUDIO_BYTES).hexdigest()
    connection = open_database(root)
    try:
        with connection:
            connection.execute(
                "INSERT INTO bilibili_users VALUES (1, 'offline creator', 1, 1)"
            )
            connection.execute(
                "INSERT INTO videos VALUES ('BVsnapshot', 1, 1, 'portable archive', 1, 1, 1)"
            )
            connection.execute(
                """INSERT INTO video_parts VALUES
                   (1, 'BVsnapshot', 0, 1, 'part one', 1000, 'metadata_collected', 1, 1)"""
            )
            connection.execute(
                "INSERT INTO video_tags VALUES ('BVsnapshot', 10, 'offline', 'old_channel')"
            )
            connection.execute(
                "INSERT INTO ingestion_cursors VALUES (1, 7, 180, 'limited', NULL, 3)"
            )
            connection.execute(
                "INSERT INTO audio_objects VALUES (1, ?, ?, 'm4a', 1000, ?, 2)",
                (digest, len(AUDIO_BYTES), AUDIO_KEY),
            )
            connection.execute(
                "INSERT INTO part_audio_objects VALUES (1, 1, 2, 'workflow')"
            )
        workflow = WorkflowRepository(connection)
        profile_id = workflow.register_profile(PORTABLE_PROFILE)
        workflow.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=profile_id)
        ids = {}
        for kind in (JobKind.SUBTITLE, JobKind.AUDIO):
            job = workflow.claim("original-device", kinds=(kind,))
            assert job is not None
            result = {"outcome": "absent"} if kind is JobKind.SUBTITLE else {
                "storage_key": AUDIO_KEY,
                "sha256": digest,
                "duration_ms": 1000,
            }
            workflow.finish(job.job_id, worker_id="original-device", result=result)
            ids[kind.value] = job.job_id
        ids["asr"] = connection.execute(
            "SELECT job_id FROM workflow_jobs WHERE kind = 'asr'"
        ).fetchone()["job_id"]
        return ids
    finally:
        connection.close()


def _snapshot_command(capsys, *argv: str) -> dict:
    assert main(["snapshot", *argv]) == 0
    captured = capsys.readouterr()
    assert not captured.err
    report = json.loads(captured.out)
    assert isinstance(report, dict)
    return report


def _reject_command(capsys, *argv: str) -> str:
    assert main(["snapshot", *argv]) == 1
    captured = capsys.readouterr()
    assert captured.out or captured.err
    return captured.out + captured.err


def _resume_offline(root: Path, monkeypatch, capsys) -> list[Path]:
    from bili_asr import asr, audio
    from bili_asr.workflow_runtime import ArchiveWorkflowHandlers

    transcribed_paths = []
    original_path = root.resolve() / AUDIO_KEY

    def forbid_completed_job(*args, **kwargs):
        pytest.fail("the restored workflow repeated a completed acquisition")

    def transcribe(runner, audio_path, *, paired_subtitle_text):
        path = Path(audio_path)
        assert path != original_path and path.suffix == original_path.suffix
        assert path.read_bytes() == AUDIO_BYTES
        assert original_path.read_bytes() == AUDIO_BYTES
        assert paired_subtitle_text is None
        # ASR receives a verified private copy; the downloaded artifact remains
        # the evidence represented by the portable snapshot.
        transcribed_paths.append(original_path)
        return [{"start": 0.0, "end": 1.0, "text": TRANSCRIPT_TEXT}]

    def restored_runner(handlers, profile_id):
        profile = handlers.repository.profile(profile_id)
        assert profile == PORTABLE_PROFILE
        assert profile.asr_config() == PORTABLE_PROFILE.asr_config()
        return SimpleNamespace(provenance=lambda: {"language": "en"}, transcribed_coverage=lambda: {
            "decoded_s": 1.0, "produced_s": 1.0, "coverage": 1.0,
            "coverage_min": .9, "coverage_short": False})

    monkeypatch.setattr(audio, "download_audio", forbid_completed_job)
    monkeypatch.setattr(ArchiveWorkflowHandlers, "subtitle", forbid_completed_job)
    monkeypatch.setattr(ArchiveWorkflowHandlers, "_runner", restored_runner)
    monkeypatch.setattr(asr, "two_pass_transcribe", transcribe)
    assert main(["workflow", "run", "--archive-root", str(root), "--worker-id", "new-device"]) == 0
    captured = capsys.readouterr()
    assert "succeeded=2 failed=0" in captured.out
    assert not captured.err
    return transcribed_paths


def test_snapshot_cli_moves_metadata_audio_and_progress_then_resumes_offline(
    tmp_path, monkeypatch, capsys,
) -> None:
    source = tmp_path / "original-device" / "archive"
    ids = _seed_archive(source)
    source_database = (source / "archive.db").read_bytes()
    snapshot = tmp_path / "transfer" / "archive.zip"
    snapshot.parent.mkdir()
    _snapshot_command(capsys, "save", "--archive-root", str(source), "--out", str(snapshot))
    assert (source / "archive.db").read_bytes() == source_database

    copied = tmp_path / "new-device" / "received.zip"
    copied.parent.mkdir()
    shutil.copy2(snapshot, copied)
    detached = source.with_name("disconnected-original")
    source.rename(detached)
    assert not source.exists()
    _snapshot_command(capsys, "check", "--file", str(copied))
    target = tmp_path / "new-device" / "restored-archive"
    _snapshot_command(capsys, "restore", "--file", str(copied), "--archive-root", str(target))

    connection = open_database(target)
    try:
        cursor = connection.execute("SELECT * FROM ingestion_cursors WHERE mid = 1").fetchone()
        assert (cursor["next_page"], cursor["observed_total"], cursor["state"]) == (7, 180, "limited")
        assert connection.execute("SELECT title FROM videos").fetchone()[0] == "portable archive"
        assert connection.execute("SELECT tag_name FROM video_tags").fetchone()[0] == "offline"
        assert connection.execute("SELECT storage_key FROM audio_objects").fetchone()[0] == AUDIO_KEY
        assert connection.execute("SELECT COUNT(*) FROM part_audio_objects").fetchone()[0] == 1
    finally:
        connection.close()

    assert _resume_offline(target, monkeypatch, capsys) == [target / AUDIO_KEY]
    connection = open_database(target)
    try:
        jobs = {row["job_id"]: row for row in connection.execute("SELECT * FROM workflow_jobs")}
        for job_id in ids.values():
            assert jobs[job_id]["status"] == "succeeded"
            assert jobs[job_id]["attempt_count"] == 1
        assert connection.execute("SELECT COUNT(*) FROM transcripts WHERE source_kind = 'asr-local'").fetchone()[0] == 1
        assert connection.execute("SELECT text FROM transcript_segments").fetchone()[0] == TRANSCRIPT_TEXT
        publication = connection.execute("SELECT artifact_json FROM workflow_publications").fetchone()
        assert publication is not None
        artifacts = json.loads(publication["artifact_json"])
        assert set(artifacts) == set(REQUIRED_ARTIFACT_KEYS)
        assert archive.archive_bundle_complete(target, artifacts, require_readable=True)
        bundle_bytes = {relative: (target / relative).read_bytes() for relative in artifacts.values()}
        marker = archive.bundle_marker_path(artifacts["srt_path"])
        bundle_bytes[marker.as_posix()] = (target / marker).read_bytes()
        marker_document = json.loads(bundle_bytes[marker.as_posix()])
        assert marker_document["schema"] == BUNDLE_SCHEMA_V3
        assert set(marker_document["artifacts"]) == set(REQUIRED_ARTIFACT_KEYS) | {SOURCE_METADATA_KEY}
        metadata_artifact = marker_document["artifacts"][SOURCE_METADATA_KEY]
        metadata_bytes = (target / metadata_artifact["path"]).read_bytes()
        assert hashlib.sha256(metadata_bytes).hexdigest() == metadata_artifact["sha256"]
        assert json.loads(metadata_bytes)["title"] == "portable archive"
        bundle_bytes[metadata_artifact["path"]] = metadata_bytes
        assert (target / artifacts["vtt_path"]).read_text(encoding="utf-8").startswith("WEBVTT\n")
        assert (target / artifacts["txt_path"]).read_text(encoding="utf-8") == TRANSCRIPT_TEXT + "\n"
        completed_jobs = [dict(row) for row in connection.execute("SELECT * FROM workflow_jobs ORDER BY job_id")]
        completed_attempts = [dict(row) for row in connection.execute("SELECT * FROM workflow_attempts ORDER BY attempt_id")]
        assert len(completed_jobs) == 4 and all(job["status"] == "succeeded" for job in completed_jobs)
    finally:
        connection.close()
    assert (detached / "archive.db").read_bytes() == source_database
    assert (detached / AUDIO_KEY).read_bytes() == AUDIO_BYTES

    completed_snapshot = tmp_path / "completed.zip"
    _snapshot_command(capsys, "save", "--archive-root", str(target), "--out", str(completed_snapshot))
    _snapshot_command(capsys, "check", "--file", str(completed_snapshot))
    completed_target = tmp_path / "third-device" / "archive"
    _snapshot_command(
        capsys, "restore", "--file", str(completed_snapshot), "--archive-root", str(completed_target),
    )
    assert archive.archive_bundle_complete(completed_target, artifacts, require_readable=True)
    assert {relative: (completed_target / relative).read_bytes() for relative in bundle_bytes} == bundle_bytes
    assert main(["workflow", "run", "--archive-root", str(completed_target)]) == 0
    assert "succeeded=0 failed=0 cancelled=0 idle=1" in capsys.readouterr().out
    connection = open_database(completed_target)
    try:
        assert connection.execute("SELECT text FROM transcript_segments").fetchone()[0] == TRANSCRIPT_TEXT
        assert connection.execute("SELECT COUNT(*) FROM workflow_attempts").fetchone()[0] == 4
        assert [dict(row) for row in connection.execute("SELECT * FROM workflow_jobs ORDER BY job_id")] == completed_jobs
        assert [dict(row) for row in connection.execute("SELECT * FROM workflow_attempts ORDER BY attempt_id")] == completed_attempts
    finally:
        connection.close()


def test_snapshot_restore_requeues_expired_attempt_without_changing_source(
    tmp_path, monkeypatch, capsys,
) -> None:
    source = tmp_path / "source"
    ids = _seed_archive(source)
    connection = open_database(source)
    try:
        workflow = WorkflowRepository(connection)
        running = workflow.claim("old-device", kinds=(JobKind.ASR,))
        assert running is not None and running.job_id == ids["asr"]
        with connection:
            connection.execute(
                "UPDATE workflow_jobs SET lease_expires_at = 0 WHERE job_id = ?", (running.job_id,)
            )
    finally:
        connection.close()
    source_database = (source / "archive.db").read_bytes()
    snapshot = tmp_path / "expired.zip"
    _snapshot_command(capsys, "save", "--archive-root", str(source), "--out", str(snapshot))
    target = tmp_path / "target"
    _snapshot_command(capsys, "restore", "--file", str(snapshot), "--archive-root", str(target))
    assert (source / "archive.db").read_bytes() == source_database
    connection = open_database(target)
    try:
        job = connection.execute("SELECT * FROM workflow_jobs WHERE job_id = ?", (ids["asr"],)).fetchone()
        assert job["status"] == "queued"
        assert job["lease_owner"] is None and job["lease_expires_at"] is None
        assert job["attempt_count"] == 1
        attempt = connection.execute("SELECT * FROM workflow_attempts WHERE job_id = ?", (ids["asr"],)).fetchone()
        assert attempt["outcome"] == "failed"
        assert attempt["finished_at"] is not None and attempt["error_code"]
    finally:
        connection.close()
    _resume_offline(target, monkeypatch, capsys)
    connection = open_database(target)
    try:
        attempts = connection.execute(
            "SELECT outcome FROM workflow_attempts WHERE job_id = ? ORDER BY rowid", (ids["asr"],)
        ).fetchall()
        assert [row["outcome"] for row in attempts] == ["failed", "succeeded"]
    finally:
        connection.close()


def test_snapshot_save_refuses_an_active_worker(tmp_path, capsys) -> None:
    source = tmp_path / "source"
    _seed_archive(source)
    connection = open_database(source)
    try:
        assert WorkflowRepository(connection).claim("still-running", kinds=(JobKind.ASR,)) is not None
    finally:
        connection.close()
    source_database = (source / "archive.db").read_bytes()
    snapshot = tmp_path / "active.zip"
    _reject_command(capsys, "save", "--archive-root", str(source), "--out", str(snapshot))
    assert not snapshot.exists()
    assert (source / "archive.db").read_bytes() == source_database


def test_snapshot_restore_refuses_nonempty_destination_without_overwrite(tmp_path, capsys) -> None:
    source = tmp_path / "source"
    _seed_archive(source)
    snapshot = tmp_path / "snapshot.zip"
    _snapshot_command(capsys, "save", "--archive-root", str(source), "--out", str(snapshot))
    target = tmp_path / "existing-archive"
    target.mkdir()
    sentinel = target / "precious.txt"
    sentinel.write_text("existing data", encoding="utf-8")
    _reject_command(capsys, "restore", "--file", str(snapshot), "--archive-root", str(target))
    assert sentinel.read_text(encoding="utf-8") == "existing data"
    assert sorted(path.name for path in target.iterdir()) == ["precious.txt"]


def test_snapshot_check_and_restore_reject_tampered_audio(tmp_path, capsys) -> None:
    source = tmp_path / "source"
    _seed_archive(source)
    snapshot = tmp_path / "snapshot.zip"
    _snapshot_command(capsys, "save", "--archive-root", str(source), "--out", str(snapshot))
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(snapshot) as original, zipfile.ZipFile(tampered, "w") as modified:
        for member in original.infolist():
            content = original.read(member)
            if member.filename == AUDIO_KEY:
                content = b"x" * len(content)
            modified.writestr(member, content)
    _reject_command(capsys, "check", "--file", str(tampered))
    target = tmp_path / "target"
    _reject_command(capsys, "restore", "--file", str(tampered), "--archive-root", str(target))
    assert not (target / "archive.db").exists()
    assert not target.exists() or not list(target.iterdir())


def test_snapshot_cli_rejects_invalid_compressed_member_without_traceback(tmp_path, capsys) -> None:
    source = tmp_path / "source"
    _seed_archive(source)
    snapshot = tmp_path / "snapshot.zip"
    _snapshot_command(capsys, "save", "--archive-root", str(source), "--out", str(snapshot))
    compressed = tmp_path / "compressed.zip"
    with zipfile.ZipFile(snapshot) as original, zipfile.ZipFile(
        compressed, "w", compression=zipfile.ZIP_DEFLATED,
    ) as modified:
        for member in original.infolist():
            modified.writestr(member.filename, original.read(member))
    with zipfile.ZipFile(compressed) as bundle:
        member = bundle.getinfo(AUDIO_KEY)
    content = bytearray(compressed.read_bytes())
    header = member.header_offset
    name_length = int.from_bytes(content[header + 26:header + 28], "little")
    extra_length = int.from_bytes(content[header + 28:header + 30], "little")
    content[header + 30 + name_length + extra_length] = 0xFF
    compressed.write_bytes(content)
    _reject_command(capsys, "check", "--file", str(compressed))
    target = tmp_path / "target"
    _reject_command(capsys, "restore", "--file", str(compressed), "--archive-root", str(target))
    assert not target.exists() or not list(target.iterdir())


def test_snapshot_collects_external_artifacts_into_portable_archive(tmp_path, capsys) -> None:
    source = tmp_path / "source"
    media = tmp_path / "separate-drive"
    _seed_archive(source, artifact_root=media)
    snapshot = tmp_path / "snapshot.zip"
    _snapshot_command(
        capsys, "save", "--archive-root", str(source), "--artifact-root", str(media), "--out", str(snapshot),
    )
    source.rename(tmp_path / "disconnected-source")
    media.rename(tmp_path / "disconnected-media")
    target = tmp_path / "restored"
    _snapshot_command(capsys, "restore", "--file", str(snapshot), "--archive-root", str(target))
    assert (target / AUDIO_KEY).read_bytes() == AUDIO_BYTES
    connection = sqlite3.connect(target / "archive.db")
    try:
        assert connection.execute("SELECT storage_key FROM audio_objects").fetchone()[0] == AUDIO_KEY
    finally:
        connection.close()
