"""Offline acceptance checks for explicit publication and cancellation races."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import multiprocessing
from pathlib import Path
import sqlite3
import threading
import traceback

import pytest

from bili_asr import archive, cli
from bili_asr.storage import open_database
from bili_asr.storage.workflow import JobKind, WorkflowRepository
from bili_asr.workflow_runtime import ArchiveWorkflowHandlers
from bili_asr.workflow_errors import JobExecutionError
from bili_asr.workflow_models import JobCancelledError, LeaseLostError
from tests.test_workflow_control_plane import _seed_part


@pytest.fixture
def database(tmp_path):
    connection = open_database(tmp_path)
    _seed_part(connection)
    try:
        yield connection
    finally:
        connection.close()


def _store_transcript(connection, *, transcript_id=1, part_id=1, source="subtitle-cc", created_at=1):
    text = f"{source} transcript {transcript_id}"
    with connection:
        connection.execute(
            """INSERT INTO transcripts(
                   transcript_id, video_part_id, source_kind, language, model_id,
                   version, content_sha256, created_at
               ) VALUES (?, ?, ?, 'zh-CN', NULL, 1, ?, ?)""",
            (transcript_id, part_id, source, hashlib.sha256(text.encode()).hexdigest(), created_at),
        )
        connection.execute(
            "INSERT INTO transcript_segments VALUES (?, 0, 0, 900, ?)",
            (transcript_id, text),
        )
        if source == "asr-local":
            from bili_asr.storage.models import TranscriptRecord, TranscriptSegmentRecord
            from tests.support.asr_quality import measured_fixture_evidence
            measured_fixture_evidence(connection, TranscriptRecord(transcript_id, part_id, source, "zh-CN", None,
                1, hashlib.sha256(text.encode()).hexdigest(), created_at,
                (TranscriptSegmentRecord(0, 900, text),)))
    return text


def _publish_args(root, *part_ids):
    args = ["workflow", "publish", "--archive-root", str(root)]
    for part_id in part_ids:
        args.extend(("--part-id", str(part_id)))
    return args


def _publication(connection):
    return connection.execute("SELECT * FROM workflow_jobs WHERE kind = 'publish'").fetchone()


def test_publication_contract_error_preserves_original_transcript_and_attempt(database, tmp_path, monkeypatch):
    _store_transcript(database)
    repository = WorkflowRepository(database)
    job_id, _ = repository.request_publication(video_part_id=1, transcript_id=1)
    job = repository.claim("publisher", kinds=(JobKind.PUBLISH,))
    before = [tuple(row) for row in database.execute("SELECT * FROM transcript_segments")]
    attempt_before = [tuple(row) for row in database.execute("SELECT * FROM workflow_attempts")]

    def invalid_bundle(*args, **kwargs):
        raise ValueError("SECRET model output, unrepresentable cue")

    monkeypatch.setattr(archive, "write_archive", invalid_bundle)
    handlers = ArchiveWorkflowHandlers(database, repository, archive_root=tmp_path, sessdata=None)
    try:
        with pytest.raises(JobExecutionError) as captured:
            handlers.publish(job)
    finally:
        handlers.close()
    assert captured.value.error_code == "publication_contract_failed"
    assert captured.value.safe_details == {
        "stage": "publication_validation", "reason_code": "bundle_contract_invalid", "transcript_id": 1}
    assert "SECRET" not in str(captured.value) + json.dumps(captured.value.safe_details)
    assert [tuple(row) for row in database.execute("SELECT * FROM transcript_segments")] == before
    assert [tuple(row) for row in database.execute("SELECT * FROM workflow_attempts")] == attempt_before
    assert repository.explain_job(job_id)["status"] == "running"
    assert database.execute("SELECT count(*) FROM workflow_publications").fetchone()[0] == 0


def test_backward_publication_cues_fail_without_sorting_or_clamping(database, tmp_path):
    _store_transcript(database)
    with database:
        database.execute("UPDATE transcript_segments SET start_ms=1000,end_ms=2000 WHERE transcript_id=1")
        database.execute("INSERT INTO transcript_segments VALUES (1,1,900,1800,'second backward cue')")
    repository = WorkflowRepository(database)
    repository.request_publication(video_part_id=1, transcript_id=1)
    job = repository.claim("publisher", kinds=(JobKind.PUBLISH,))
    before = [tuple(row) for row in database.execute("SELECT * FROM transcript_segments ORDER BY ordinal")]
    handlers = ArchiveWorkflowHandlers(database, repository, archive_root=tmp_path, sessdata=None)
    try:
        with pytest.raises(JobExecutionError, match="publication_contract_failed"):
            handlers.publish(job)
    finally:
        handlers.close()
    assert [tuple(row) for row in database.execute("SELECT * FROM transcript_segments ORDER BY ordinal")] == before
    assert database.execute("SELECT count(*) FROM workflow_publications").fetchone()[0] == 0
    assert not list(tmp_path.rglob(".bundle-complete.json"))


@pytest.mark.parametrize("failure", [LeaseLostError("lease lost"), JobCancelledError("cancelled"), OSError("disk error")])
def test_publication_operational_failure_is_not_reclassified(database, tmp_path, monkeypatch, failure):
    _store_transcript(database)
    repository = WorkflowRepository(database)
    repository.request_publication(video_part_id=1, transcript_id=1)
    job = repository.claim("publisher", kinds=(JobKind.PUBLISH,))

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(archive, "write_archive", fail)
    handlers = ArchiveWorkflowHandlers(database, repository, archive_root=tmp_path, sessdata=None)
    try:
        with pytest.raises(type(failure)) as captured:
            handlers.publish(job)
    finally:
        handlers.close()
    assert captured.value is failure


class _PublicationCommitFailure(sqlite3.OperationalError):
    pass


class _PublicationConnection(sqlite3.Connection):
    fail_next_commit = False

    def commit(self):
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise _PublicationCommitFailure("injected publication failure")
        return super().commit()


def _republish_after_expiry(root, now, events, results):
    # A fresh process has its own archive lock and SQLite connection. Advance
    # only its scheduler clock to model reclamation after the old lease expires.
    from bili_asr.storage import workflow as workflow_storage

    workflow_storage._now = lambda: now
    connection = sqlite3.connect(Path(root) / "archive.db", timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.set_trace_callback(
        lambda sql: events["attempted"].set() if sql == "BEGIN IMMEDIATE" else None,
    )
    try:
        events["ready"].set()
        assert events["start"].wait(10), "old publisher never started failure cleanup"
        repository = WorkflowRepository(connection)
        job = repository.claim("new-publisher", kinds=(JobKind.PUBLISH,))
        assert job is not None
        handlers = ArchiveWorkflowHandlers(connection, repository, archive_root=root, sessdata=None)
        try:
            result = handlers.publish(job)
        finally:
            handlers.close()
        repository.finish(job.job_id, worker_id=job.lease_owner, result=result,
                          expected_attempt_count=job.attempt_count)
        results.put({"result": result, "attempt_count": job.attempt_count})
    except BaseException:
        results.put({"error": traceback.format_exc()})
    finally:
        connection.close()
        events["finished"].set()


@pytest.mark.parametrize("invalid_part_id", [2, 99])
def test_publish_validates_every_selected_target_before_queuing_any(database, tmp_path, capsys, invalid_part_id):
    _store_transcript(database)
    with database:
        database.execute(
            """INSERT INTO video_parts(
                   video_part_id, bvid, page_index, cid, title, duration_ms,
                   processing_status, created_at, updated_at
               ) VALUES (2, 'BVtest', 1, 2, 'no transcript', 1000, 'metadata_collected', 1, 1)"""
        )

    assert cli.main(_publish_args(tmp_path, 1, invalid_part_id)) == 1
    captured = capsys.readouterr()
    assert "workflow publish:" in captured.err
    assert captured.out == ""
    assert database.execute("SELECT COUNT(*) FROM workflow_jobs").fetchone()[0] == 0


@pytest.mark.parametrize("source", ["subtitle-cc", "subtitle-ai"])
def test_publish_cli_and_worker_prefer_a_usable_decode_over_any_caption(database, tmp_path, capsys, source):
    """A finished decode with usable admission is the archival text.

    The caption is still projected as the preferred candidate when no decode
    exists; this case measures the decode winning when one does — including
    against a caption stored first and against both caption kinds.
    """
    _store_transcript(database, source=source)
    asr_text = _store_transcript(database, transcript_id=2, source="asr-local", created_at=100)

    assert cli.main(_publish_args(tmp_path, 1)) == 0
    assert "transcript_id=2" in capsys.readouterr().out
    row = _publication(database)
    assert json.loads(row["payload_json"])["transcript_id"] == 2
    repository = WorkflowRepository(database)
    job = repository.claim("offline-publisher", kinds=(JobKind.PUBLISH,))
    assert job is not None
    handlers = ArchiveWorkflowHandlers(database, repository, archive_root=tmp_path, sessdata=None)
    try:
        result = handlers.publish(job)
    finally:
        handlers.close()
    repository.finish(job.job_id, worker_id=job.lease_owner, result=result,
                      expected_attempt_count=job.attempt_count)

    assert result["transcript_id"] == 2
    assert result["source_kind"] == "asr-local"
    assert (tmp_path / result["txt_path"]).read_text(encoding="utf-8") == asr_text + "\n"
    assert database.execute("SELECT transcript_id FROM workflow_publications").fetchone()[0] == 2


def test_explicit_publish_requeues_succeeded_job_and_preserves_attempt_history(database, tmp_path, capsys):
    _store_transcript(database)
    assert cli.main(_publish_args(tmp_path, 1)) == 0
    capsys.readouterr()
    repository = WorkflowRepository(database)
    job = repository.claim("offline-publisher", kinds=(JobKind.PUBLISH,))
    assert job is not None
    repository.finish(job.job_id, worker_id=job.lease_owner,
                      result={"requested_transcript_id": 1, "transcript_id": 1},
                      expected_attempt_count=job.attempt_count)
    before = [dict(row) for row in database.execute("SELECT * FROM workflow_attempts")]

    assert cli.main(_publish_args(tmp_path, 1)) == 0
    assert "status=queued" in capsys.readouterr().out
    row = _publication(database)
    assert row["job_id"] == job.job_id
    assert row["status"] == "queued" and row["attempt_count"] == 1
    assert [dict(row) for row in database.execute("SELECT * FROM workflow_attempts")] == before
    next_job = repository.claim("offline-publisher", kinds=(JobKind.PUBLISH,))
    assert next_job is not None and next_job.job_id == job.job_id and next_job.attempt_count == 2


@pytest.mark.parametrize("running", [False, True])
def test_publish_cli_keeps_cancelled_job_and_attempt_history_unchanged(database, tmp_path, capsys, running):
    _store_transcript(database)
    assert cli.main(_publish_args(tmp_path, 1)) == 0
    capsys.readouterr()
    repository = WorkflowRepository(database)
    job_id = _publication(database)["job_id"]
    if running:
        assert repository.claim("offline-publisher", kinds=(JobKind.PUBLISH,)) is not None
    assert repository.cancel(job_ids=[job_id])[0].changed
    before_job = dict(_publication(database))
    before_attempts = [dict(row) for row in database.execute("SELECT * FROM workflow_attempts")]

    assert cli.main(_publish_args(tmp_path, 1)) == 0
    assert "status=cancelled" in capsys.readouterr().out
    assert dict(_publication(database)) == before_job
    assert [dict(row) for row in database.execute("SELECT * FROM workflow_attempts")] == before_attempts
    assert repository.claim("offline-publisher", kinds=(JobKind.PUBLISH,)) is None


@pytest.mark.parametrize("running", [False, True])
def test_concurrent_publication_request_cannot_mutate_or_revive_cancelled_job(database, tmp_path, running):
    repository = WorkflowRepository(database)
    job_id, _ = repository.request_publication(video_part_id=1, transcript_id=1)
    if running:
        assert repository.claim("publisher", kinds=(JobKind.PUBLISH,)) is not None
    cancel_attempted = threading.Event()
    cancel_finished = threading.Event()
    failures = []
    cancelled_payloads = []
    thread = None
    read_was_transactional = None

    def cancel():
        other = sqlite3.connect(tmp_path / "archive.db", timeout=5)
        other.row_factory = sqlite3.Row
        other.set_trace_callback(lambda sql: cancel_attempted.set() if sql == "BEGIN IMMEDIATE" else None)
        try:
            result = WorkflowRepository(other).cancel(job_ids=[job_id])
            assert result[0].changed
            cancelled_payloads.append(other.execute(
                "SELECT payload_json FROM workflow_jobs WHERE job_id = ?", (job_id,),
            ).fetchone()[0])
        except BaseException as exc:
            failures.append(exc)
        finally:
            other.close()
            cancel_finished.set()

    def trace(sql):
        nonlocal thread, read_was_transactional
        if sql.startswith("SELECT job_id, payload_json, status FROM workflow_jobs"):
            read_was_transactional = database.in_transaction
        if thread is None and sql.lstrip().startswith("UPDATE workflow_jobs"):
            thread = threading.Thread(target=cancel, daemon=True)
            thread.start()
            if not cancel_attempted.wait(5):
                failures.append(AssertionError("canceller did not reach BEGIN IMMEDIATE"))
            # An unguarded SELECT lets cancellation commit before this UPDATE.
            # Force that old race deterministically; a guarded read instead
            # holds SQLite's write lock until the publication request commits.
            if read_was_transactional is False and not cancel_finished.wait(5):
                failures.append(AssertionError("unguarded cancellation did not commit"))

    database.set_trace_callback(trace)
    try:
        repository.request_publication(video_part_id=1, transcript_id=2, force=True)
    finally:
        database.set_trace_callback(None)
    assert thread is not None
    thread.join(10)
    assert not thread.is_alive() and not failures
    assert cancel_finished.is_set()
    row = _publication(database)
    assert row["status"] == "cancelled"
    assert [row["payload_json"]] == cancelled_payloads
    assert repository.claim("publisher", kinds=(JobKind.PUBLISH,)) is None


@pytest.mark.parametrize("failure", ["guard_exit", "commit"])
def test_failed_publication_cleans_marker_under_sqlite_lock_before_another_process_republishes(
    database, tmp_path, monkeypatch, failure,
):
    _store_transcript(database)
    connection = sqlite3.connect(tmp_path / "archive.db", timeout=10, factory=_PublicationConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    repository = WorkflowRepository(connection)
    job_id, _ = repository.request_publication(video_part_id=1, transcript_id=1)
    job = repository.claim("old-publisher", kinds=(JobKind.PUBLISH,))
    assert job is not None and job.job_id == job_id
    lease_end = connection.execute(
        "SELECT lease_expires_at FROM workflow_jobs WHERE job_id = ?", (job_id,),
    ).fetchone()[0]
    marker = archive.bundle_marker_path(archive.bundle_paths_for_stem(tmp_path, "BVtest.p0")["srt_path"])
    context = multiprocessing.get_context("spawn")
    events = {name: context.Event() for name in ("ready", "start", "attempted", "finished")}
    results = context.Queue()
    process = context.Process(target=_republish_after_expiry,
                              args=(str(tmp_path), lease_end + 1, events, results))
    owned_transaction = repository.owned_transaction
    cleanup_states = []
    republished = []

    @contextmanager
    def failing_guard(selected, *, on_rollback=None):
        assert on_rollback is not None, "runtime did not forward the archive cleanup callback"

        def invalidate_under_lock():
            cleanup_states.append(connection.in_transaction)
            assert marker.is_file(), "failure was not injected after complete bundle replacement"
            events["start"].set()
            assert events["attempted"].wait(10), "new publisher never attempted SQLite BEGIN IMMEDIATE"
            # A trace event alone cannot prove a connection is blocked. Probe
            # SQLite with a third connection and a zero busy timeout as well.
            probe = sqlite3.connect(tmp_path / "archive.db", timeout=0)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    probe.execute("BEGIN IMMEDIATE")
            finally:
                probe.close()
            assert not events["finished"].is_set()
            on_rollback()
            assert not marker.exists()

        try:
            with owned_transaction(selected, on_rollback=invalidate_under_lock):
                yield
                if failure == "guard_exit":
                    raise _PublicationCommitFailure("injected publication failure")
                connection.fail_next_commit = True
        except _PublicationCommitFailure:
            assert not connection.in_transaction
            # Suspend the old guard after rollback until the independent
            # publisher commits. Any delayed outer invalidation now deletes
            # the new marker and makes the assertions below fail.
            assert events["finished"].wait(10), "new publisher did not finish after lock release"
            result = results.get(timeout=2)
            assert "error" not in result, result.get("error")
            republished.append(result)
            assert marker.is_file()
            raise

    monkeypatch.setattr(repository, "owned_transaction", failing_guard)
    handlers = ArchiveWorkflowHandlers(connection, repository, archive_root=tmp_path, sessdata=None)
    process.start()
    try:
        # Coverage instrumentation on WSL can exceed ten seconds while a
        # spawned process imports sources from a Windows mount. Allow startup
        # separately from the unchanged publication and lock-boundary waits.
        assert events["ready"].wait(60), "independent publisher did not open its SQLite connection"
        with pytest.raises(_PublicationCommitFailure, match="injected publication failure"):
            handlers.publish(job)
        process.join(10)
        assert not process.is_alive() and process.exitcode == 0
        assert cleanup_states == [True]
        assert len(republished) == 1 and republished[0]["attempt_count"] == 2
        assert marker.is_file()
        paths = archive.bundle_relpaths_for_stem("BVtest.p0")
        assert all(republished[0]["result"][key] == value for key, value in paths.items())
        assert archive.archive_bundle_complete(tmp_path, paths)
        assert _publication(database)["status"] == "succeeded"
        assert database.execute("SELECT COUNT(*) FROM workflow_publications").fetchone()[0] == 1
        attempts = database.execute(
            "SELECT outcome, error_code FROM workflow_attempts WHERE job_id = ? ORDER BY rowid", (job_id,),
        ).fetchall()
        assert [(row["outcome"], row["error_code"]) for row in attempts] == [
            ("failed", "lease_expired"), ("succeeded", None),
        ]
        assert not list((tmp_path / "transcripts").rglob("*.tmp"))
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)
        handlers.close()
        connection.close()
        results.close()
        results.join_thread()
