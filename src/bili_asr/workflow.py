"""Stateless workflow execution boundary.

Handlers are injected callables.  The executor does not know whether a job
uses Bilibili APIs, an object store, a GPU model, or a test double; it only
persists claims and outcomes through :class:`WorkflowRepository`.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, ExitStack, nullcontext
from dataclasses import dataclass
from typing import Any, Protocol

from bili_asr.workflow_models import JobCancelledError, JobKind, LeaseLostError, WorkflowJob


JobHandler = Callable[[WorkflowJob], Mapping[str, Any] | None]
_ATTEMPT_LOCAL = threading.local()


class WorkerDrainTimeout(RuntimeError):
    """The configured graceful shutdown deadline elapsed during a task."""

    error_code = "worker_drain_timeout"


class WorkerPreparingDrain(RuntimeError):
    """Drain interrupted preparation before any business attempt existed."""


class LeaseRenewalFailed(RuntimeError):
    """Background renewal cannot recover within the current lease budget."""

    error_code = "lease_renewal_failed"


class WorkerInputUnavailable(ValueError):
    """An observed input or capacity block prevents creating a business attempt."""


def attempt_cancellation(job: WorkflowJob) -> threading.Event:
    """Return the current handler's loss-of-ownership token, never another attempt's."""
    current = getattr(_ATTEMPT_LOCAL, "attempt", None)
    if current is not None and current[0] == (job.job_id, job.lease_owner, job.attempt_count):
        return current[1]
    return threading.Event()


def attempt_checkpoint(job: WorkflowJob) -> None:
    current = getattr(_ATTEMPT_LOCAL, "attempt", None)
    if current is not None and current[0] == (job.job_id, job.lease_owner, job.attempt_count):
        current[2].check_drain()
        current[2].check_health()
        if current[1].is_set():
            raise LeaseLostError("attempt ownership was lost during inference")


class LeaseRepository(Protocol):
    def renew_lease(self, job: WorkflowJob, *, lease_seconds: int) -> None: ...
    def close(self) -> None: ...


class WorkflowControl(Protocol):
    """The executor's durable port; storage owns its transaction semantics."""

    def peek_candidate(self, *, kinds: tuple[JobKind, ...] | None = None,
                       exclude_job_ids: tuple[str, ...] = ()) -> WorkflowJob | None: ...
    def claim(self, worker_id: str, *, lease_seconds: int,
              kinds: tuple[JobKind, ...] | None,
              expected_candidate: WorkflowJob | None = None) -> WorkflowJob | None: ...
    def finish(self, job_id: str, *, worker_id: str, result: Mapping[str, Any] | None,
               expected_attempt_count: int) -> None: ...
    def fail(self, job_id: str, *, worker_id: str, error_code: str,
             expected_attempt_count: int, result: Mapping[str, Any] | None = None) -> None: ...
    def open_lease_repository(self) -> LeaseRepository | None: ...


@dataclass(frozen=True)
class ExecutionSummary:
    succeeded: int
    failed: int
    idle: bool
    cancelled: int = 0
    blocked: int = 0


