"""Default and catalog archives consume the exact successful audio prerequisite."""
from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from bili_asr.artifact_inventory import stream_hash
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.asr.session import AsrInferenceSession
from bili_asr.services.workflow_audio_input import verified_audio_input
from bili_asr.storage import (
    AsrPolicy,
    AsrProfile,
    JobKind,
    TranscriptRepository,
    open_database,
)
from bili_asr.storage.archive_contracts import _resource
from bili_asr.storage.workflow import (
    JobCancelledError,
    LeaseLostError,
    WorkflowRepository,
)
from bili_asr.workflow import WorkflowExecutor
from bili_asr.workflow_errors import JobExecutionError
from bili_asr.workflow_runtime import ArchiveWorkflowHandlers
from tests.test_workflow_control_plane import _seed_part


@pytest.fixture(params=[False, True], ids=["default", "catalog"])
def input_worker(tmp_path, request, monkeypatch):
    connection = open_database(tmp_path)
    if request.param:
        connection.executescript(_resource("schema-artifact-storage.sql"))
    _seed_part(connection)
    workflow = WorkflowRepository(connection)
    profile = workflow.register_profile(AsrProfile("identity", "offline", device="cuda"))
    workflow.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=profile)
    path = tmp_path / "audio" / "input.m4a"
    path.parent.mkdir()
    content = b"original audio bytes"
    path.write_bytes(content)
    monkeypatch.setattr("bili_asr.workflow_runtime.subprocess.run", lambda *args, **kwargs:
                        type("Probe", (), {"stdout": '{"format":{"duration":1}}'})())
    handlers = ArchiveWorkflowHandlers(connection, workflow, archive_root=tmp_path, sessdata=None)
    audio_job = workflow.claim("audio", kinds=(JobKind.AUDIO,))
    result = handlers.store_audio(audio_job, handlers._part(audio_job), path, path)
    workflow.finish(audio_job.job_id, worker_id="audio", result=result)
    try:
        yield connection, workflow, handlers, path, content, audio_job
    finally:
        handlers.close()
        connection.close()
    assert not list(path.parent.glob(".workflow-audio-input-*"))


def _transcribe(content, seen):
    def transcribe(config, path, **kwargs):
        seen.append(Path(path))
        assert Path(path).read_bytes() == content
        return [{"start": 0, "end": 1, "text": "verified input"}], {"language": "English"}, _coverage()
    return transcribe


def _coverage():
    return {"decoded_s": 1.0, "produced_s": 1.0, "coverage": 1.0,
            "coverage_min": .85, "coverage_short": False}


def test_matching_prerequisite_uses_fixed_generation_and_records_verification(input_worker):
    connection, workflow, handlers, path, content, audio_job = input_worker
    old_attempts = [tuple(row) for row in connection.execute("SELECT * FROM workflow_attempts WHERE job_id=?", (audio_job.job_id,))]
    seen = []
    handlers.timeout_transcriber = _transcribe(content, seen)
    job = workflow.claim("asr", kinds=(JobKind.ASR,))
    result = handlers.local_asr(job)
    evidence = TranscriptRepository(connection).read_asr_evidence(result["run_id"], 1)["audio"]
    assert evidence["sha256"] == hashlib.sha256(content).hexdigest()
    assert evidence["verification"]["status"] == "matched"
    assert evidence["verification"]["observed_byte_size"] == len(content)
    assert len(seen) == 1 and seen[0] != path and seen[0].suffix == path.suffix
    assert not seen[0].exists()
    assert path.read_bytes() == content
    assert [tuple(row) for row in connection.execute("SELECT * FROM workflow_attempts WHERE job_id=?", (audio_job.job_id,))] == old_attempts


