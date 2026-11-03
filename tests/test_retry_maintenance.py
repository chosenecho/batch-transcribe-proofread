"""Retry handoff preserves history and survives interrupted single-host passes."""

import pytest

from bili_asr.services.retry_maintenance import (
    RetryPolicy,
    health,
    maintain_failed_jobs,
    tick,
)
from bili_asr.services.retry_maintenance_io import (
    maintenance_lock,
    read_json,
    update_health,
    write_json,
)
from bili_asr.storage import AsrPolicy, AsrProfile, WorkflowRepository, open_database
from bili_asr.storage.retry_maintenance import failed_candidates, requeue_exact_failure
from bili_asr.workflow_models import JobKind


def policy_value():
    return {"schema_version": 1, "job_ids": [], "part_ids": [1], "kinds": ["audio"],
            "retryable_errors": {"audio": ["TimeoutError"]}, "max_retries": 2,
            "delays_seconds": [0, 600], "interval_seconds": 60}


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "archive"
    connection = open_database(root)
    with connection:
        connection.execute("INSERT INTO bilibili_users(mid,display_name,created_at,updated_at) VALUES(1,'test',1,1)")
        connection.execute("INSERT INTO videos(bvid,aid,mid,title,pubdate,created_at,updated_at) VALUES('BVtest',1,1,'test',1,1,1)")
        connection.execute("INSERT INTO video_parts(video_part_id,bvid,page_index,cid,title,duration_ms,processing_status,created_at,updated_at) "
                           "VALUES(1,'BVtest',0,1,'p0',1000,'metadata_collected',1,1)")
    repository = WorkflowRepository(connection)
    profile = repository.register_profile(AsrProfile(profile_key="test", model_name="test", device="cpu"))
    repository.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=profile)
    job = repository.claim("worker", kinds=[JobKind.AUDIO])
    repository.fail(job.job_id, worker_id="worker", error_code="TimeoutError")
    with connection:
        connection.execute('UPDATE workflow_attempts SET finished_at=1000')
        connection.execute('UPDATE workflow_jobs SET available_at=1000 WHERE status=\'failed\'')
    yield root, connection, repository, job
    connection.close()


def paths(tmp_path):
    return {"policy_path": tmp_path / "policy.json", "state_path": tmp_path / "state.json",
            "lock_path": tmp_path / "maintenance.lock", "health_path": tmp_path / "health.json"}


def seed_files(tmp_path):
    value = paths(tmp_path)
    write_json(value["policy_path"], policy_value())
    write_json(value["state_path"], {"retry_holds": {}, "retry_ledger": {}, "phase": "all_uploader"})
    return value


def test_restart_backoff_budget_and_attempt_evidence(archive, tmp_path):
    root, connection, repository, job = archive
    files = seed_files(tmp_path)
    first = tick(archive_root=root, **files, clock=lambda: 1000)
    assert first["events"][0]["action"] == "requeued"
    assert connection.execute("SELECT outcome,error_code FROM workflow_attempts").fetchone()[:] == ("failed", "TimeoutError")
    next_job = repository.claim("worker", kinds=[job.kind])
    repository.fail(next_job.job_id, worker_id="worker", error_code="TimeoutError")
    with connection:
        connection.execute("UPDATE workflow_attempts SET finished_at=1000 WHERE outcome='failed'")
        connection.execute("UPDATE workflow_jobs SET available_at=1000 WHERE status='failed'")
    assert tick(archive_root=root, **files, clock=lambda: 1599)["events"][0]["reason"] == "backoff"
    assert tick(archive_root=root, **files, clock=lambda: 1600)["events"][0]["retry"] == 2
    third = repository.claim("worker", kinds=[job.kind])
    repository.fail(third.job_id, worker_id="worker", error_code="TimeoutError")
    assert tick(archive_root=root, **files, clock=lambda: 2600)["events"][0]["reason"] == "budget_exhausted"
    assert read_json(files["state_path"])["phase"] == "all_uploader"
    assert connection.execute("SELECT COUNT(*) FROM workflow_attempts").fetchone()[0] == 3


