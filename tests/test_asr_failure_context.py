"""Parent-side ASR failures retain their run identity without changing control flow."""

import json

import pytest

from bili_asr.workflow import (
    LeaseRenewalFailed, WorkerDrainTimeout, WorkerInputUnavailable,
    WorkerPreparingDrain, WorkflowExecutor,
)
from bili_asr.workflow_errors import JobExecutionError
from bili_asr.workflow_models import JobCancelledError, JobKind, LeaseLostError
from bili_asr.workflow_runtime import ArchiveWorkflowHandlers
from bili_asr.editorial_budget import BudgetExceeded
from tests.test_asr_admission import runtime as runtime, executor
from tests.support.asr_quality import coverage, cue


@pytest.mark.parametrize("phase", ["inference", "result_commit"])
@pytest.mark.parametrize("error_type", [ValueError, TypeError, RuntimeError,
    type("SensitiveCustomFailure", (Exception,), {})])
def test_parent_asr_failure_persists_run_and_safe_phase(runtime, monkeypatch, phase, error_type):
    connection, workflow, _, root = runtime
    error = error_type("SESSDATA secret /private/path?token=secret")

    def fail(*_args, **_kwargs):
        raise error

    if phase == "inference":
        handlers = ArchiveWorkflowHandlers(connection, workflow, archive_root=root, sessdata=None,
                                           timeout_transcriber=fail)
        worker = WorkflowExecutor(workflow, worker_id="inference", handlers=handlers.handlers(), kinds=(JobKind.ASR,))
    else:
        handlers, worker = executor(runtime, [cue(0, 4)], coverage())
        monkeypatch.setattr("bili_asr.services.asr_result.preserve_inference", fail)
    try:
        assert worker.run().failed == 1
    finally:
        handlers.close()
    run = connection.execute("SELECT * FROM acquisition_runs WHERE kind='asr'").fetchone()
    attempt = connection.execute("SELECT * FROM workflow_attempts WHERE job_id IN "
        "(SELECT job_id FROM workflow_jobs WHERE kind='asr')").fetchone()
    diagnostic = json.loads(attempt["result_json"])["diagnostic"]
    assert run["outcome"] == "failed"
    expected_type = error_type.__name__ if error_type in (ValueError, TypeError, RuntimeError) else "Exception"
    assert attempt["error_code"] == expected_type
    profile_id = connection.execute("SELECT profile_id FROM workflow_jobs WHERE kind='asr'").fetchone()[0]
    assert diagnostic == {"phase": phase, "exception_type": expected_type,
        "run_id": run["run_id"], "profile_id": profile_id,
        "job_id": attempt["job_id"], "attempt_count": 1}
    assert "secret" not in attempt["result_json"] and "Sensitive" not in attempt["result_json"]
    assert workflow.claim("paid", kinds=(JobKind.PROOFREAD,)) is None


@pytest.mark.parametrize("error", [JobCancelledError("cancelled"), LeaseLostError("lost"),
    WorkerDrainTimeout("drained"), LeaseRenewalFailed("renewal failed"),
    WorkerInputUnavailable("unavailable"), WorkerPreparingDrain("preparing drain"),
    JobExecutionError("reviewed_failure", {"stage": "reviewed"}), KeyboardInterrupt(), SystemExit(7)])
def test_asr_control_and_reviewed_errors_keep_original_identity(runtime, error):
    connection, workflow, _, root = runtime
    job = workflow.claim("inference", kinds=(JobKind.ASR,))

    def fail(*_args, **_kwargs):
        raise error

    handlers = ArchiveWorkflowHandlers(connection, workflow, archive_root=root, sessdata=None,
                                       timeout_transcriber=fail)
    try:
        with pytest.raises(type(error)) as caught:
            handlers.local_asr(job)
        assert caught.value is error
        assert connection.execute("SELECT outcome FROM acquisition_runs WHERE kind='asr'").fetchone()[0] == "failed"
    finally:
        handlers.close()


def test_reviewed_domain_diagnostic_survives_added_run_context(runtime):
    connection, workflow, _, root = runtime
    job = workflow.claim("inference", kinds=(JobKind.ASR,))
    error = BudgetExceeded(3, ["input_token_limit"])

    def fail(*_args, **_kwargs):
        raise error

    handlers = ArchiveWorkflowHandlers(connection, workflow, archive_root=root, sessdata=None,
                                       timeout_transcriber=fail)
    try:
        with pytest.raises(JobExecutionError) as caught:
            handlers.local_asr(job)
        assert caught.value.error_code == "editorial_budget_exceeded"
        assert error.safe_details.items() <= caught.value.safe_details.items()
        assert caught.value.safe_details["phase"] == "inference"
        assert caught.value.safe_details["run_id"]
    finally:
        handlers.close()
