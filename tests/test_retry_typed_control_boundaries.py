"""Typed control evidence remains inactive without scope and keeps spent budgets."""
import hashlib
from copy import deepcopy

import pytest

from bili_asr.services.retry_control import capture_control, encoded
from bili_asr.services.retry_maintenance import (
    RetryPolicy,
    maintain_failed_jobs,
    validate_state,
)
from bili_asr.services.workflow_retry import guarded_retry_failed, load_control_state
from tests.test_retry_maintenance import archive as retry_archive
from tests.test_retry_maintenance import policy_value

archive = retry_archive


def typed_state(job_id):
    return {"format": "retry-control/v1", "schema_version": 1,
            "retry_holds": {job_id: "manual alignment investigation"},
            "retry_ledger": {job_id: {"retries": 1, "last_retry_at": 1000, "max_retries": 1}}}


@pytest.mark.parametrize("held,expected", [(True, "retry_hold"), (False, "budget_exhausted")])
def test_legacy_typed_controls_keep_holds_or_exhausted_budget_across_manual_retry(archive, tmp_path, held, expected):
    _, connection, workflow, job = archive
    state = typed_state(job.job_id)
    if not held:
        state["retry_holds"] = {}
    path = tmp_path / "typed-control.json"
    path.write_bytes(encoded(state))
    original = path.read_bytes()
    attempts = [tuple(row) for row in connection.execute("SELECT * FROM workflow_attempts")]
    loaded = load_control_state(path)
    assert loaded.status == "known" and loaded.retry_ledger[job.job_id]["retries"] == 1
    validate_state(state)
    report = guarded_retry_failed(workflow, job_ids=[job.job_id], control_state_path=path, now=10000,
                                  automatic=False, manual_override=True, override_reason="cause reviewed", dry_run=True)
    assert report["decisions"][0]["reason"] == expected
    assert workflow.explain_job(job.job_id)["status"] == "failed"
    assert path.read_bytes() == original
    assert [tuple(row) for row in connection.execute("SELECT * FROM workflow_attempts")] == attempts


@pytest.mark.parametrize("damage", ["unknown_version", "boolean_version", "boolean_retries", "extra_field"])
def test_invalid_typed_controls_stop_both_manual_and_maintenance_before_charging(archive, tmp_path, damage):
    _, connection, workflow, job = archive
    state = typed_state(job.job_id)
    if damage == "unknown_version":
        state["format"] = "retry-control/v9"
    elif damage == "boolean_version":
        state["schema_version"] = True
    elif damage == "boolean_retries":
        state["retry_ledger"][job.job_id]["retries"] = True
    else:
        state["credential"] = "offline-placeholder-must-not-be-interpreted"
    path = tmp_path / "invalid-typed.json"
    path.write_bytes(encoded(state))
    raw = path.read_bytes()
    evidence = deepcopy(state)
    with pytest.raises(ValueError):
        load_control_state(path)
    with pytest.raises(ValueError):
        maintain_failed_jobs(connection, RetryPolicy.load(policy_value()), state, state_path=path, now=10000)
    assert state == evidence and path.read_bytes() == raw
    assert workflow.explain_job(job.job_id)["status"] == "failed"
    assert connection.execute("SELECT count(*) FROM workflow_attempts").fetchone()[0] == 1


def test_portable_control_requires_scope_even_when_its_digests_are_valid(archive, tmp_path):
    root, connection, workflow, job = archive
    path = tmp_path / "operational.json"
    path.write_bytes(encoded({"retry_holds": {job.job_id: "do not requeue"}, "retry_ledger": {
        job.job_id: {"retries": 2, "last_retry_at": 1000, "max_retries": 2}}}))
    portable = capture_control(path, connection, database_sha256=hashlib.sha256((root / "archive.db").read_bytes()).hexdigest(),
                               captured_at=1000)
    restored = tmp_path / "portable.json"
    restored.write_bytes(encoded(portable))
    original = restored.read_bytes()
    with pytest.raises(ValueError, match="scope verification"):
        load_control_state(restored)
    control = load_control_state(restored, connection=connection, now=1001)
    assert control.retry_holds == frozenset({job.job_id}) and control.retry_ledger[job.job_id]["retries"] == 2
    with pytest.raises(ValueError, match="clock"):
        load_control_state(restored, connection=connection, now=999)
    assert restored.read_bytes() == original and workflow.explain_job(job.job_id)["status"] == "failed"