def test_first_retry_delay_anchors_failed_attempt_finished_at(archive, tmp_path):
    root, connection, _repository, _job = archive
    files = seed_files(tmp_path)
    value = policy_value()
    value["delays_seconds"] = [300, 600]
    write_json(files["policy_path"], value)
    with connection:
        connection.execute("UPDATE workflow_attempts SET finished_at=1000")
    assert tick(archive_root=root, **files, clock=lambda: 1299)["events"][0]["reason"] == "backoff"
    assert read_json(files["state_path"])["retry_ledger"] == {}
    assert tick(archive_root=root, **files, clock=lambda: 1300)["events"][0]["action"] == "requeued"


@pytest.mark.parametrize("change,reason", [
    ({"retry_holds": "manual investigation"}, "held"),
    ({"error": "subtitle_body_unavailable"}, "error_not_allowlisted"),
    ({"error": "ValueError"}, "error_not_allowlisted"),
    ({"error": "HTTPStatusError"}, "error_not_allowlisted"),
])
def test_holds_and_quality_contract_failures_never_retry(archive, tmp_path, change, reason):
    root, connection, _repository, job = archive
    files = seed_files(tmp_path)
    state = read_json(files["state_path"])
    if "retry_holds" in change:
        state["retry_holds"][job.job_id] = change["retry_holds"]
    else:
        with connection:
            connection.execute("UPDATE workflow_jobs SET last_error_code=? WHERE job_id=?", (change["error"], job.job_id))
    write_json(files["state_path"], state)
    assert tick(archive_root=root, **files, clock=lambda: 1000)["events"][0]["reason"] == reason
    assert read_json(files["state_path"])["retry_ledger"] == {}


def test_exact_attempt_fence_rejects_changed_attempt_and_cancelled(archive):
    _root, connection, repository, job = archive
    candidate = failed_candidates(connection, job_ids=[job.job_id], part_ids=[], kinds=["audio"])[0]
    repository.requeue_failed(job_ids=[job.job_id])
    newer = repository.claim("worker", kinds=[job.kind])
    repository.fail(newer.job_id, worker_id="worker", error_code="TimeoutError")
    assert not requeue_exact_failure(connection, candidate, 1000, eligible=lambda: True)
    repository.cancel(job_ids=[job.job_id])
    assert not requeue_exact_failure(connection, candidate, 1000, eligible=lambda: True)


def test_crash_after_charge_never_creates_free_retry(archive, tmp_path, monkeypatch):
    root, _connection, repository, job = archive
    files = seed_files(tmp_path)
    def interrupt(*args, **kwargs):
        raise RuntimeError("interrupted")
    monkeypatch.setattr("bili_asr.services.retry_maintenance.requeue_exact_failure", interrupt)
    with pytest.raises(RuntimeError):
        tick(archive_root=root, **files, clock=lambda: 1000)
    assert read_json(files["state_path"])["retry_ledger"][job.job_id]["retries"] == 1
    monkeypatch.undo()
    retry = tick(archive_root=root, **files, clock=lambda: 2000)
    assert retry["events"][0]["reason"] == "reservation_requires_review"
    assert repository.explain_job(job.job_id)["status"] == "failed"


def test_concurrent_pass_lock_skip_does_not_advance_success(archive, tmp_path):
    root, _connection, repository, job = archive
    files = seed_files(tmp_path)
    update_health(files["health_path"], last_succeeded_at=900, status="succeeded", next_due_at=960)
    with maintenance_lock(files["lock_path"]):
        result = tick(archive_root=root, **files, clock=lambda: 1000)
    assert result["status"] == "lock_skipped"
    report = health(health_path=files["health_path"], stale_after_seconds=90, clock=lambda: 1000)
    assert report["maintenance"]["last_succeeded_at"] == 900
    assert report["maintenance"]["lock_skip_count"] == 1
    assert report["maintenance"]["health_status"] == "stale"
    assert repository.explain_job(job.job_id)["status"] == "failed"


def test_missing_state_fails_closed_and_records_failure(archive, tmp_path):
    root, _connection, repository, job = archive
    files = seed_files(tmp_path)
    write_json(files["state_path"], {"retry_ledger": {}})
    with pytest.raises(ValueError):
        tick(archive_root=root, **files, clock=lambda: 1000)
    report = health(health_path=files["health_path"], stale_after_seconds=180, clock=lambda: 1000)
    assert report["maintenance"]["health_status"] == "failed"
    assert report["maintenance"]["last_started_at"] == 1000
    assert report["workers"]["status"] == "not_supplied"
    assert repository.explain_job(job.job_id)["status"] == "failed"


