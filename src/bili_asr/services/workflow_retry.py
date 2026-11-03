"""Guarded retry use case with explicit, read-only external control evidence."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from bili_asr.storage.workflow_retry import (
    editorial_retry_facts,
    requeue_decided_job,
    retry_candidates,
    retry_replacements,
)
from bili_asr.workflow_failure_policy import POLICY_VERSION, failure_policy

_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_MAX_BYTES = 8 * 1024 * 1024
_MAX_ENTRIES = 50000


@dataclass(frozen=True)
class ControlState:
    status: str = "not_provided"
    retry_holds: frozenset[str] = frozenset()
    retry_ledger: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    digest: str | None = None

    def as_dict(self) -> dict:
        return {"status": self.status, "digest": self.digest,
                "retry_holds": len(self.retry_holds) if self.status != "not_provided" else None,
                "retry_ledger_entries": len(self.retry_ledger) if self.status != "not_provided" else None}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate control-state key")
        result[key] = value
    return result


def load_control_state(path: str | Path | None = None, *, no_control_state: bool = False,
                       connection=None, now: int | None = None) -> ControlState:
    """Read known-empty controls only through an explicit declaration.

    Other operational state is ignored. Arbitrary hold reasons are never copied
    into diagnostics; the digest detects even changes in ignored state fields.
    """
    if path is not None and no_control_state:
        raise ValueError("control state and no-control-state are mutually exclusive")
    if path is None:
        return ControlState("declared_empty", digest="declared_empty") if no_control_state else ControlState()
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValueError("control state exceeds size limit")
        state = json.loads(raw, object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("control state is unavailable or invalid") from None
    if not isinstance(state, dict):
        raise ValueError("control state must be an object")  # noqa: TRY004 - serialized input contract
    if state.get("format") == "retry-control/v2":
        from bili_asr.services.retry_control import validate_control
        validate_control(state, connection=connection, now=now)
        if connection is None:
            raise ValueError("portable retry controls require archive scope verification")
    elif "format" in state:
        from bili_asr.services.retry_control import validate_legacy_typed
        validate_legacy_typed(state)
    complete = "retry_holds" in state and "retry_ledger" in state
    holds, ledger = state.get("retry_holds", {}), state.get("retry_ledger", {})
    for items in (holds, ledger):
        if (not isinstance(items, dict) or len(items) > _MAX_ENTRIES
                or any(not isinstance(key, str) or not _ID.fullmatch(key) for key in items)):
            raise ValueError("invalid or oversized control-state mapping")
    bounded_ledger = {}
    for key, entry in ledger.items():
        if not isinstance(entry, dict):
            raise ValueError("invalid retry ledger entry")  # noqa: TRY004 - serialized input contract
        bounded = {}
        for name in ("retries", "last_retry_at", "next_retry_at", "max_retries"):
            if name in entry:
                value = entry[name]
                if type(value) is not int or not 0 <= value <= 2**63 - 1:
                    raise ValueError("invalid retry ledger count or time")
                bounded[name] = value
        # An entry without its retry count cannot prove budget availability.
        if "retries" not in bounded:
            raise ValueError("retry ledger entry requires retries")
        bounded_ledger[key] = bounded
    return ControlState("known" if complete else "not_provided", frozenset(holds), bounded_ledger, hashlib.sha256(raw).hexdigest())


def decide_retry(job: Mapping, *, control_state: ControlState, now: int,
                 automatic: bool = True, manual_override: bool = False,
                 override_reason: str | None = None, replacements: Iterable[Mapping] = ()) -> dict:
    """Project one decision without I/O, clocks or mutations.

    Database attempt count remains the authoritative lower bound on spent
    budget, even if an external ledger is stale or intentionally empty.
    """
    code = job.get("last_error_code", job.get("error_code"))
    policy = failure_policy(code)
    job_id = job["job_id"]
    ledger = control_state.retry_ledger.get(job_id, {})
    attempts = int(job.get("attempt_count", 0))
    retries = max(0, attempts - 1, ledger.get("retries", 0))
    budget = min(policy.max_attempts - 1, ledger.get("max_retries", policy.max_attempts - 1))
    finished = job.get("latest_finished_at")
    next_at = max(int(job.get("available_at", 0)), ledger.get("next_retry_at", 0))
    if finished is not None:
        next_at = max(next_at, int(finished) + policy.backoff_seconds[min(retries, len(policy.backoff_seconds) - 1)])
    if "last_retry_at" in ledger:
        next_at = max(next_at, ledger["last_retry_at"] + policy.backoff_seconds[min(retries, len(policy.backoff_seconds) - 1)])
    active_replacements = [dict(item) for item in replacements if item["status"] in ("queued", "running", "succeeded")]
    reason = "eligible"
    if job.get("status") != "failed":
        reason = "not_failed"
    elif job_id in control_state.retry_holds:
        reason = "retry_hold"
    elif control_state.status == "not_provided":
        reason = "control_state_unknown"
    elif active_replacements:
        reason = "replacement_exists"
    elif (job.get('editorial_retry') or {}).get('eligible') is False:
        reason = job['editorial_retry']['reason']
    elif automatic and not policy.automatic_retry:
        reason = "policy_denied"
    elif not automatic and policy.retry_policy not in ("automatic", "manual") and not manual_override:
        reason = "manual_override_required"
    elif retries >= budget:
        reason = "budget_exhausted"
    elif now < next_at:
        reason = "backoff"
    elif manual_override and (automatic or not override_reason or not override_reason.strip()):
        reason = "invalid_manual_override"
    elif manual_override:
        reason = "manual_override"
    return {"job_id": job_id, "error_code": code, **policy.as_dict(),
            "allowed": reason in ("eligible", "manual_override"), "reason": reason,
            "control_state": control_state.status, "automatic": automatic,
            "next_retry_at": next_at, "retries_used": retries, "max_retries": budget,
            "replacements": active_replacements, "editorial_retry": job.get('editorial_retry')}


def guarded_retry_failed(workflow, *, part_ids=None, job_ids=None, kinds=None,
                         control_state_path=None, no_control_state: bool = False,
                         automatic: bool = False, manual_override: bool = False,
                         override_reason: str | None = None, now: int | None = None,
                         dry_run: bool = False) -> dict:
    """Decide, then revalidate under the SQLite writer fence before requeue.

    External files remain read-only: their owner must serialize changes with
    retry operations. A digest is checked before decisions and immediately
    before committing; detected changes roll back the entire batch.
    """
    job_ids = None if job_ids is None else tuple(dict.fromkeys(job_ids))
    part_ids = None if part_ids is None else tuple(part_ids)
    kinds = None if kinds is None else tuple(kinds)
    if manual_override:
        if (automatic or job_ids is None or len(job_ids) != 1 or part_ids is not None or kinds is not None
                or not isinstance(override_reason, str) or not 1 <= len(override_reason.strip()) <= 1024):
            raise ValueError("manual override requires exactly one job-id and a bounded nonempty reason")
    elif override_reason is not None:
        raise ValueError("override reason requires manual override")
    captured_at = int(time.time()) if now is None else int(now)
    control = load_control_state(control_state_path, no_control_state=no_control_state,
                                 connection=workflow.connection, now=captured_at)
    before = retry_candidates(workflow.connection, part_ids=part_ids, job_ids=job_ids, kinds=kinds,
                              failed_only=job_ids is None)

    def decisions(rows):
        replacements = retry_replacements(workflow.connection, (row["job_id"] for row in rows))
        editorial = editorial_retry_facts(workflow.connection, rows)
        return [decide_retry({**row, 'editorial_retry': editorial.get(row['job_id'])}, control_state=control, now=captured_at, automatic=automatic,
                             manual_override=manual_override, override_reason=override_reason,
                             replacements=replacements[row["job_id"]]) for row in rows]

    report = {"policy_version": POLICY_VERSION, "captured_at": captured_at,
              "control_state": control.as_dict(), "requeued": 0, "dry_run": dry_run}
    if manual_override:
        report["override_reason_sha256"] = hashlib.sha256(override_reason.strip().encode()).hexdigest()
    if dry_run:
        report["decisions"] = decisions(before)
        return report
    with workflow.commit_guard.transaction():
        current_control = load_control_state(control_state_path, no_control_state=no_control_state,
                                             connection=workflow.connection, now=captured_at)
        if current_control != control:
            raise ValueError("retry control state changed; inspect again")
        # Never add newly failed jobs to a batch that was inspected before locking.
        snapshots = {row["job_id"]: row for row in before}
        selected_ids = tuple(snapshots)
        current = []
        for offset in range(0, len(selected_ids), 500):
            current.extend(retry_candidates(workflow.connection,
                job_ids=selected_ids[offset:offset + 500], failed_only=False))
        result = decisions(current)
        for row, decision in zip(current, result):
            if row != snapshots[row["job_id"]]:
                decision.update(allowed=False, reason="job_changed")
            if decision["allowed"]:
                count = requeue_decided_job(workflow.connection, row, now=captured_at)
                report["requeued"] += count
                decision["requeued"] = bool(count)
        if load_control_state(control_state_path, no_control_state=no_control_state,
                              connection=workflow.connection, now=captured_at) != control:
            raise ValueError("retry control state changed; inspect again")
        report["decisions"] = result
    return report
