"""Holds, budgets, race fences and attempt evidence survive guarded retries."""

import json

import pytest

from bili_asr.services.workflow_retry import (
    ControlState,
    decide_retry,
    guarded_retry_failed,
    load_control_state,
)
from bili_asr.storage import WorkflowRepository, open_database


def _job(code="deepseek_http_503", **overrides):
    return {"job_id": "job-1", "status": "failed", "last_error_code": code,
            "attempt_count": 1, "latest_finished_at": 10, "available_at": 0, **overrides}


@pytest.fixture
def workflow(tmp_path):
    connection = open_database(tmp_path)
    with connection:
        connection.execute("INSERT INTO workflow_jobs(job_id,kind,dedupe_key,payload_json,status,available_at,attempt_count,last_error_code,created_at,updated_at) VALUES('job-1','proofread','one','{}','failed',0,1,'deepseek_http_503',0,10)")
        connection.execute("INSERT INTO workflow_attempts VALUES('attempt-1','job-1','worker',1,10,'failed','deepseek_http_503','{}')")
    yield WorkflowRepository(connection)
    connection.close()


def test_missing_and_partial_controls_are_unknown_and_never_assumed_empty(tmp_path):
    assert load_control_state().status == "not_provided"
    path = tmp_path / "controls.json"
    path.write_text('{"retry_holds":{}}')
    assert load_control_state(path).status == "not_provided"
    for automatic in (True, False):
        assert decide_retry(_job(), control_state=load_control_state(), now=100, automatic=automatic)["reason"] == "control_state_unknown"
    assert load_control_state(no_control_state=True).status == "declared_empty"


def test_hold_reason_and_credentials_never_enter_projection(tmp_path):
    path = tmp_path / "controls.json"
    path.write_text(json.dumps({"retry_holds": {"job-1": {"reason": "secret token!", "credentials": "password"}}, "retry_ledger": {}}))
    control = load_control_state(path)
    assert "secret" not in repr(control)
    assert decide_retry(_job(), control_state=control, now=100, automatic=False,
                        manual_override=True, override_reason="reviewed")["reason"] == "retry_hold"


def test_partial_controls_still_preserve_known_holds(tmp_path):
    path = tmp_path / "controls.json"
    path.write_text('{"retry_holds":{"job-1":{"reason":"private"}}}')
    state = load_control_state(path)
    assert state.status == "not_provided"
    assert state.retry_holds == frozenset({"job-1"})
    assert decide_retry(_job(), control_state=state, now=100, automatic=False,
                        manual_override=True, override_reason="checked")["reason"] == "retry_hold"


@pytest.mark.parametrize("document", [
    '{"retry_holds":{},"retry_holds":{},"retry_ledger":{}}',
    '{"retry_holds":{},"retry_ledger":{"job-1":{"retries":true}}}',
    '{"retry_holds":{},"retry_ledger":{"job-1":{}}}',
    '{"retry_holds":[],"retry_ledger":{}}',
])
def test_invalid_controls_fail_closed(tmp_path, document):
    path = tmp_path / "controls.json"
    path.write_text(document)
    with pytest.raises(ValueError):
        load_control_state(path)


def test_budget_uses_authoritative_attempt_count_even_with_empty_ledger():
    control = load_control_state(no_control_state=True)
    assert decide_retry(_job(attempt_count=3), control_state=control, now=10000)["reason"] == "budget_exhausted"
    assert decide_retry(_job(attempt_count=2), control_state=control, now=100)["reason"] == "backoff"
    assert decide_retry(_job(attempt_count=2), control_state=control, now=610)["allowed"]


@pytest.mark.parametrize("code", ["deepseek_http_402", "RevisionValidationError", "DeepSeekError", "editorial_source_repetition"])
def test_nontransient_automatic_retry_is_denied_even_without_holds(code):
    decision = decide_retry(_job(code), control_state=load_control_state(no_control_state=True), now=100)
    assert not decision["allowed"]
    assert decision["reason"] == "policy_denied"


def test_ledger_budget_and_backoff_can_only_restrict_database_budget():
    control = ControlState("known", retry_ledger={"job-1": {"retries": 2}})
    assert decide_retry(_job(), control_state=control, now=10000)["reason"] == "budget_exhausted"
    control = ControlState("known", retry_ledger={"job-1": {"retries": 0, "next_retry_at": 999}})
    assert decide_retry(_job(), control_state=control, now=998)["reason"] == "backoff"
    control = ControlState("known", retry_ledger={"job-1": {"retries": 1, "last_retry_at": 500}})
    assert decide_retry(_job(), control_state=control, now=1099)["reason"] == "backoff"


def test_manual_override_remains_blocked_by_active_replacement():
    decision = decide_retry(_job("editorial_source_repetition"), control_state=load_control_state(no_control_state=True),
                            now=100, automatic=False, manual_override=True, override_reason="checked",
                            replacements=[{"job_id": "new", "kind": "proofread", "status": "succeeded"}])
    assert decision["reason"] == "replacement_exists"