@pytest.mark.parametrize("mutation", [
    {"part_ids": [], "job_ids": []}, {"kinds": []}, {"schema_version": 2},
    {"max_retries": True}, {"retryable_errors": {"audio": ["ValueError"]}},
])
def test_unsafe_or_unknown_policy_is_rejected(mutation):
    value = policy_value()
    value.update(mutation)
    with pytest.raises(ValueError):
        RetryPolicy.load(value)


def test_scope_is_intersection_and_worker_health_independent(archive, tmp_path):
    root, _connection, _repository, _job = archive
    files = seed_files(tmp_path)
    value = policy_value()
    value["job_ids"] = ["different-job"]
    write_json(files["policy_path"], value)
    assert tick(archive_root=root, **files, clock=lambda: 1000)["events"] == []
    worker = tmp_path / "workers.json"
    write_json(worker, {"status": "alive", "observed_at": 2000})
    result = health(health_path=files["health_path"], stale_after_seconds=180,
                    worker_health_path=worker, clock=lambda: 2000)
    assert result["workers"]["status"] == "alive"
    assert result["maintenance"]["health_status"] == "stale"


def test_archive_precheck_failure_records_maintenance_failure(tmp_path):
    files = seed_files(tmp_path)
    with pytest.raises(FileNotFoundError):
        tick(archive_root=tmp_path / "absent", **files, clock=lambda: 1000)
    report = health(health_path=files["health_path"], stale_after_seconds=180, clock=lambda: 1000)
    assert report["maintenance"]["health_status"] == "failed"
    assert report["maintenance"]["last_started_at"] == 1000


def test_path_aliases_fail_before_overwriting_state_or_database(archive, tmp_path):
    root, _connection, _repository, _job = archive
    files = seed_files(tmp_path)
    original = files["state_path"].read_bytes()
    files["health_path"] = files["state_path"]
    with pytest.raises(ValueError):
        tick(archive_root=root, **files)
    assert files["state_path"].read_bytes() == original
    files["health_path"] = root / "archive.db"
    database = files["health_path"].read_bytes()
    with pytest.raises(ValueError):
        tick(archive_root=root, **files)
    assert files["health_path"].read_bytes() == database


def test_cli_tick_and_health_exit_codes(archive, tmp_path, capsys):
    from bili_asr.cli import main
    root, _connection, _repository, _job = archive
    files = seed_files(tmp_path)
    arguments = ["retry-maintenance", "tick", "--archive-root", str(root)]
    for name, path in files.items():
        arguments.extend(["--" + name.removesuffix("_path"), str(path)])
    assert main(arguments) == 0
    assert '"action": "requeued"' in capsys.readouterr().out
    assert main(["retry-maintenance", "health", "--health", str(files["health_path"])]) == 0
    assert '"health_status": "healthy"' in capsys.readouterr().out
    assert main(["retry-maintenance", "health", "--health", str(tmp_path / "missing")]) == 1
    assert '"health_status": "never_succeeded"' in capsys.readouterr().out


def test_editorial_legacy_cross_reference_and_new_budget_gate(tmp_path):
    from bili_asr.editorial import EditorialConfig
    from tests.test_ai_editorial import database as editorial_database
    from tests.test_editorial_regeneration import ReferenceClient, queued

    generator = editorial_database.__wrapped__(tmp_path)
    connection = next(generator)
    try:
        legacy = EditorialConfig.from_snapshot({})
        workflow, _repository, _handlers, executor, _prepared, job_id = queued(
            connection, tmp_path, ReferenceClient(failures=10), legacy)
        assert executor.run().failed == 1
        value = policy_value()
        value.update(job_ids=[job_id], part_ids=[], kinds=["proofread"],
                     retryable_errors={"proofread": ["RevisionValidationError"]})
        state = {"retry_holds": {}, "retry_ledger": {}}
        # Historical reference recovery remains explicit manual work; a
        # deployment allowlist cannot grant automatic revision exceptions.
        with pytest.raises(ValueError, match='unsafe maintenance'):
            RetryPolicy.load(value)
        assert workflow.explain_job(job_id)["attempt_count"] == 1
        workflow.cancel(job_ids=[job_id])

        # New snapshots exhaust physical calls even with a fresh job ledger.
        workflow, _repository, _handlers, executor, _prepared, new_id = queued(
            connection, tmp_path, ReferenceClient(failures=10))
        assert executor.run().failed == 1
        value["job_ids"] = [new_id]
        # Spoofing an allowlisted workflow category cannot bypass persisted budget.
        with connection:
            connection.execute("UPDATE workflow_jobs SET last_error_code='deepseek_transport_failed',available_at=1000 WHERE job_id=?", (new_id,))
            connection.execute("UPDATE workflow_attempts SET error_code='deepseek_transport_failed',finished_at=1000 WHERE job_id=?", (new_id,))
        value['retryable_errors'] = {'proofread': ['deepseek_transport_failed']}
        result = maintain_failed_jobs(connection, RetryPolicy.load(value), state,
                                     state_path=tmp_path / "state.json", now=2000)
        assert result[0]["reason"] == "physical_budget_exhausted"
        assert new_id not in state["retry_ledger"]
    finally:
        try:
            next(generator)
        except StopIteration:
            pass


