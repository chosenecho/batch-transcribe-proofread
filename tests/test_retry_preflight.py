"""A retry handoff preview preserves failed evidence and operational files."""

import json
import os

import pytest

from bili_asr.services.retry_maintenance import tick
from bili_asr.services.retry_maintenance_io import read_json, write_json
from bili_asr.services.retry_preflight import preflight
from tests.test_retry_maintenance import (
    archive as archive,  # noqa: PLC0414 - registers the shared fixture
)
from tests.test_retry_maintenance import policy_value, seed_files


def test_preflight_does_not_dispatch_or_change_operational_files(archive, tmp_path):
    root, connection, repository, job = archive
    files = seed_files(tmp_path)
    before = {key: path.read_bytes() for key, path in files.items() if path.exists()}
    database_before = (root / "archive.db").read_bytes()
    attempts = connection.execute("SELECT * FROM workflow_attempts").fetchall()
    result = preflight(archive_root=root, **files, clock=lambda: 1000)
    assert result["status"] == "preflight_passed"
    assert result["read_only"] and not result["handoff_verified"]
    assert result["events"][0]["action"] == "would_requeue"
    assert repository.explain_job(job.job_id)["status"] == "failed"
    assert connection.execute("SELECT * FROM workflow_attempts").fetchall() == attempts
    assert (root / "archive.db").read_bytes() == database_before
    assert {key: files[key].read_bytes() for key in before} == before
    assert not files["lock_path"].exists()
    assert not files["health_path"].exists()
    assert not files["health_path"].with_suffix(".json.lock").exists()
    # A later explicit tick makes the dispatch, not the read-only preview.
    assert tick(archive_root=root, **files, clock=lambda: 1000)["events"][0]["action"] == "requeued"


@pytest.mark.parametrize("case,expected", [
    ("held", "held"), ("payment", "error_not_allowlisted"),
    ("exhausted", "budget_exhausted"), ("backoff", "backoff"),
    ("reserved", "reservation_requires_review"), ("mismatch", "attempt_mismatch"),
])
def test_preview_agrees_with_tick_guards(archive, tmp_path, case, expected):
    root, connection, repository, job = archive
    files = seed_files(tmp_path)
    state = read_json(files["state_path"])
    if case == "held":
        state["retry_holds"][job.job_id] = {"reason": "private investigation evidence"}
    elif case == "payment":
        with connection:
            connection.execute("UPDATE workflow_jobs SET last_error_code='HTTPStatusError'")
    elif case == "mismatch":
        with connection:
            connection.execute("UPDATE workflow_attempts SET error_code='other'")
    elif case == "exhausted":
        state["retry_ledger"][job.job_id] = {"retries": 2, "last_retry_at": 1}
    elif case == "backoff":
        state["retry_ledger"][job.job_id] = {"retries": 1, "last_retry_at": 999}
    elif case == "reserved":
        attempt_id = connection.execute("SELECT attempt_id FROM workflow_attempts").fetchone()[0]
        state["retry_ledger"][job.job_id] = {"retries": 1, "last_retry_at": 1,
                                              "reserved_attempt_id": attempt_id}
    write_json(files["state_path"], state)
    before = files["state_path"].read_bytes()
    preview = preflight(archive_root=root, **files, clock=lambda: 1000)
    assert preview["events"][0]["reason"] == expected
    assert "private investigation" not in json.dumps(preview)
    assert files["state_path"].read_bytes() == before
    dispatched = tick(archive_root=root, **files, clock=lambda: 1000)
    assert dispatched["events"][0]["reason"] == expected
    assert repository.explain_job(job.job_id)["status"] == "failed"


def test_unknown_selectors_fail_scope_even_when_no_failed_candidates(archive, tmp_path):
    root, _connection, _repository, _job = archive
    files = seed_files(tmp_path)
    policy = policy_value()
    policy.update(job_ids=["missing-job"], part_ids=[123456])
    write_json(files["policy_path"], policy)
    report = preflight(archive_root=root, **files)
    assert report["status"] == "invalid_scope"
    assert report["missing_job_ids"] == ["missing-job"]
    assert report["missing_part_ids"] == [123456]
    assert report["events"] == []


@pytest.mark.parametrize("alias", ["state", "database", "health_lock", "archive_lock"])
def test_preflight_rejects_file_aliases_before_access(archive, tmp_path, alias):
    root, _connection, _repository, _job = archive
    files = seed_files(tmp_path)
    original = files["state_path"].read_bytes()
    if alias == "state":
        os.link(files["state_path"], files["health_path"])
    elif alias == "database":
        files["health_path"] = root / "archive.db"
    elif alias == "archive_lock":
        files["health_path"] = root.parent / f".{root.name}.archive-maintenance.lock"
    else:
        files["state_path"] = files["health_path"].with_suffix(".json.lock")
    with pytest.raises(ValueError, match="must differ"):
        preflight(archive_root=root, **files)
    with pytest.raises(ValueError, match="must differ"):
        tick(archive_root=root, **files)
    assert (tmp_path / "state.json").read_bytes() == original
    assert not files["lock_path"].exists()