class WorkflowExecutor:
    """Claim jobs one at a time and invoke stateless handlers."""

    def __init__(
        self,
        repository: WorkflowControl,
        *,
        worker_id: str,
        handlers: Mapping[JobKind, JobHandler],
        kinds: tuple[JobKind, ...] | None = None,
        lease_seconds: int = 900,
        heartbeat_interval_seconds: float | None = None,
        drain_requested: Callable[[], bool] | None = None,
        drain_timeout_seconds: float | None = None,
        prepare_candidate: Callable[[WorkflowJob, Callable[[], None]], None] | None = None,
        candidate_access: Callable[[WorkflowJob], AbstractContextManager[None]] | None = None,
        drain_checkpoint: Callable[[], None] | None = None,
        attempt_scope: Callable[[], AbstractContextManager] | None = None,
        on_success: Callable[[WorkflowJob, Mapping[str, Any] | None], None] | None = None,
    ):
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if heartbeat_interval_seconds is not None and heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        self.repository = repository
        self.worker_id = worker_id
        self.handlers = dict(handlers)
        if kinds is not None and (not kinds or any(kind not in self.handlers for kind in kinds)):
            raise ValueError("selected job kinds must have registered handlers")
        self.kinds = kinds
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds or max(1.0, lease_seconds / 3)
        if drain_timeout_seconds is not None and (not math.isfinite(drain_timeout_seconds) or drain_timeout_seconds <= 0):
            raise ValueError("drain_timeout_seconds must be finite and positive")
        self.drain_requested = drain_requested or (lambda: False)
        self.drain_timeout_seconds = drain_timeout_seconds
        self.prepare_candidate = prepare_candidate
        self.candidate_access = candidate_access
        self.drain_checkpoint = drain_checkpoint
        self.attempt_scope = attempt_scope or nullcontext
        self.on_success = on_success

    def run(self, *, limit: int | None = None) -> ExecutionSummary:
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        succeeded = failed = cancelled = 0
        blocked = set()
        while limit is None or succeeded + failed + cancelled < limit:
            if self.drain_requested():
                return ExecutionSummary(succeeded, failed, idle=succeeded + failed + cancelled == 0,
                                        cancelled=cancelled, blocked=len(blocked))
            with ExitStack() as resources:
                claim_args = {}
                if self.prepare_candidate is not None or self.candidate_access is not None:
                    candidate = self.repository.peek_candidate(kinds=self.kinds, **({"exclude_job_ids": tuple(blocked)} if blocked else {}))
                    if candidate is None:
                        return ExecutionSummary(succeeded, failed, succeeded + failed + cancelled == 0, cancelled, len(blocked))
                    def checkpoint():
                        if self.drain_requested():
                            raise WorkerPreparingDrain("worker drained before claim")
                    try:
                        if self.candidate_access is not None:
                            resources.enter_context(self.candidate_access(candidate))
                        if self.prepare_candidate is not None:
                            self.prepare_candidate(candidate, checkpoint)
                        checkpoint()
                    except WorkerInputUnavailable:
                        blocked.add(candidate.job_id)
                        continue
                    except WorkerPreparingDrain:
                        return ExecutionSummary(succeeded, failed, succeeded + failed + cancelled == 0, cancelled, len(blocked))
                    claim_args["expected_candidate"] = candidate
                job = self.repository.claim(
                    self.worker_id, lease_seconds=self.lease_seconds, kinds=self.kinds, **claim_args
                )
                if job is None:
                    if claim_args:
                        continue  # Candidate changed while preparing; select again without an attempt.
                    return ExecutionSummary(succeeded, failed, idle=succeeded + failed + cancelled == 0,
                                            cancelled=cancelled, blocked=len(blocked))
                handler = self.handlers.get(job.kind)
                if handler is None:
                    if self._fail(job, "no_handler"):
                        cancelled += 1
                    else:
                        failed += 1
                    continue
                heartbeat = _LeaseHeartbeat(
                    self.repository,
                    job,
                    lease_seconds=self.lease_seconds,
                    interval_seconds=self.heartbeat_interval_seconds,
                    drain_requested=self.drain_requested,
                    drain_timeout_seconds=self.drain_timeout_seconds,
                    drain_checkpoint=self.drain_checkpoint,
                )
                heartbeat.start()
                _ATTEMPT_LOCAL.attempt = ((job.job_id, job.lease_owner, job.attempt_count), heartbeat.cancelled, heartbeat)
                try:
                    with self.attempt_scope():
                        heartbeat.check_health()
                        result = handler(job)
                        heartbeat.check_health()
                except Exception as exc:  # noqa: BLE001 - injected handlers return bounded typed diagnostics.
                    from bili_asr.workflow_errors import JobExecutionError
                    from bili_asr.workflow_failure_diagnostics import execution_failure
                    if isinstance(exc, JobExecutionError):
                        error_code, details = exc.error_code, {"diagnostic": exc.safe_details}
                    elif isinstance(exc, (WorkerDrainTimeout, LeaseRenewalFailed)):
                        error_code, details = exc.error_code, None
                    else:
                        error_code, diagnostic = execution_failure(exc)
                        details = {"diagnostic": {**diagnostic, "job_id": job.job_id, "attempt_count": job.attempt_count}}
                    if self._fail(job, error_code or "Exception",
                                  details if isinstance(details, Mapping) else None):
                        cancelled += 1
                    else:
                        failed += 1
                else:
                    try:
                        self.repository.finish(job.job_id, worker_id=self.worker_id, result=result,
                                               expected_attempt_count=job.attempt_count)
                    except JobCancelledError:
                        cancelled += 1
                    except LeaseLostError:
                        failed += 1
                    else:
                        succeeded += 1
                        if self.on_success is not None:
                            self.on_success(job, result)
                finally:
                    del _ATTEMPT_LOCAL.attempt
                    heartbeat.stop()
        return ExecutionSummary(succeeded, failed, idle=False, cancelled=cancelled, blocked=len(blocked))

    def _fail(self, job: WorkflowJob, error_code: str, details: Mapping[str, Any] | None = None) -> bool:
        """Return whether the terminal write observed authoritative cancellation."""
        try:
            kwargs = {} if details is None else {"result": details}
            self.repository.fail(job.job_id, worker_id=self.worker_id, error_code=error_code,
                                 expected_attempt_count=job.attempt_count, **kwargs)
        except JobCancelledError:
            return True
        except LeaseLostError:
            # Reclamation records lease_expired. A stale worker must leave the
            # newer attempt's result and ownership untouched, even with a reused ID.
            pass
        except Exception as exc:
            # A failed database cannot persist its own diagnostic. Emit only a
            # bounded independent signal, then preserve the original failure.
            import json
            from bili_asr.diagnostics import write_stderr
            from bili_asr.exception_diagnostics import capture_failure
            try:
                write_stderr(json.dumps({"event": "workflow_failure_persist_failed", "job_id": job.job_id,
                    "attempt_count": job.attempt_count, "diagnostic": capture_failure(exc, phase="failure_persistence")},
                    sort_keys=True))
            except OSError:
                pass  # Preserve the persistence error even if stderr is also unavailable.
            raise
        return False


