"""A worker loses all authoritative results when its commit fence fails."""

from __future__ import annotations

from threading import Event, Thread

import pytest

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.sources.models import SubtitleSegment, SubtitleTrack
from bili_asr.storage import AcquisitionRunRecord, TranscriptRepository, TranscriptSegmentRecord
from bili_asr.storage.job_commit import JobCommitGuard
from bili_asr.storage.workflow import WorkflowRepository
import bili_asr.storage.workflow as workflow_storage
from bili_asr.workflow_models import AsrPolicy, AsrProfile, JobCancelledError, JobKind, LeaseLostError
from bili_asr.workflow_runtime import ArchiveWorkflowHandlers
from tests.test_archive_sessions import _exclusive_available
from tests.test_workflow_control_plane import _seed_part


@pytest.fixture
def session(tmp_path):
    with ArchiveSession(tmp_path, mode=ArchiveAccessMode.BOOTSTRAP) as opened:
        _seed_part(opened.connection)
        yield opened


def _claim(session, kind=JobKind.SUBTITLE):
    repository = WorkflowRepository(session.connection)
    repository.plan_with_profile(part_ids=[1], policy=AsrPolicy.ALL,
        profile=AsrProfile("result-fence", "offline-model", device="cuda"))
    if kind is JobKind.ASR:
        audio = repository.claim("audio-worker", kinds=(JobKind.AUDIO,))
        repository.finish(audio.job_id, worker_id="audio-worker", result={"storage_key": "audio/offline.m4a"})
    job = repository.claim("result-worker", kinds=(kind,))
    expiry = session.connection.execute(
        "SELECT lease_expires_at FROM workflow_jobs WHERE job_id = ?", (job.job_id,)).fetchone()[0]
    now = [expiry - 1]
    repository.commit_guard = JobCommitGuard(session.connection, clock=lambda: now[0])
    return repository, job, now, expiry


def _start(repository, kind="subtitle"):
    repository.start_acquisition_run(AcquisitionRunRecord(
        run_id="result-run", kind=kind, selector_kind="bvid", selector_target="BVtest",
        requested_limit=1, credential_present=False, started_at=1))


def _assert_no_result(connection):
    for table in ("transcripts", "transcript_segments", "acquisition_attempts", "asr_models",
                  "transcript_asr_evidence", "transcript_coverage_attestations"):
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert not connection.in_transaction


@pytest.mark.parametrize("guard", ["callback", "shared_transaction"])
@pytest.mark.parametrize("result", ["subtitle", "asr", "absence", "finish_run"])
def test_exact_expiry_inside_result_transaction_rolls_back_all_facts(session, guard, result):
    connection = session.connection
    workflow, job, now, expiry = _claim(session, JobKind.ASR if result == "asr" else JobKind.SUBTITLE)
    options = {"write_guard": lambda: workflow.assert_lease(job)} if guard == "callback" else {
        "write_transaction": lambda: workflow.owned_transaction(job)}
    repository = TranscriptRepository(connection, **options)
    _start(repository, "asr" if result == "asr" else "subtitle")
    prefix = "UPDATE acquisition_runs" if result == "finish_run" else "INSERT INTO acquisition_attempts"

    def expire_on_last_write(sql):
        if sql.lstrip().startswith(prefix):
            now[0] = expiry

    connection.set_trace_callback(expire_on_last_write)
    try:
        with pytest.raises(LeaseLostError):
            if result == "subtitle":
                repository.record_acquired_transcript(run_id="result-run", video_part_id=1,
                    source_kind="subtitle-ai", language="zh-CN",
                    segments=(TranscriptSegmentRecord(0, 900, "must not commit"),),
                    started_at=1, finished_at=2, created_at=2)
            elif result == "asr":
                repository.record_local_transcript(run_id="result-run", video_part_id=1,
                    language="zh-CN", segments=(TranscriptSegmentRecord(0, 900, "must not commit"),),
                    model_name="offline-model", model_revision="fixed", started_at=1, finished_at=2, created_at=2,
                    coverage={"decoded_s": 1.0, "produced_s": .9, "coverage": .9,
                              "coverage_min": .8, "coverage_short": False},
                    asr_evidence={"schema_version": 1, "diagnostics": {"source": "offline"}})
            elif result == "absence":
                repository.record_subtitle_attempt(run_id="result-run", video_part_id=1,
                    outcome="no-subtitle", error_code=None, started_at=1, finished_at=2,
                    credential_verified=True)
            else:
                repository.finish_acquisition_run("result-run", 2, outcome="complete")
    finally:
        connection.set_trace_callback(None)
    assert now[0] == expiry
    _assert_no_result(connection)
    assert connection.execute("SELECT outcome FROM acquisition_runs").fetchone()[0] == "running"
    # Failure cleanup remains possible after the fence rejects success.
    repository.finish_acquisition_run("result-run", 2, outcome="failed")
    assert connection.execute("SELECT outcome FROM acquisition_runs").fetchone()[0] == "failed"


