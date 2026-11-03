"""Read-only evidence for an explicit retry-maintenance handoff."""

import time
from pathlib import Path

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.services.retry_maintenance import (
    RetryPolicy,
    retry_skip_reason,
    validate_maintenance_paths,
    validate_state,
)
from bili_asr.services.retry_maintenance_io import read_json
from bili_asr.storage.retry_maintenance import failed_candidates


def _preview(connection, policy: RetryPolicy, state: dict, candidate: dict, now: int) -> dict:
    """Explain the tick's guards without reserving or dispatching a retry."""
    job_id = candidate["job_id"]
    entry = state["retry_ledger"].get(job_id, {"retries": 0, "last_retry_at": 0})
    reason = retry_skip_reason(connection, policy, state, candidate, now)
    return {"job_id": job_id, "attempt_id": candidate["attempt_id"],
            "action": "skipped" if reason else "would_requeue",
            **({"reason": reason} if reason else {}),
            "retries_used": max(entry["retries"], candidate["attempt_count"] - 1)}


def preflight(*, archive_root, policy_path: Path, state_path: Path, lock_path: Path,
              health_path: Path, clock=time.time) -> dict:
    """Validate inputs and preview only; a later tick rechecks all dispatch guards.

    ArchiveSession coordinates read access. No retry lock, health or state file is
    created, no retry allowance is reserved, and no database row is changed.
    This cannot prove that an old retry owner has stopped or a timer is enabled.
    """
    validate_maintenance_paths(archive_root, policy_path=policy_path, state_path=state_path,
                               lock_path=lock_path, health_path=health_path)
    policy = RetryPolicy.load(read_json(policy_path))
    state = read_json(state_path)
    validate_state(state)
    now = int(clock())
    with ArchiveSession(archive_root, mode=ArchiveAccessMode.READ) as session:
        connection = session.connection
        # One SQLite snapshot keeps selector evidence and failed attempts aligned.
        connection.execute("BEGIN")
        if state.get("format") == "retry-control/v2":
            from bili_asr.services.retry_control import validate_control
            validate_control(state, connection=connection, now=now)
        missing_jobs = [job for job in policy.job_ids if not connection.execute(
            "SELECT 1 FROM workflow_jobs WHERE job_id=?", (job,)).fetchone()]
        missing_parts = [part for part in policy.part_ids if not connection.execute(
            "SELECT 1 FROM video_parts WHERE video_part_id=?", (part,)).fetchone()]
        candidates = failed_candidates(connection, job_ids=policy.job_ids,
                                       part_ids=policy.part_ids, kinds=policy.kinds)
        events = [_preview(connection, policy, state, candidate, now) for candidate in candidates]
    return {"status": "invalid_scope" if missing_jobs or missing_parts else "preflight_passed",
            "checked_at": now, "read_only": True, "interval_seconds": policy.interval_seconds,
            "missing_job_ids": missing_jobs, "missing_part_ids": missing_parts,
            "retry_holds_count": len(state["retry_holds"]),
            "retry_ledger_count": len(state["retry_ledger"]), "events": events,
            "handoff_verified": False}