class _LeaseHeartbeat:
    """Renew one exact attempt from a connection owned by a daemon thread."""

    def __init__(
        self,
        repository: WorkflowControl,
        job: WorkflowJob,
        *,
        lease_seconds: int,
        interval_seconds: float,
        drain_requested: Callable[[], bool] = lambda: False,
        drain_timeout_seconds: float | None = None,
        drain_checkpoint: Callable[[], None] | None = None,
    ) -> None:
        self.repository = repository
        self.job = job
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self.cancelled = threading.Event()
        self._ownership_error: LeaseLostError | None = None
        self.drain_requested = drain_requested
        self.drain_timeout_seconds = drain_timeout_seconds
        self.drain_checkpoint = drain_checkpoint
        self.failed = threading.Event()
        self._drain_started: float | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name=f"workflow-lease-{self.job.job_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def check_drain(self) -> None:
        if self.drain_checkpoint is not None:
            self.drain_checkpoint()
            return
        if self.drain_requested():
            if self._drain_started is None:
                self._drain_started = time.monotonic()
            if (self.drain_timeout_seconds is not None
                    and time.monotonic() - self._drain_started >= self.drain_timeout_seconds):
                raise WorkerDrainTimeout("graceful worker shutdown deadline elapsed")

    def check_health(self) -> None:
        if self.failed.is_set():
            raise LeaseRenewalFailed("lease renewal failed before its recovery deadline")
        if self.cancelled.is_set():
            if self._ownership_error is not None:
                raise self._ownership_error
            raise LeaseLostError("attempt ownership was lost during execution")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds + 1.0))
        self._thread = None

    def _run(self) -> None:
        # Open the connection inside the heartbeat thread so sqlite's default
        # thread-affinity check is satisfied.
        import sqlite3
        from bili_asr.archive_maintenance import ArchiveBusyError

        lease_repository = None
        # Stop degraded execution before lease expiry. Claim timestamps have
        # second precision, so leave a reserve rather than spending the whole
        # lease on retries. Successful renewal resets this recovery budget.
        budget = max(0.1, self.lease_seconds / 2)
        deadline = time.monotonic() + budget
        try:
            # Renew once before the first wait.  Claim timestamps are stored at
            # second precision; waiting for the first interval can otherwise
            # lose a one-second test lease at a wall-clock boundary.
            while not self._stop.is_set():
                try:
                    if lease_repository is None:
                        lease_repository = self.repository.open_lease_repository()
                        if lease_repository is None:
                            return  # In-memory repositories deliberately have no second connection.
                    lease_repository.renew_lease(self.job, lease_seconds=self.lease_seconds)
                except LeaseLostError as error:
                    # Preserve authoritative cancellation when the heartbeat
                    # observes it before the handler's next database check.
                    self._ownership_error = error
                    self.cancelled.set()
                    # The terminal write will be fenced by the same attempt tuple.
                    return
                except (sqlite3.Error, OSError, ArchiveBusyError):
                    # A transient SQLite lock or I/O error must not turn a healthy
                    # handler into a false success.  Retry at the next interval;
                    # terminal writes still enforce the exact lease fence.
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.failed.set()
                        return
                    if self._stop.wait(min(self.interval_seconds, remaining)):
                        return
                    continue
                except Exception:  # noqa: BLE001 - expose fatal background faults to the executor.
                    self.failed.set()
                    return
                deadline = time.monotonic() + budget
                if self._stop.wait(self.interval_seconds):
                    return
        finally:
            if lease_repository is not None:
                lease_repository.close()
