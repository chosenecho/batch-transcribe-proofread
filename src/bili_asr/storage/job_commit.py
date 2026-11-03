"""One SQLite ownership fence for authoritative worker side effects."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import sqlite3
import time

from bili_asr.workflow_models import JobCancelledError, JobStatus, LeaseLostError, WorkflowJob


class JobCommitGuard:
    def __init__(self, connection: sqlite3.Connection, *, clock: Callable[[], float] = time.time):
        self.connection = connection
        self.clock = clock

    def assert_unexpired(self, deadline: int | None) -> None:
        """Check a captured deadline after a terminal write clears the lease."""
        if deadline is None or int(deadline) <= int(self.clock()):
            raise LeaseLostError("job lease was lost")

    def assert_lease(self, job: WorkflowJob) -> int:
        """Validate this attempt and return its deadline for lease transitions."""
        row = self.connection.execute(
            "SELECT status, lease_owner, lease_expires_at, attempt_count "
            "FROM workflow_jobs WHERE job_id = ?", (job.job_id,),
        ).fetchone()
        if row is not None and row["status"] == JobStatus.CANCELLED.value:
            raise JobCancelledError("job was cancelled")
        if (row is None or row["status"] != JobStatus.RUNNING.value
                or row["lease_owner"] != job.lease_owner
                or row["attempt_count"] != job.attempt_count):
            raise LeaseLostError("job lease was lost")
        self.assert_unexpired(row["lease_expires_at"])
        return int(row["lease_expires_at"])

    @contextmanager
    def transaction(self, *, on_rollback: Callable[[], None] | None = None) -> Iterator[None]:
        if self.connection.in_transaction:
            raise RuntimeError("workflow write transaction cannot be nested")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except BaseException:
            # Remove an incomplete mutable bundle marker while the writer lock
            # still excludes another publisher. Immutable artifacts may remain
            # as unregistered, retryable files after a failed database commit.
            try:
                if on_rollback is not None:
                    on_rollback()
            finally:
                self.connection.rollback()
            raise

    @contextmanager
    def owned_transaction(self, job: WorkflowJob, *, on_rollback: Callable[[], None] | None = None) -> Iterator[None]:
        with self.transaction(on_rollback=on_rollback):
            self.assert_lease(job)
            yield
            self.assert_lease(job)