def test_editorial_transport_retry_has_remaining_physical_budget(tmp_path):
    from tests.test_ai_editorial import database as editorial_database
    from tests.test_editorial_regeneration import queued

    class ConnectError(RuntimeError):
        pass

    class TransportClient:
        def complete(self, request, config):
            raise ConnectError("safe fake transport")

    generator = editorial_database.__wrapped__(tmp_path)
    connection = next(generator)
    try:
        _workflow, repository, _handlers, executor, _prepared, job_id = queued(
            connection, tmp_path, TransportClient())
        assert executor.run().failed == 1
        assert repository.recovery_status_for_job(job_id)["eligible"]
        with connection:
            connection.execute('UPDATE workflow_jobs SET available_at=1000 WHERE job_id=?', (job_id,))
            connection.execute('UPDATE workflow_attempts SET finished_at=1000 WHERE job_id=?', (job_id,))
        value = policy_value()
        value.update(job_ids=[job_id], part_ids=[], kinds=["proofread"],
                     retryable_errors={"proofread": ["ConnectError"]})
        state = {"retry_holds": {}, "retry_ledger": {}}
        result = maintain_failed_jobs(connection, RetryPolicy.load(value), state,
                                     state_path=tmp_path / "state.json", now=1000)
        assert result[0]["action"] == "requeued"
        assert state["retry_ledger"][job_id]["retries"] == 1
    finally:
        try:
            next(generator)
        except StopIteration:
            pass


@pytest.mark.parametrize("failure,expected", [("transport", "requeued"), ("payment", "error_not_allowlisted")])
def test_requests_transport_vs_payment_runtime_maintenance_gate(tmp_path, failure, expected):
    import requests

    from bili_asr.deepseek import DeepSeekClient
    from tests.test_ai_editorial import database as editorial_database
    from tests.test_editorial_regeneration import queued

    class HttpSession:
        def post(self, *args, **kwargs):
            if failure == "transport":
                raise requests.Timeout("secret request URL must not survive")
            response = requests.Response()
            response.status_code = 402
            return response

    generator = editorial_database.__wrapped__(tmp_path)
    connection = next(generator)
    try:
        client = DeepSeekClient(api_key="offline-test", session=HttpSession())
        _workflow, _repository, _handlers, executor, _prepared, job_id = queued(connection, tmp_path, client)
        assert executor.run().failed == 1
        with connection:
            connection.execute('UPDATE workflow_jobs SET available_at=1000 WHERE job_id=?', (job_id,))
            connection.execute('UPDATE workflow_attempts SET finished_at=1000 WHERE job_id=?', (job_id,))
        value = policy_value()
        value.update(job_ids=[job_id], part_ids=[], kinds=["proofread"],
                     retryable_errors={"proofread": ["deepseek_transport_failed"]})
        state = {"retry_holds": {}, "retry_ledger": {}}
        result = maintain_failed_jobs(connection, RetryPolicy.load(value), state,
                                     state_path=tmp_path / "state.json", now=1000)
        assert result[0].get("reason", result[0]["action"]) == expected
        assert bool(state["retry_ledger"]) == (failure == "transport")
    finally:
        try:
            next(generator)
        except StopIteration:
            pass