@pytest.mark.parametrize("invalid", ["wrong-bytes", "missing"])
def test_input_failure_preserves_acquisition_and_records_only_consumer_failure(input_worker, invalid):
    connection, workflow, handlers, path, content, audio_job = input_worker
    old = [tuple(row) for row in connection.execute("SELECT * FROM workflow_attempts WHERE job_id=?", (audio_job.job_id,))]
    if invalid == "missing":
        path.unlink()
    else:
        path.write_bytes(b"x" * len(content))
    handlers.timeout_transcriber = lambda *args, **kwargs: pytest.fail("invalid input must not run a model")
    summary = WorkflowExecutor(workflow, worker_id="asr", handlers=handlers.handlers(), kinds=(JobKind.ASR,)).run(limit=1)
    assert summary.failed == 1 and summary.succeeded == 0
    assert connection.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0
    failure = connection.execute("SELECT error_code,result_json FROM workflow_attempts WHERE job_id != ? AND outcome='failed'", (audio_job.job_id,)).fetchone()
    details = json.loads(failure["result_json"])["diagnostic"]
    if invalid == "wrong-bytes":
        identity = details.get("identity", details)
        assert identity["actual_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert identity["actual_sha256"] != identity["expected_sha256"]
        assert identity["stage"] == "before-inference"
    assert [tuple(row) for row in connection.execute("SELECT * FROM workflow_attempts WHERE job_id=?", (audio_job.job_id,))] == old


def test_replacement_after_validation_cannot_change_inference_bytes_or_publish_stale_generation(input_worker):
    connection, workflow, handlers, original, content, _job = input_worker
    seen = []
    def replace_name(config, path, **kwargs):
        replacement = original.with_name("replacement.m4a")
        replacement.write_bytes(b"new audio at mutable acquisition name")
        os.replace(replacement, original)
        return _transcribe(content, seen)(config, path, **kwargs)
    handlers.timeout_transcriber = replace_name
    with pytest.raises(JobExecutionError, match="audio_generation_changed"):
        handlers.local_asr(workflow.claim("asr", kinds=(JobKind.ASR,)))
    assert seen and original.read_bytes() != content
    assert connection.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0


def test_restoring_bytes_and_mtime_after_in_place_write_cannot_hide_generation_change(input_worker):
    connection, workflow, handlers, original, content, _audio = input_worker
    initial = original.stat()
    def aba_write(config, path, **kwargs):
        original.write_bytes(b"x" * len(content))
        original.write_bytes(content)
        os.utime(original, ns=(initial.st_atime_ns, initial.st_mtime_ns))
        return _transcribe(content, [])(config, path, **kwargs)
    handlers.timeout_transcriber = aba_write
    with pytest.raises(JobExecutionError, match="audio_generation_changed") as failure:
        handlers.local_asr(workflow.claim("asr", kinds=(JobKind.ASR,)))
    assert failure.value.safe_details["stage"] == "after-inference"
    assert original.read_bytes() == content and original.stat().st_mtime_ns == initial.st_mtime_ns
    assert connection.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0


def test_in_place_write_during_inference_discards_transcript_and_keeps_failure(input_worker):
    connection, workflow, handlers, original, _content, _job = input_worker
    def mutate_bytes(*args, **kwargs):
        original.write_bytes(b"changed in place while the model is reading")
        return [{"start": 0, "end": 1, "text": "must discard"}], {"language": "English"}, None
    handlers.timeout_transcriber = mutate_bytes
    with pytest.raises(JobExecutionError, match="audio_generation_changed") as error:
        handlers.local_asr(workflow.claim("asr", kinds=(JobKind.ASR,)))
    assert error.value.safe_details["stage"] == "after-inference"
    assert connection.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0
    assert connection.execute("SELECT outcome FROM acquisition_runs WHERE kind='asr'").fetchone()[0] == "failed"
    if connection.execute("SELECT name FROM sqlite_master WHERE name='artifact_pins'").fetchone():
        assert connection.execute("SELECT COUNT(*) FROM artifact_pins WHERE released_at IS NULL").fetchone()[0] == 0


@pytest.mark.parametrize("revoke", ["cancel", "lease"])
def test_revoked_attempt_during_streaming_hash_cannot_start_inference(input_worker, monkeypatch, revoke):
    connection, workflow, handlers, original, _content, _audio = input_worker
    # Hashing remains chunked, and each chunk checks authoritative ownership.
    original.write_bytes(b"x" * (3 * 1024 * 1024))
    job = workflow.claim("asr", kinds=(JobKind.ASR,))
    from bili_asr.services import workflow_audio_input
    original_hash = workflow_audio_input.stream_hash
    def revoke_on_chunk(stream, destination=None, **kwargs):
        checkpoint = kwargs["checkpoint"]
        calls = 0
        def checked():
            nonlocal calls
            calls += 1
            if calls == 2:
                if revoke == "cancel":
                    workflow.cancel(job_ids=[job.job_id])
                else:
                    with connection:
                        connection.execute("UPDATE workflow_jobs SET lease_owner='new-owner' WHERE job_id=?", (job.job_id,))
            checkpoint()
        return original_hash(stream, destination, checkpoint=checked)
    monkeypatch.setattr(workflow_audio_input, "stream_hash", revoke_on_chunk)
    handlers.timeout_transcriber = lambda *args, **kwargs: pytest.fail("revoked input must not run")
    with pytest.raises(JobCancelledError if revoke == "cancel" else LeaseLostError):
        handlers.local_asr(job)
    assert connection.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0


def test_missing_historical_digest_is_observed_only_without_rewriting_old_attempt(tmp_path):
    with open_database(tmp_path) as connection:
        _seed_part(connection)
        workflow = WorkflowRepository(connection)
        workflow.plan_with_profile(part_ids=[1], policy=AsrPolicy.ALL,
                                   profile=AsrProfile("historical", "offline", device="cuda"))
        path = tmp_path / "audio/input.m4a"
        path.parent.mkdir()
        path.write_bytes(b"historical bytes with unknown acquisition hash")
        audio = workflow.claim("audio", kinds=(JobKind.AUDIO,))
        workflow.finish(audio.job_id, worker_id="audio", result={"storage_key": "audio/input.m4a"})
        previous = connection.execute("SELECT result_json FROM workflow_attempts WHERE job_id=?", (audio.job_id,)).fetchone()[0]
        handlers = ArchiveWorkflowHandlers(connection, workflow, archive_root=tmp_path, sessdata=None,
                                           timeout_transcriber=_transcribe(path.read_bytes(), []))
        try:
            result = handlers.local_asr(workflow.claim("asr", kinds=(JobKind.ASR,)))
        finally:
            handlers.close()
        evidence = TranscriptRepository(connection).read_asr_evidence(result["run_id"], 1)["audio"]
        assert "sha256" not in evidence
        assert evidence["verification"]["status"] == "observed-only"
        assert evidence["verification"]["expected_sha256"] is None
        assert evidence["verification"]["observed_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert connection.execute("SELECT result_json FROM workflow_attempts WHERE job_id=?", (audio.job_id,)).fetchone()[0] == previous


def test_large_hash_requests_only_bounded_chunks_and_checks_last_chunk():
    class LargeStream:
        def __init__(self):
            self.remaining = 9 * 1024 * 1024 + 13
            self.reads = []
        def read(self, size):
            assert 0 < size <= 1024 * 1024
            self.reads.append(size)
            chunk = b"x" * min(size, self.remaining)
            self.remaining -= len(chunk)
            return chunk
    stream = LargeStream()
    checked = []
    size, _digest = stream_hash(stream, checkpoint=lambda: checked.append(True))
    assert size == 9 * 1024 * 1024 + 13
    assert len(checked) == len(stream.reads) == 11


class ReadingProcessRunner:
    """A spawnable model double opens exactly the input the parent verified."""
    def __init__(self, config):
        pass
    def set_hotword_evidence(self, **kwargs):
        pass
    def transcribe(self, path, **kwargs):
        return [{"start": 0, "end": 1, "text": Path(path).read_bytes().decode("ascii")}]
    def rebuild_hotwords_from_first_pass(self, text):
        return []
    def provenance(self):
        return {"language": "English"}
    def transcribed_coverage(self):
        return _coverage()
    def diagnostics(self):
        return {}
    def release(self):
        pass


def test_real_spawn_process_can_read_the_verified_fixed_generation(input_worker):
    connection, workflow, handlers, _path, content, _audio = input_worker
    handlers.inference_session = AsrInferenceSession(runner_factory=ReadingProcessRunner)
    result = handlers.local_asr(workflow.claim("asr", kinds=(JobKind.ASR,)))
    assert connection.execute("SELECT text FROM transcript_segments WHERE transcript_id=?", (result["transcript_id"],)).fetchone()[0] == content.decode("ascii")


def test_explicit_size_conflict_refuses_inference_and_keeps_original_result(input_worker):
    connection, workflow, handlers, _path, content, audio_job = input_worker
    with connection:
        row = connection.execute("SELECT result_json FROM workflow_attempts WHERE job_id=?", (audio_job.job_id,)).fetchone()
        result = json.loads(row[0])
        result["byte_size"] = len(content) + 1
        connection.execute("UPDATE workflow_attempts SET result_json=? WHERE job_id=?", (json.dumps(result), audio_job.job_id))
    handlers.timeout_transcriber = lambda *args, **kwargs: pytest.fail("conflicting size must not run")
    with pytest.raises(JobExecutionError, match="artifact_input_identity_mismatch") as failure:
        handlers.local_asr(workflow.claim("asr", kinds=(JobKind.ASR,)))
    assert failure.value.safe_details["reason"] == "dependency-size-conflict"
    assert connection.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0


def test_insufficient_space_refuses_before_private_copy_and_releases_pin(input_worker, monkeypatch):
    connection, workflow, handlers, path, _content, _audio = input_worker
    from bili_asr.services import asr_input_space
    monkeypatch.setattr(asr_input_space.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    handlers.timeout_transcriber = lambda *args, **kwargs: pytest.fail("space exhaustion must not run")
    with pytest.raises(JobExecutionError, match="audio_input_space_unavailable"):
        handlers.local_asr(workflow.claim("asr", kinds=(JobKind.ASR,)))
    assert not list(path.parent.glob(".workflow-audio-input-*"))
    assert not list((path.parent.parent / ".asr-input-reservations").glob("*.json"))
    assert connection.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0


@pytest.mark.parametrize("repeat", range(3))
def test_overlapping_profiles_use_independent_private_inodes_without_generation_false_positives(tmp_path, repeat):
    with open_database(tmp_path) as connection:
        _seed_part(connection)
    path = tmp_path / "audio/input.m4a"
    path.parent.mkdir()
    content = b"same shared audio for concurrent profiles"
    path.write_bytes(content)
    dependency = {"storage_key": "audio/input.m4a", "sha256": hashlib.sha256(content).hexdigest(), "byte_size": len(content)}
    barrier = Barrier(2)
    def consume(_index):
        connection = open_database(tmp_path)
        try:
            with verified_audio_input(connection, ArtifactRoots.of(tmp_path), 1, dependency,
                                      retained=None, checkpoint=lambda: None) as audio_input:
                barrier.wait(timeout=10)
                assert audio_input.path.read_bytes() == content
                audio_input.assert_unchanged()
                return audio_input.path
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as workers:
        paths = list(workers.map(consume, range(2)))
    assert paths[0] != paths[1] and all(not entry.exists() for entry in paths)
    assert path.read_bytes() == content


def test_stream_copy_completes_short_writes_and_refuses_zero_progress():
    from io import BytesIO
    class ShortWriter:
        def __init__(self, stop=False):
            self.buffer = BytesIO()
            self.stop = stop
        def write(self, chunk):
            if self.stop:
                return 0
            return self.buffer.write(chunk[:3])
    content = b"one bounded artifact chunk"
    destination = ShortWriter()
    assert stream_hash(BytesIO(content), destination) == (len(content), hashlib.sha256(content).hexdigest())
    assert destination.buffer.getvalue() == content
    with pytest.raises(OSError, match="cannot complete artifact copy"):
        stream_hash(BytesIO(content), ShortWriter(stop=True))


def test_private_input_corruption_never_commits_even_when_source_is_unchanged(input_worker):
    connection, workflow, handlers, original, content, _audio = input_worker
    def corrupt_input(config, path, **kwargs):
        Path(path).write_bytes(b"corrupted private input")
        return [{"start": 0, "end": 1, "text": "must discard"}], {"language": "English"}, None
    handlers.timeout_transcriber = corrupt_input
    with pytest.raises(JobExecutionError, match="audio_digest_mismatch"):
        handlers.local_asr(workflow.claim("asr", kinds=(JobKind.ASR,)))
    assert original.read_bytes() == content
    assert connection.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0


def test_readonly_legacy_fallback_never_receives_private_input_or_reservation_writes(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    artifacts = tmp_path / "artifacts"
    archive.mkdir()
    artifacts.mkdir()
    connection = open_database(archive)
    _seed_part(connection)
    source = archive / "audio/input.m4a"
    source.parent.mkdir()
    content = b"correct input in readonly legacy fallback"
    source.write_bytes(content)
    original_open = Path.open
    def confined_write(path, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+") and path.is_relative_to(archive / "audio"):
            pytest.fail("private input must use the configured write base")
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", confined_write)
    roots = ArtifactRoots.of(archive, artifacts)
    try:
        with verified_audio_input(connection, roots, 1,
                                  {"storage_key": "audio/input.m4a", "sha256": hashlib.sha256(content).hexdigest()},
                                  retained=None, checkpoint=lambda: None) as audio_input:
            assert audio_input.path.is_relative_to(artifacts / "audio")
            assert audio_input.path.read_bytes() == content
            audio_input.assert_unchanged()
        assert not (archive / ".asr-input-reservations").exists()
    finally:
        connection.close()