def test_preflight_and_tick_refuse_linked_operational_state(archive, tmp_path):
    root, _connection, _repository, _job = archive
    files = seed_files(tmp_path)
    original = files["state_path"].read_bytes()
    linked_state = tmp_path / "linked-state.json"
    try:
        linked_state.symlink_to(files["state_path"])
    except OSError:
        pytest.skip("this environment does not permit symlink creation")
    files["state_path"] = linked_state
    for action in (preflight, tick):
        with pytest.raises(ValueError, match="cannot use links"):
            action(archive_root=root, **files)
    assert linked_state.is_symlink()
    assert (tmp_path / "state.json").read_bytes() == original
    assert not files["health_path"].exists()
    assert not files["lock_path"].exists()


def test_cli_preflight_invalid_state_and_missing_archive_are_fail_closed(archive, tmp_path, capsys):
    from bili_asr.cli import main

    root, _connection, _repository, _job = archive
    files = seed_files(tmp_path)
    arguments = ["retry-maintenance", "preflight", "--archive-root", str(root)]
    for name, path in files.items():
        arguments.extend(["--" + name.removesuffix("_path"), str(path)])
    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["events"][0]["action"] == "would_requeue"
    write_json(files["state_path"], {"retry_holds": {"keep": "private"}})
    assert main(arguments) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "retry-maintenance: ValueError"
    assert captured.out == ""
    assert not files["health_path"].exists()
    assert not files["lock_path"].exists()
    seed_files(tmp_path)
    arguments[arguments.index(str(root))] = str(tmp_path / "missing-archive")
    assert main(arguments) == 1
    assert not (tmp_path / "missing-archive").exists()


@pytest.mark.parametrize("response_id", ["offline-call", "local-precheck"])
def test_preflight_checks_persisted_editorial_budget_without_provider_calls(tmp_path, response_id):
    from tests.test_ai_editorial import database as editorial_database
    from tests.test_editorial_regeneration import ReferenceClient, queued

    generator = editorial_database.__wrapped__(tmp_path)
    connection = next(generator)
    try:
        class ResponseIdClient(ReferenceClient):
            def complete(self, request, config):
                response = super().complete(request, config)
                response["id"] = response_id
                return response
        workflow, _repository, _handlers, executor, _prepared, job_id = queued(
            connection, tmp_path, ResponseIdClient(failures=10))
        assert executor.run().failed == 1
        files = seed_files(tmp_path)
        policy = policy_value()
        policy.update(job_ids=[job_id], part_ids=[], kinds=["proofread"],
                      retryable_errors={"proofread": ["deepseek_transport_failed"]})
        write_json(files["policy_path"], policy)
        with connection:
            connection.execute("UPDATE workflow_jobs SET last_error_code='deepseek_transport_failed' WHERE job_id=?", (job_id,))
            connection.execute("UPDATE workflow_attempts SET error_code='deepseek_transport_failed' WHERE job_id=?", (job_id,))
        preview = preflight(archive_root=tmp_path, **files)
        assert preview["events"][0]["reason"] == "physical_budget_exhausted"
        dispatched = tick(archive_root=tmp_path, **files)
        assert dispatched["events"][0]["reason"] == "physical_budget_exhausted"
        assert workflow.explain_job(job_id)["status"] == "failed"
        assert read_json(files["state_path"])["retry_ledger"] == {}
    finally:
        try:
            next(generator)
        except StopIteration:
            pass


def test_preflight_uses_database_attempt_budget_with_empty_ledger(archive, tmp_path):
    root, connection, repository, job = archive
    files = seed_files(tmp_path)
    with connection:
        connection.execute("UPDATE workflow_jobs SET attempt_count=3 WHERE job_id=?", (job.job_id,))
    report = preflight(archive_root=root, **files, clock=lambda: 10000)
    assert report["events"][0]["reason"] == "budget_exhausted"
    assert report["events"][0]["retries_used"] == 2
    assert tick(archive_root=root, **files, clock=lambda: 10000)["events"][0]["reason"] == "budget_exhausted"
    assert read_json(files["state_path"])["retry_ledger"] == {}
    assert repository.explain_job(job.job_id)["status"] == "failed"


def test_preflight_reuses_shared_replacement_guard(archive, tmp_path):
    root, connection, repository, job = archive
    files = seed_files(tmp_path)
    with connection:
        connection.execute("""INSERT INTO workflow_jobs(job_id,kind,dedupe_key,payload_json,status,
            available_at,created_at,updated_at) VALUES('repair','proofread','repair',?,'succeeded',0,0,0)""",
            (json.dumps({'repair_of_job_id': job.job_id}),))
    preview = preflight(archive_root=root, **files, clock=lambda: 10000)
    assert preview["events"][0]["reason"] == "replacement_exists"
    assert tick(archive_root=root, **files, clock=lambda: 10000)["events"][0]["reason"] == "replacement_exists"
    assert read_json(files["state_path"])["retry_ledger"] == {}
    assert repository.explain_job(job.job_id)["status"] == "failed"