@pytest.mark.parametrize("change", ["cancelled", "owner", "attempt"])
def test_result_owner_change_inside_transaction_rolls_back_transcript_and_attempt(session, change):
    connection = session.connection
    workflow, job, _, _ = _claim(session)
    repository = TranscriptRepository(connection, write_transaction=lambda: workflow.owned_transaction(job))
    _start(repository)
    # Inject a change after result writes: a writer lock normally prevents a
    # different SQLite connection from changing these fields concurrently.
    mutation = {"cancelled": "status = 'cancelled'", "owner": "lease_owner = 'other-owner'",
                "attempt": "attempt_count = attempt_count + 1"}[change]
    connection.execute(f"""CREATE TEMP TRIGGER change_result_owner AFTER INSERT ON acquisition_attempts
        BEGIN UPDATE workflow_jobs SET {mutation} WHERE kind = 'subtitle'; END""")
    error = JobCancelledError if change == "cancelled" else LeaseLostError
    with pytest.raises(error):
        repository.record_acquired_transcript(run_id="result-run", video_part_id=1,
            source_kind="subtitle-ai", language="zh-CN", segments=(TranscriptSegmentRecord(0, 900, "late"),),
            started_at=1, finished_at=2, created_at=2)
    _assert_no_result(connection)


@pytest.mark.parametrize("kind", [JobKind.SUBTITLE, JobKind.ASR])
def test_runtime_result_expiry_uses_shared_guard_and_failure_cleanup_releases_session(session, tmp_path, kind):
    connection = session.connection
    workflow, job, now, expiry = _claim(session, kind)

    class Gateway:
        async def get_subtitle_tracks(self, bvid, cid):
            return (SubtitleTrack("zh-CN", "caption", False, "offline"),)

        async def fetch_subtitle_segments(self, track, bvid, cid):
            return (SubtitleSegment(0, 900, "late subtitle"),)

    def transcribe(*args, **kwargs):
        kwargs["diagnostics_sink"].update({"source": "offline"})
        return [{"start": 0, "end": .9, "text": "late ASR"}], {"language": "Chinese"}, None

    if kind is JobKind.ASR:
        (tmp_path / "audio").mkdir()
        (tmp_path / "audio/offline.m4a").write_bytes(b"offline audio")
    handlers = ArchiveWorkflowHandlers(connection, workflow, archive_root=tmp_path, sessdata=None,
        gateway_factory=lambda **kwargs: Gateway(), timeout_transcriber=transcribe)

    def expire_on_insert(sql):
        if sql.lstrip().startswith("INSERT INTO acquisition_attempts"):
            now[0] = expiry

    connection.set_trace_callback(expire_on_insert)
    try:
        with pytest.raises(LeaseLostError):
            (handlers.subtitle if kind is JobKind.SUBTITLE else handlers.local_asr)(job)
    finally:
        connection.set_trace_callback(None)
        handlers.close()
    _assert_no_result(connection)
    run = connection.execute("SELECT outcome, finished_at FROM acquisition_runs").fetchone()
    assert run["outcome"] == "failed" and run["finished_at"] is not None
    assert connection.execute("SELECT COUNT(*) FROM workflow_jobs WHERE kind = 'publish'").fetchone()[0] == 0
    assert not _exclusive_available(tmp_path)
    session.close()
    assert _exclusive_available(tmp_path)


def test_callback_and_transaction_guards_cannot_be_combined(session):
    workflow, job, _, _ = _claim(session)
    with pytest.raises(ValueError, match="one ownership guard"):
        TranscriptRepository(session.connection, write_guard=lambda: workflow.assert_lease(job),
            write_transaction=lambda: workflow.owned_transaction(job))


def _control_snapshot(connection):
    return tuple(tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid"))
                 for table in ("workflow_jobs", "workflow_attempts"))


def _while_waiting_for_writer(session, now, deadline, operation):
    """Advance a deterministic clock while a real second writer is blocked."""
    ready = Event()
    results = []

    def worker():
        try:
            with ArchiveSession(session.archive_root, mode=ArchiveAccessMode.WRITE) as opened:
                repository = WorkflowRepository(opened.connection)

                def mark_begin(sql):
                    if sql.strip().startswith("BEGIN"):
                        ready.set()

                opened.connection.set_trace_callback(mark_begin)
                results.append(operation(repository))
        except BaseException as error:
            results.append(error)

    session.connection.execute("BEGIN IMMEDIATE")
    thread = Thread(target=worker)
    thread.start()
    try:
        assert ready.wait(10), "second writer never tried to acquire the SQLite lock"
        now[0] = deadline
    finally:
        session.connection.rollback()
        thread.join(10)
    assert not thread.is_alive(), "second writer failed to finish after the SQLite lock was released"
    assert len(results) == 1
    return results[0]


