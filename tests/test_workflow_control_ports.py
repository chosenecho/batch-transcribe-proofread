"""The executor can operate against its declared port without SQLite helpers."""
from contextlib import contextmanager
from dataclasses import replace
import inspect

import pytest

from bili_asr.workflow import WorkflowControl, WorkflowExecutor, WorkerInputUnavailable
from bili_asr.workflow_models import JobCancelledError, JobKind, JobStatus, LeaseLostError, WorkflowJob


class MemoryControl:
    def __init__(self):
        self.candidate = WorkflowJob("first", JobKind.SUBTITLE, 1, None, None, {}, JobStatus.QUEUED, 0)
        self.attempts = []
        self.finished = []
        self.failure = None

    def peek_candidate(self, *, kinds=None, exclude_job_ids=()):
        candidate = self.candidate
        if candidate is None or candidate.job_id in exclude_job_ids:
            return None
        return candidate if kinds is None or candidate.kind in kinds else None

    def claim(self, worker_id, *, lease_seconds, kinds, expected_candidate=None):
        candidate = self.peek_candidate(kinds=kinds)
        if candidate is None or (expected_candidate is not None and expected_candidate != candidate):
            return None
        self.candidate = None
        job = replace(candidate, status=JobStatus.RUNNING, attempt_count=candidate.attempt_count + 1,
                      lease_owner=worker_id)
        self.attempts.append(job)
        return job

    def finish(self, job_id, *, worker_id, result, expected_attempt_count):
        if self.failure is not None:
            raise self.failure
        self.finished.append((job_id, worker_id, expected_attempt_count, result))

    def fail(self, job_id, *, worker_id, error_code, expected_attempt_count, result=None):
        if self.failure is not None:
            raise self.failure
        pytest.fail(f"unexpected job failure: {error_code}")

    def open_lease_repository(self):
        return None


def executor(control, **kwargs):
    return WorkflowExecutor(control, worker_id="worker", kinds=(JobKind.SUBTITLE,),
                            handlers={JobKind.SUBTITLE: lambda job: {"job": job.job_id}}, **kwargs)


def test_control_protocol_covers_candidate_keywords_consumed_by_executor():
    assert set(inspect.signature(WorkflowControl.peek_candidate).parameters) == {
        "self", "kinds", "exclude_job_ids"}
    assert "expected_candidate" in inspect.signature(WorkflowControl.claim).parameters


@pytest.mark.parametrize("prepared", [False, True])
def test_minimal_port_runs_plain_and_candidate_access_paths(prepared):
    control = MemoryControl()
    active = []
    @contextmanager
    def access(candidate):
        active.append(candidate.job_id)
        yield
        active.pop()
    def prepare(candidate, checkpoint):
        assert active == [candidate.job_id]
        checkpoint()
    options = {"prepare_candidate": prepare, "candidate_access": access} if prepared else {}
    result = executor(control, **options).run()
    assert result.succeeded == 1 and result.failed == 0
    assert control.finished == [("first", "worker", 1, {"job": "first"})]
    assert active == []


def test_changed_candidate_is_prepared_again_before_creating_an_attempt():
    control = MemoryControl()
    prepared = []
    def prepare(candidate, checkpoint):
        prepared.append(candidate.job_id)
        if candidate.job_id == "first":
            control.candidate = replace(candidate, job_id="replacement")
    result = executor(control, prepare_candidate=prepare).run()
    assert result.succeeded == 1 and prepared == ["first", "replacement"]
    assert [job.job_id for job in control.attempts] == ["replacement"]


def test_blocked_preparation_is_excluded_without_consuming_attempt():
    control = MemoryControl()
    def prepare(candidate, checkpoint):
        raise WorkerInputUnavailable("retained bytes unavailable")
    result = executor(control, prepare_candidate=prepare).run()
    assert result.blocked == 1 and result.idle
    assert control.attempts == [] and control.finished == []


def test_unexpected_preparation_failure_does_not_consume_attempt():
    control = MemoryControl()
    def prepare(candidate, checkpoint):
        raise ValueError("failed warmup")
    with pytest.raises(ValueError, match="failed warmup"):
        executor(control, prepare_candidate=prepare).run()
    assert control.attempts == []


@pytest.mark.parametrize("failure", [LeaseLostError("lost"), JobCancelledError("cancelled")])
def test_rejected_terminal_write_does_not_report_success(failure):
    control = MemoryControl()
    control.failure = failure
    result = executor(control).run()
    assert result.succeeded == 0 and control.finished == []
    assert result.cancelled == isinstance(failure, JobCancelledError)
    assert result.failed == (not isinstance(failure, JobCancelledError))
