"""Cancellation is authoritative across SQLite connections and late results."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from bili_asr import archive, asr, cli
from bili_asr.editorial import EditorialConfig, TEMPLATE_VERSION
from bili_asr.editorial_runtime import EditorialWorkflowHandlers
from bili_asr.sources.models import SubtitleSegment, SubtitleTrack
from bili_asr.storage import AcquisitionRunRecord, TranscriptRepository, TranscriptSegmentRecord, open_database
from bili_asr.storage.editorial import EditorialRepository
from bili_asr.storage.workflow import AsrPolicy, AsrProfile, JobCancelledError, JobKind, WorkflowRepository
from bili_asr.workflow import WorkflowExecutor
from bili_asr.workflow_runtime import ArchiveWorkflowHandlers
from tests.test_ai_editorial import FakeClient, envelope, insert_record, no_changes, record
from tests.test_workflow_control_plane import _seed_part


@pytest.fixture
def database(tmp_path):
    connection = open_database(tmp_path)
    _seed_part(connection)
    try:
        yield connection
    finally:
        connection.close()


def _other_connection(root):
    connection = sqlite3.connect(root / "archive.db", timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _plan(connection, *, editorial=False, device="cpu"):
    repository = WorkflowRepository(connection)
    profile = repository.register_profile(AsrProfile("cancel-test", "offline-model", device=device))
    repository.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=profile,
                    editorial_config=EditorialConfig().to_dict() if editorial else None)
    return repository


def _job_row(connection, job_id):
    return connection.execute("SELECT * FROM workflow_jobs WHERE job_id = ?", (job_id,)).fetchone()


def _cancel_from_other_connection(root, job_id):
    connection = _other_connection(root)
    try:
        result = WorkflowRepository(connection).cancel(job_ids=[job_id])
        assert len(result) == 1 and result[0].changed
        return result
    finally:
        connection.close()


def _assert_cancelled(connection, job_id, *, attempts):
    row = _job_row(connection, job_id)
    assert row["status"] == "cancelled"
    assert row["lease_owner"] is None and row["lease_expires_at"] is None
    rows = connection.execute("SELECT * FROM workflow_attempts WHERE job_id = ? ORDER BY rowid", (job_id,)).fetchall()
    assert len(rows) == attempts
    if attempts:
        assert rows[-1]["outcome"] == "cancelled"
        assert rows[-1]["finished_at"] is not None
        assert rows[-1]["error_code"] == "cancelled"
        assert rows[-1]["result_json"] is None


def test_queued_cancellation_has_no_fake_attempt_and_reports_transitive_blocked_jobs(database):
    repository = _plan(database, editorial=True)
    audio_id = database.execute("SELECT job_id FROM workflow_jobs WHERE kind = 'audio'").fetchone()[0]
    result = repository.cancel(job_ids=[audio_id, audio_id])
    assert len(result) == 1
    assert (result[0].previous_status, result[0].status, result[0].changed) == ("queued", "cancelled", True)
    _assert_cancelled(database, audio_id, attempts=0)
    assert _job_row(database, audio_id)["attempt_count"] == 0
    blocked = repository.blocked_by_cancelled()
    assert {row[0] for row in database.execute(
        "SELECT kind FROM workflow_jobs WHERE job_id IN (" + ",".join("?" for _ in blocked) + ")", tuple(blocked)
    )} == {"asr", "proofread", "render_document"}
    assert repository.count_by_status() == {"queued": 4, "cancelled": 1}
    assert repository.claim("blocked", kinds=(JobKind.AUDIO, JobKind.ASR, JobKind.PROOFREAD, JobKind.RENDER_DOCUMENT)) is None


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
def test_terminal_cancellation_is_an_explicit_noop_preserving_history(database, terminal):
    repository = _plan(database)
    job = repository.claim("worker", kinds=(JobKind.SUBTITLE,))
    if terminal == "succeeded":
        repository.finish(job.job_id, worker_id="worker", expected_attempt_count=job.attempt_count,
                          result={"before": "cancellation"})
    elif terminal == "failed":
        repository.fail(job.job_id, worker_id="worker", expected_attempt_count=job.attempt_count, error_code="upstream_error")
    else:
        repository.cancel(job_ids=[job.job_id])
    before_job = tuple(_job_row(database, job.job_id))
    before_attempts = [tuple(row) for row in database.execute("SELECT * FROM workflow_attempts")]
    result = repository.cancel(job_ids=[job.job_id])
    assert (result[0].previous_status, result[0].status, result[0].changed) == (terminal, terminal, False)
    assert tuple(_job_row(database, job.job_id)) == before_job
    assert [tuple(row) for row in database.execute("SELECT * FROM workflow_attempts")] == before_attempts


def test_unknown_selected_id_rolls_back_entire_cancellation(database):
    repository = _plan(database)
    job = repository.claim("worker", kinds=(JobKind.SUBTITLE,))
    before = tuple(_job_row(database, job.job_id))
    with pytest.raises(ValueError, match="unknown.*missing"):
        repository.cancel(job_ids=[job.job_id, "missing"])
    assert tuple(_job_row(database, job.job_id)) == before
    assert database.execute("SELECT outcome FROM workflow_attempts").fetchone()[0] == "running"
    assert not database.in_transaction


def test_cancelled_running_attempt_rejects_finish_failure_renew_and_reclaim(database, tmp_path):
    repository = _plan(database)
    job = repository.claim("same-worker", kinds=(JobKind.SUBTITLE,))
    _cancel_from_other_connection(tmp_path, job.job_id)
    _assert_cancelled(database, job.job_id, attempts=1)
    assert repository.is_cancelled(job)
    actions = [lambda: repository.assert_lease(job),
               lambda: repository.renew_lease(job, lease_seconds=100),
               lambda: repository.finish(job.job_id, worker_id="same-worker", expected_attempt_count=job.attempt_count),
               lambda: repository.fail(job.job_id, worker_id="same-worker", error_code="late_failure",
                                       expected_attempt_count=job.attempt_count)]
    for action in actions:
        with pytest.raises(JobCancelledError):
            action()
    assert repository.claim("new-worker", kinds=(JobKind.SUBTITLE,)) is None
    _assert_cancelled(database, job.job_id, attempts=1)


def test_cancel_current_attempt_with_reused_worker_id_preserves_expired_history(database):
    repository = _plan(database)
    old = repository.claim("same-worker", kinds=(JobKind.SUBTITLE,))
    with database:
        database.execute("UPDATE workflow_jobs SET lease_expires_at = 0 WHERE job_id = ?", (old.job_id,))
    current = repository.claim("same-worker", kinds=(JobKind.SUBTITLE,))
    assert current.attempt_count == old.attempt_count + 1
    repository.cancel(job_ids=[current.job_id])
    outcomes = [tuple(row) for row in database.execute(
        "SELECT outcome, error_code FROM workflow_attempts WHERE job_id = ? ORDER BY rowid", (old.job_id,)
    )]
    assert outcomes == [("failed", "lease_expired"), ("cancelled", "cancelled")]
    for job in (old, current):
        with pytest.raises(JobCancelledError):
            repository.finish(job.job_id, worker_id="same-worker", expected_attempt_count=job.attempt_count)


def test_retry_plan_and_changed_publication_request_do_not_resurrect_cancelled_jobs(database):
    repository = _plan(database)
    publication_id, _ = repository.request_publication(video_part_id=1, transcript_id=1)
    ids = [row[0] for row in database.execute("SELECT job_id FROM workflow_jobs")]
    repository.cancel(job_ids=ids)
    before = [tuple(row) for row in database.execute("SELECT * FROM workflow_jobs ORDER BY job_id")]
    assert repository.requeue_failed() == 0
    profile = database.execute("SELECT profile_id FROM workflow_asr_profiles").fetchone()[0]
    plan = repository.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=profile)
    assert plan.subtitle_jobs == plan.audio_jobs == plan.asr_jobs == 0
    assert repository.request_publication(video_part_id=1, transcript_id=2) == (publication_id, False)
    assert [tuple(row) for row in database.execute("SELECT * FROM workflow_jobs ORDER BY job_id")] == before
    assert repository.claim("later") is None


def test_completed_attempt_commits_before_cancellation_and_returns_noop_on_other_connection(database, tmp_path):
    repository = _plan(database)
    job = repository.claim("worker", kinds=(JobKind.SUBTITLE,))
    repository.finish(job.job_id, worker_id="worker", expected_attempt_count=job.attempt_count, result={"accepted": True})
    other = _other_connection(tmp_path)
    try:
        result = WorkflowRepository(other).cancel(job_ids=[job.job_id])
        assert result[0].status == "succeeded" and not result[0].changed
    finally:
        other.close()
    row = database.execute("SELECT outcome, result_json FROM workflow_attempts WHERE job_id = ?", (job.job_id,)).fetchone()
    assert row["outcome"] == "succeeded" and json.loads(row["result_json"]) == {"accepted": True}


def test_owned_transaction_serializes_other_connection_cancellation_and_retains_prior_commit(database, tmp_path):
    repository = _plan(database)
    job = repository.claim("worker", kinds=(JobKind.SUBTITLE,))
    cancel_attempted = threading.Event()
    cancel_finished = threading.Event()
    failures = []

    def cancel():
        other = _other_connection(tmp_path)
        other.set_trace_callback(lambda sql: cancel_attempted.set() if sql == "BEGIN IMMEDIATE" else None)
        try:
            WorkflowRepository(other).cancel(job_ids=[job.job_id])
        except BaseException as exc:
            failures.append(exc)
        finally:
            other.close()
            cancel_finished.set()

    with repository.owned_transaction(job):
        database.execute("UPDATE videos SET title = 'committed before accepted cancellation' WHERE bvid = 'BVtest'")
        thread = threading.Thread(target=cancel, daemon=True)
        thread.start()
        assert cancel_attempted.wait(5), "canceller never reached the competing SQLite transaction"
        assert not cancel_finished.is_set()
        assert _job_row(database, job.job_id)["status"] == "running"
    thread.join(10)
    assert not thread.is_alive() and not failures
    assert cancel_finished.is_set()
    assert database.execute("SELECT title FROM videos").fetchone()[0] == "committed before accepted cancellation"
    _assert_cancelled(database, job.job_id, attempts=1)


def test_executor_counts_late_cancellation_and_limit_includes_it(database, tmp_path):
    repository = _plan(database)

    def late_result(job):
        _cancel_from_other_connection(tmp_path, job.job_id)
        return {"late": "result"}

    summary = WorkflowExecutor(repository, worker_id="executor", kinds=(JobKind.SUBTITLE, JobKind.AUDIO),
                               handlers={JobKind.SUBTITLE: late_result, JobKind.AUDIO: late_result}).run(limit=1)
    assert (summary.succeeded, summary.failed, summary.cancelled, summary.idle) == (0, 0, 1, False)
    assert repository.count_by_status() == {"queued": 2, "cancelled": 1}
    assert database.execute("SELECT COUNT(*) FROM workflow_attempts").fetchone()[0] == 1


@pytest.mark.parametrize("late_failure", [True, False])
def test_executor_counts_cancellation_after_attempt_reclamation(database, tmp_path, late_failure, monkeypatch):
    repository = _plan(database)
    # Explicitly advance the lease instead of racing the heartbeat thread.
    monkeypatch.setattr("bili_asr.workflow._LeaseHeartbeat.start", lambda self: None)

    def reclaimed_result(job):
        with database:
            database.execute("UPDATE workflow_jobs SET lease_expires_at = 0 WHERE job_id = ?", (job.job_id,))
        replacement = repository.claim("replacement", kinds=(JobKind.SUBTITLE,))
        assert replacement.job_id == job.job_id
        assert replacement.attempt_count == job.attempt_count + 1
        _cancel_from_other_connection(tmp_path, job.job_id)
        if late_failure:
            raise RuntimeError("late failure after cancellation")
        return {"late": "result"}

    summary = WorkflowExecutor(repository, worker_id="stale", kinds=(JobKind.SUBTITLE,),
                               handlers={JobKind.SUBTITLE: reclaimed_result}).run(limit=1)
    assert (summary.succeeded, summary.failed, summary.cancelled) == (0, 0, 1)
    cancelled_job = next(job for job in repository.list_jobs() if job.status == "cancelled")
    _assert_cancelled(database, cancelled_job.job_id, attempts=2)


def test_old_attempt_schema_is_rejected_before_cancellation_mutation(database):
    repository = _plan(database)
    job = repository.claim("worker", kinds=(JobKind.SUBTITLE,))
    # Reproduce the historical CHECK contract without touching the shipped schema.
    script = database.execute("SELECT sql FROM sqlite_master WHERE name = 'workflow_attempts'").fetchone()[0]
    with database:
        database.execute("DELETE FROM workflow_attempts")
        database.execute("DROP TABLE workflow_attempts")
        database.execute(script.replace(", 'cancelled'", ""))
    before = tuple(_job_row(database, job.job_id))
    with pytest.raises(ValueError, match="predates cancellation.*rebuilt archive database"):
        repository.cancel(job_ids=[job.job_id])
    assert tuple(_job_row(database, job.job_id)) == before


def _start_run(repository, run_id):
    repository.start_acquisition_run(AcquisitionRunRecord(
        run_id=run_id, kind="subtitle", selector_kind="bvid", selector_target="BVtest",
        requested_limit=1, credential_present=False, started_at=1,
    ))


def test_cancel_after_earlier_lease_check_is_rechecked_inside_transcript_write_transaction(database, tmp_path):
    workflow = _plan(database)
    job = workflow.claim("worker", kinds=(JobKind.SUBTITLE,))
    repository = TranscriptRepository(database, write_guard=lambda: workflow.assert_lease(job))
    _start_run(repository, "cancelled-caption")
    workflow.assert_lease(job)
    _cancel_from_other_connection(tmp_path, job.job_id)
    with pytest.raises(JobCancelledError):
        repository.record_acquired_transcript(
            run_id="cancelled-caption", video_part_id=1, source_kind="subtitle-ai", language="zh-CN",
            segments=(TranscriptSegmentRecord(0, 900, "late caption"),), started_at=1, finished_at=2, created_at=2,
        )
    assert database.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM transcript_segments").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM acquisition_attempts").fetchone()[0] == 0
    assert not database.in_transaction


def test_cancel_before_publish_guard_prevents_all_final_bundle_files(database, tmp_path):
    workflow = _plan(database)
    job = workflow.claim("worker", kinds=(JobKind.SUBTITLE,))
    workflow.assert_lease(job)
    _cancel_from_other_connection(tmp_path, job.job_id)
    with pytest.raises(JobCancelledError):
        archive.write_archive(tmp_path, {"bvid": "BVtest", "page_index": 0, "cid": 1, "title": "test", "work_id": "BVtest:p0"},
                              [{"start": 0, "end": 0.9, "text": "late caption"}], source="subtitle",
                              publication_guard=lambda invalidate: workflow.owned_transaction(job, on_rollback=invalidate))
    assert not list((tmp_path / "transcripts").rglob("bundle.*"))
    assert not list((tmp_path / "transcripts").rglob("*.tmp"))


def test_subtitle_api_return_after_cancellation_cannot_store_or_request_publication(database, tmp_path, monkeypatch):
    from bili_asr import workflow_runtime

    workflow = _plan(database)

    class Gateway:
        async def get_subtitle_tracks(self, _bvid, _cid):
            return (SubtitleTrack("ai-zh", "中文", True, "track"),)

        async def fetch_subtitle_segments(self, _track, _bvid, _cid):
            job_id = database.execute("SELECT job_id FROM workflow_jobs WHERE kind = 'subtitle'").fetchone()[0]
            _cancel_from_other_connection(tmp_path, job_id)
            return (SubtitleSegment(0, 900, "response arrived too late"),)

    monkeypatch.setattr(workflow_runtime, "BilibiliApiGateway", lambda **_kwargs: Gateway())
    handlers = ArchiveWorkflowHandlers(database, workflow, archive_root=tmp_path, sessdata=None)
    try:
        summary = WorkflowExecutor(workflow, worker_id="caption-worker", handlers=handlers.handlers(),
                                   kinds=(JobKind.SUBTITLE,)).run()
    finally:
        handlers.close()
    assert (summary.succeeded, summary.failed, summary.cancelled) == (0, 0, 1)
    assert database.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM workflow_jobs WHERE kind = 'publish'").fetchone()[0] == 0
    runs = database.execute("SELECT outcome, finished_at FROM acquisition_runs").fetchall()
    assert len(runs) == 1 and runs[0]["outcome"] == "failed" and runs[0]["finished_at"] is not None
    assert not list((tmp_path / "transcripts").rglob("bundle.*"))


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_asr_model_return_after_cancellation_cannot_store_or_request_publication(database, tmp_path, monkeypatch, device):
    workflow = _plan(database, device=device)
    audio_file = tmp_path / "audio" / "offline.m4a"
    audio_file.parent.mkdir()
    audio_file.write_bytes(b"offline test media")
    audio_job = workflow.claim("audio-worker", kinds=(JobKind.AUDIO,))
    workflow.finish(audio_job.job_id, worker_id="audio-worker", expected_attempt_count=audio_job.attempt_count,
                    result={"storage_key": "audio/offline.m4a"})
    asr_id = database.execute("SELECT job_id FROM workflow_jobs WHERE kind = 'asr'").fetchone()[0]

    def late_inference(*_args, **_kwargs):
        _cancel_from_other_connection(tmp_path, asr_id)
        segments = [{"start": 0, "end": 0.9, "text": "late local ASR"}]
        return (segments, {"language": "Chinese"}, None) if device == "cuda" else segments

    class OfflineRunner:
        def provenance(self):
            return {"language": "Chinese"}

    monkeypatch.setattr(asr, "two_pass_transcribe", late_inference)
    monkeypatch.setattr(asr, "transcribe_with_timeout", late_inference)
    handlers = ArchiveWorkflowHandlers(database, workflow, archive_root=tmp_path, sessdata=None)
    monkeypatch.setattr(handlers, "_runner", lambda _profile: OfflineRunner())
    try:
        summary = WorkflowExecutor(workflow, worker_id="asr-worker", handlers=handlers.handlers(),
                                   kinds=(JobKind.ASR,)).run()
    finally:
        handlers.close()
    assert (summary.succeeded, summary.failed, summary.cancelled) == (0, 0, 1)
    _assert_cancelled(database, asr_id, attempts=1)
    assert database.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM asr_models").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM workflow_jobs WHERE kind = 'publish'").fetchone()[0] == 0
    run = database.execute("SELECT outcome, finished_at FROM acquisition_runs").fetchone()
    assert run["outcome"] == "failed" and run["finished_at"] is not None


def test_download_return_after_cancellation_cannot_publish_audio_or_store_objects(database, tmp_path, monkeypatch):
    from bili_asr import audio

    workflow = _plan(database)
    audio_id = database.execute("SELECT job_id FROM workflow_jobs WHERE kind = 'audio'").fetchone()[0]

    def late_download(_client, _identity, staged_target, *, artifact_roots):
        assert staged_target.parent == artifact_roots.write_base / "audio"
        assert artifact_roots.write_base.is_relative_to(tmp_path / "audio")
        _cancel_from_other_connection(tmp_path, audio_id)
        staged_target.write_bytes(b"late downloaded audio")
        return staged_target

    monkeypatch.setattr(audio, "download_audio", late_download)
    handlers = ArchiveWorkflowHandlers(database, workflow, archive_root=tmp_path, sessdata=None)
    try:
        summary = WorkflowExecutor(workflow, worker_id="audio-worker", handlers=handlers.handlers(),
                                   kinds=(JobKind.AUDIO,)).run()
    finally:
        handlers.close()
    assert (summary.succeeded, summary.failed, summary.cancelled) == (0, 0, 1)
    assert database.execute("SELECT COUNT(*) FROM audio_objects").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM part_audio_objects").fetchone()[0] == 0
    assert not list((tmp_path / "audio").rglob("*.m4a"))
    assert not list((tmp_path / "audio").glob(".workflow-audio-*"))


@pytest.mark.parametrize("heartbeat_observes", [False, True])
def test_proofread_api_return_after_cancellation_keeps_diagnostic_but_no_accepted_revision(
    database, tmp_path, monkeypatch, heartbeat_observes,
):
    insert_record(database, record())
    workflow = WorkflowRepository(database)
    repository = EditorialRepository(database)
    prepared = repository.prepare(1, None, EditorialConfig())
    proofread_id, render_id, _, _ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    cancellation_observed = threading.Event()
    renew = WorkflowRepository.renew_lease

    def observe_cancellation(repository, job, *, lease_seconds):
        try:
            return renew(repository, job, lease_seconds=lease_seconds)
        except JobCancelledError:
            cancellation_observed.set()
            raise

    monkeypatch.setattr(WorkflowRepository, "renew_lease", observe_cancellation)

    class LateClient(FakeClient):
        def complete(self, request, config):
            _cancel_from_other_connection(tmp_path, proofread_id)
            if heartbeat_observes:
                assert cancellation_observed.wait(5), "heartbeat never observed committed cancellation"
            return envelope(no_changes(json.loads(request["messages"][1]["content"])))

    handlers = EditorialWorkflowHandlers(repository, workflow, archive_root=tmp_path, client=LateClient())
    try:
        summary = WorkflowExecutor(workflow, worker_id="proofreader", handlers=handlers.handlers(),
                                   kinds=(JobKind.PROOFREAD, JobKind.RENDER_DOCUMENT),
                                   heartbeat_interval_seconds=0.01).run()
    finally:
        handlers.close()
    assert (summary.succeeded, summary.failed, summary.cancelled) == (0, 0, 1)
    _assert_cancelled(database, proofread_id, attempts=1)
    assert database.execute("SELECT COUNT(*) FROM editorial_chunk_results").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM editorial_revisions").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM document_artifacts").fetchone()[0] == 0
    call = database.execute("SELECT * FROM editorial_model_calls").fetchone()
    assert json.loads(call["response_json"])["id"] == "offline-call"
    assert call["error_code"] == "JobCancelledError" and call["finished_at"] is not None
    assert workflow.blocked_by_cancelled() == {render_id}
    assert not (tmp_path / "documents").exists()


def test_render_cancel_after_preflight_is_rechecked_before_any_final_write(database, tmp_path, monkeypatch):
    insert_record(database, record())
    workflow = WorkflowRepository(database)
    repository = EditorialRepository(database)
    prepared = repository.prepare(1, None, EditorialConfig())
    workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    handlers = EditorialWorkflowHandlers(repository, workflow, archive_root=tmp_path, client=FakeClient())
    try:
        summary = WorkflowExecutor(workflow, worker_id="proofreader", handlers=handlers.handlers(),
                                   kinds=(JobKind.PROOFREAD,)).run()
        assert summary.succeeded == 1
        job = workflow.claim("renderer", kinds=(JobKind.RENDER_DOCUMENT,))
        owned = repository.owned_transaction

        @contextmanager
        def cancel_before_guard(selected):
            # The initial lease check and pair integrity preflight have passed;
            # cancellation must be rechecked before installing either file.
            assert not list((tmp_path / "documents").rglob("*.md"))
            _cancel_from_other_connection(tmp_path, selected.job_id)
            with owned(selected):
                yield

        monkeypatch.setattr(repository, "owned_transaction", cancel_before_guard)
        with pytest.raises(JobCancelledError):
            handlers.render(job)
    finally:
        handlers.close()
    _assert_cancelled(database, job.job_id, attempts=1)
    assert database.execute("SELECT COUNT(*) FROM document_artifacts").fetchone()[0] == 0
    assert not list((tmp_path / "documents").rglob("*.md"))
    assert not list((tmp_path / "documents").rglob(".render-*"))


def test_cancelled_explicit_rerender_request_stays_cancelled(database):
    insert_record(database, record())
    workflow = WorkflowRepository(database)
    repository = EditorialRepository(database)
    prepared = repository.prepare(1, None, EditorialConfig())
    proof_id, _, _, _ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    handlers = EditorialWorkflowHandlers(repository, workflow, archive_root=Path("unused"), client=FakeClient())
    job = workflow.claim("proofreader", kinds=(JobKind.PROOFREAD,))
    result = handlers.proofread(job)
    workflow.finish(job.job_id, worker_id="proofreader", expected_attempt_count=job.attempt_count, result=result)
    revision_id = repository.revision_for_job(proof_id)
    render_id, created = workflow.request_document(video_part_id=1, revision_id=revision_id, template_version=TEMPLATE_VERSION)
    assert created
    workflow.cancel(job_ids=[render_id])
    assert workflow.request_document(video_part_id=1, revision_id=revision_id, template_version=TEMPLATE_VERSION) == (render_id, False)
    _assert_cancelled(database, render_id, attempts=0)


def test_cancel_cli_reports_changed_noop_job_id_and_status_blocked_identity(database, tmp_path, capsys):
    repository = _plan(database)
    audio_id = database.execute("SELECT job_id FROM workflow_jobs WHERE kind = 'audio'").fetchone()[0]
    args = ["workflow", "cancel", "--archive-root", str(tmp_path), "--job-id", audio_id]
    assert cli.main(args) == 0
    output = capsys.readouterr().out
    assert "changed=1 noop=0" in output
    assert f"job_id={audio_id} previous=queued status=cancelled changed=1" in output
    assert cli.main(args) == 0
    assert "changed=0 noop=1" in capsys.readouterr().out
    assert cli.main(["workflow", "status", "--archive-root", str(tmp_path), "--jobs"]) == 0
    output = capsys.readouterr().out
    assert "cancelled: 1" in output and "blocked_by_cancelled: 1" in output
    assert f"job_id={audio_id} kind=audio status=cancelled bvid=BVtest page_index=0" in output
    assert repository.blocked_by_cancelled()


def test_cancel_cli_unknown_selection_is_atomic_and_legacy_schema_error_is_readable(database, tmp_path, capsys):
    repository = _plan(database)
    job = repository.claim("worker", kinds=(JobKind.SUBTITLE,))
    assert cli.main(["workflow", "cancel", "--archive-root", str(tmp_path), "--job-id", job.job_id,
                     "--job-id", "missing"]) == 1
    output = capsys.readouterr()
    assert "unknown workflow job: missing" in output.err and not output.out
    assert _job_row(database, job.job_id)["status"] == "running"
    script = database.execute("SELECT sql FROM sqlite_master WHERE name = 'workflow_attempts'").fetchone()[0]
    with database:
        database.execute("DELETE FROM workflow_attempts")
        database.execute("DROP TABLE workflow_attempts")
        database.execute(script.replace(", 'cancelled'", ""))
    assert cli.main(["workflow", "cancel", "--archive-root", str(tmp_path), "--job-id", job.job_id]) == 1
    output = capsys.readouterr()
    assert "incompatible archive database" in output.err and "workflow_attempts" in output.err
    assert "preserve archive.db and its artifacts" in output.err
    assert not output.out and _job_row(database, job.job_id)["status"] == "running"


def test_publish_handler_cancellation_after_staging_prevents_bundle_and_database_publication(database, tmp_path, monkeypatch):
    insert_record(database, record(source="subtitle-cc"))
    workflow = WorkflowRepository(database)
    publication_id, _ = workflow.request_publication(video_part_id=1, transcript_id=1)
    job = workflow.claim("publisher", kinds=(JobKind.PUBLISH,))
    assert job.job_id == publication_id
    owned = workflow.owned_transaction

    @contextmanager
    def cancel_after_staging(selected, *, on_rollback=None):
        # write_archive has prepared the complete bundle; cancellation is
        # accepted after the handler's early lease check and before the guard.
        assert (tmp_path / "transcripts").exists()
        _cancel_from_other_connection(tmp_path, selected.job_id)
        with owned(selected, on_rollback=on_rollback):
            yield

    monkeypatch.setattr(workflow, "owned_transaction", cancel_after_staging)
    handlers = ArchiveWorkflowHandlers(database, workflow, archive_root=tmp_path, sessdata=None)
    try:
        with pytest.raises(JobCancelledError):
            handlers.publish(job)
    finally:
        handlers.close()
    _assert_cancelled(database, job.job_id, attempts=1)
    assert database.execute("SELECT COUNT(*) FROM workflow_publications").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 1
    assert not list((tmp_path / "transcripts").rglob("bundle.*"))
    assert not list((tmp_path / "transcripts").rglob(archive.BUNDLE_MARKER_NAME))
    assert not list((tmp_path / "transcripts").rglob("*.tmp"))


def test_file_replacement_guard_blocks_cancellation_until_complete_bundle_commit(database, tmp_path):
    workflow = _plan(database)
    job = workflow.claim("publisher", kinds=(JobKind.SUBTITLE,))
    cancel_attempted = threading.Event()
    cancel_finished = threading.Event()
    failures = []
    thread = None

    def cancel():
        other = _other_connection(tmp_path)
        other.set_trace_callback(lambda sql: cancel_attempted.set() if sql == "BEGIN IMMEDIATE" else None)
        try:
            WorkflowRepository(other).cancel(job_ids=[job.job_id])
        except BaseException as exc:
            failures.append(exc)
        finally:
            other.close()
            cancel_finished.set()

    def before_replace():
        nonlocal thread
        workflow.assert_lease(job)
        if thread is None:
            thread = threading.Thread(target=cancel, daemon=True)
            thread.start()
            assert cancel_attempted.wait(5), "canceller never reached SQLite BEGIN IMMEDIATE"
        # Every final replacement and the ready marker happen before the
        # cancellation transaction can finish. There is no timing sleep.
        assert not cancel_finished.is_set()

    paths = archive.write_archive(
        tmp_path, {"bvid": "BVtest", "page_index": 0, "cid": 1, "title": "test", "work_id": "BVtest:p0"},
        [{"start": 0, "end": 0.9, "text": "committed before cancellation"}], source="subtitle",
        before_replace=before_replace,
        publication_guard=lambda invalidate: workflow.owned_transaction(job, on_rollback=invalidate),
    )
    assert thread is not None
    thread.join(10)
    assert not thread.is_alive() and not failures
    assert cancel_finished.is_set()
    for path in paths.values():
        assert (tmp_path / path).is_file()
    assert (tmp_path / paths["srt_path"]).parent.joinpath(archive.BUNDLE_MARKER_NAME).is_file()
    _assert_cancelled(database, job.job_id, attempts=1)
    with pytest.raises(JobCancelledError):
        workflow.finish(job.job_id, worker_id="publisher", expected_attempt_count=job.attempt_count)