def _terminal_operation(repository, job, outcome, expiry):
    if outcome == "succeeded":
        repository.finish(job.job_id, worker_id=job.lease_owner, expected_attempt_count=job.attempt_count,
                          result={"accepted": True})
    else:
        repository.fail(job.job_id, worker_id=job.lease_owner, expected_attempt_count=job.attempt_count,
                        error_code="temporary_failure", retry_at=expiry + 30 if outcome == "retry" else None)


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "retry"])
@pytest.mark.parametrize("window", ["writer_wait", "attempt_write"])
def test_terminal_exact_expiry_rolls_back_job_and_attempt(session, monkeypatch, outcome, window):
    workflow, job, now, expiry = _claim(session)
    monkeypatch.setattr(workflow_storage, "_now", lambda: now[0])
    before = _control_snapshot(session.connection)
    if window == "writer_wait":
        error = _while_waiting_for_writer(session, now, expiry,
            lambda repository: _terminal_operation(repository, job, outcome, expiry))
        assert isinstance(error, LeaseLostError)
    else:
        def expire_on_attempt(sql):
            if sql.lstrip().startswith("UPDATE workflow_attempts"):
                now[0] = expiry

        session.connection.set_trace_callback(expire_on_attempt)
        try:
            with pytest.raises(LeaseLostError):
                _terminal_operation(workflow, job, outcome, expiry)
        finally:
            session.connection.set_trace_callback(None)
    assert _control_snapshot(session.connection) == before
    assert not session.connection.in_transaction


def test_claim_waiting_for_writer_reclaims_with_a_fresh_deadline(session, monkeypatch):
    _, old_job, now, expiry = _claim(session)
    monkeypatch.setattr(workflow_storage, "_now", lambda: now[0])
    job = _while_waiting_for_writer(session, now, expiry,
        lambda repository: repository.claim("new-worker", lease_seconds=30, kinds=(JobKind.SUBTITLE,)))
    assert job is not None and not isinstance(job, BaseException)
    assert job.job_id == old_job.job_id and job.attempt_count == old_job.attempt_count + 1
    stored = session.connection.execute("SELECT lease_expires_at FROM workflow_jobs WHERE job_id = ?",
                                        (job.job_id,)).fetchone()
    assert stored[0] == expiry + 30
    outcomes = [tuple(row) for row in session.connection.execute(
        "SELECT outcome, error_code FROM workflow_attempts WHERE job_id = ? ORDER BY rowid", (job.job_id,))]
    assert outcomes == [("failed", "lease_expired"), ("running", None)]


def test_claim_expiring_during_attempt_write_rolls_back_reclamation(session, monkeypatch):
    workflow, _, now, expiry = _claim(session)
    now[0] = expiry
    monkeypatch.setattr(workflow_storage, "_now", lambda: now[0])
    before = _control_snapshot(session.connection)

    def expire_on_attempt(sql):
        if sql.lstrip().startswith("INSERT INTO workflow_attempts"):
            now[0] = expiry + 1

    session.connection.set_trace_callback(expire_on_attempt)
    try:
        with pytest.raises(LeaseLostError):
            workflow.claim("new-worker", lease_seconds=1, kinds=(JobKind.SUBTITLE,))
    finally:
        session.connection.set_trace_callback(None)
    assert _control_snapshot(session.connection) == before
    assert not session.connection.in_transaction


@pytest.mark.parametrize("window", ["already_expired", "writer_wait", "previous_deadline", "renewal_write"])
def test_renewal_rejects_exact_expiry_and_rolls_back_new_deadline(session, monkeypatch, window):
    workflow, job, now, expiry = _claim(session)
    monkeypatch.setattr(workflow_storage, "_now", lambda: now[0])
    before = _control_snapshot(session.connection)
    if window == "writer_wait":
        error = _while_waiting_for_writer(session, now, expiry,
            lambda repository: repository.renew_lease(job, lease_seconds=30))
        assert isinstance(error, LeaseLostError)
    else:
        if window == "already_expired":
            now[0] = expiry
        else:
            new_deadline = now[0] + 30

            def expire_on_renewal(sql):
                if sql.lstrip().startswith("UPDATE workflow_jobs"):
                    now[0] = expiry if window == "previous_deadline" else new_deadline

            session.connection.set_trace_callback(expire_on_renewal)
        try:
            with pytest.raises(LeaseLostError):
                workflow.renew_lease(job, lease_seconds=30)
        finally:
            session.connection.set_trace_callback(None)
    assert _control_snapshot(session.connection) == before
    assert not session.connection.in_transaction


@pytest.mark.parametrize("seconds", [0, -1])
def test_renewal_requires_positive_duration_without_writes(session, seconds):
    workflow, job, _, _ = _claim(session)
    before = _control_snapshot(session.connection)
    with pytest.raises(ValueError, match="positive"):
        workflow.renew_lease(job, lease_seconds=seconds)
    assert _control_snapshot(session.connection) == before
    assert not session.connection.in_transaction