def test_manual_override_cannot_exceed_budget_or_backoff():
    control = load_control_state(no_control_state=True)
    exhausted = decide_retry(_job("RevisionValidationError", attempt_count=4), control_state=control,
                             now=100, automatic=False, manual_override=True, override_reason="reviewed")
    assert exhausted["reason"] == "budget_exhausted"
    delayed = decide_retry(_job("RevisionValidationError", attempt_count=2, latest_finished_at=100), control_state=control,
                           now=101, automatic=False, manual_override=True, override_reason="reviewed")
    assert delayed["reason"] == "backoff"


def test_guarded_requeue_preserves_failure_attempt_evidence(workflow):
    before = tuple(workflow.connection.execute("SELECT * FROM workflow_attempts").fetchone())
    report = guarded_retry_failed(workflow, automatic=True, no_control_state=True, now=100)
    assert report["requeued"] == 1
    assert tuple(workflow.connection.execute("SELECT * FROM workflow_attempts").fetchone()) == before
    assert workflow.connection.execute("SELECT status FROM workflow_jobs").fetchone()[0] == "queued"
    assert guarded_retry_failed(workflow, job_ids=["job-1"], automatic=True, no_control_state=True, now=100)["requeued"] == 0


def test_dry_run_has_no_writes(workflow):
    report = guarded_retry_failed(workflow, automatic=True, no_control_state=True, dry_run=True, now=100)
    assert report["requeued"] == 0 and report["decisions"][0]["allowed"]
    assert workflow.connection.execute("SELECT status FROM workflow_jobs").fetchone()[0] == "failed"


def test_manual_override_requires_exact_selection_and_reason(workflow):
    with pytest.raises(ValueError, match="exactly one"):
        guarded_retry_failed(workflow, manual_override=True, override_reason="reviewed", no_control_state=True)
    with pytest.raises(ValueError, match="exactly one"):
        guarded_retry_failed(workflow, job_ids=["job-1"], manual_override=True, override_reason=" ", no_control_state=True)
    with workflow.connection:
        workflow.connection.execute("UPDATE workflow_jobs SET last_error_code='deepseek_http_402'")
    assert guarded_retry_failed(workflow, job_ids=["job-1"], no_control_state=True, now=100)["requeued"] == 0
    report = guarded_retry_failed(workflow, job_ids=["job-1"], manual_override=True,
                                  override_reason="external conditions restored", no_control_state=True, now=100)
    assert report["requeued"] == 1
    assert "external conditions restored" not in json.dumps(report)
    assert len(report["override_reason_sha256"]) == 64


def test_control_change_inside_transaction_rolls_back(workflow, tmp_path, monkeypatch):
    import bili_asr.services.workflow_retry as module
    path = tmp_path / "controls.json"
    path.write_text('{"retry_holds":{},"retry_ledger":{}}')
    original = module.load_control_state
    calls = 0
    def changing(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            path.write_text('{"retry_holds":{"job-1":{}},"retry_ledger":{}}')
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "load_control_state", changing)
    with pytest.raises(ValueError, match="changed"):
        guarded_retry_failed(workflow, automatic=True, control_state_path=path, now=100)
    assert workflow.connection.execute("SELECT status FROM workflow_jobs").fetchone()[0] == "failed"


def test_low_level_automatic_requeue_cannot_bypass_controls(workflow):
    with pytest.raises(ValueError, match="guarded"):
        workflow.requeue_failed(automatic=True)


def test_job_change_after_inspection_is_never_requeued(workflow, monkeypatch):
    import bili_asr.services.workflow_retry as module
    original = module.retry_candidates
    calls = 0
    def changing(connection, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            connection.execute("UPDATE workflow_jobs SET last_error_code='deepseek_http_402'")
        return original(connection, **kwargs)
    monkeypatch.setattr(module, "retry_candidates", changing)
    result = guarded_retry_failed(workflow, no_control_state=True, automatic=True, now=100)
    assert result["requeued"] == 0
    assert result["decisions"][0]["reason"] == "job_changed"


def test_broad_selector_reports_job_that_changes_out_of_failed_state(workflow, monkeypatch):
    import bili_asr.services.workflow_retry as module
    original = module.retry_candidates
    calls = 0
    def changing(connection, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            connection.execute("UPDATE workflow_jobs SET status='succeeded'")
        return original(connection, **kwargs)
    monkeypatch.setattr(module, "retry_candidates", changing)
    result = guarded_retry_failed(workflow, kinds=["proofread"], no_control_state=True, automatic=True, now=100)
    assert result["requeued"] == 0
    assert result["decisions"][0]["reason"] == "job_changed"


def test_inline_retry_cannot_bypass_policy_and_rolls_back_evidence(workflow):
    import time
    with workflow.connection:
        workflow.connection.execute("UPDATE workflow_jobs SET status='running',lease_owner='worker',lease_expires_at=?", (int(time.time()) + 100,))
        workflow.connection.execute("UPDATE workflow_attempts SET outcome='running',finished_at=NULL,error_code=NULL")
    before = tuple(workflow.connection.execute("SELECT * FROM workflow_attempts").fetchone())
    with pytest.raises(ValueError, match="guarded"):
        workflow.fail("job-1", worker_id="worker", error_code="deepseek_http_402", retry_at=9999999999)
    assert tuple(workflow.connection.execute("SELECT * FROM workflow_attempts").fetchone()) == before
    assert workflow.connection.execute("SELECT status FROM workflow_jobs").fetchone()[0] == "running"
