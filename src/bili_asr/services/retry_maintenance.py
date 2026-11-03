"""Explicit, bounded failed-job maintenance independent of worker liveness."""

import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession, _checked_path
from bili_asr.services.retry_maintenance_io import (
    MaintenanceBusyError,
    maintenance_lock,
    read_json,
    update_health,
    write_json,
)
from bili_asr.services.workflow_retry import ControlState, decide_retry
from bili_asr.storage.retry_maintenance import failed_candidates, requeue_exact_failure
from bili_asr.storage.workflow_retry import editorial_retry_facts, retry_replacements
from bili_asr.workflow_failure_policy import failure_policy
from bili_asr.workflow_models import JobKind

# Unknown/source-quality/HTTP-payment/contract failures require investigation.
# A deployment may narrow this set; it cannot turn generic ValueError into a retry.
RETRYABLE_ERRORS = frozenset({
    "TimeoutError", "TimeoutException", "ConnectError", "ConnectTimeout", "ReadTimeout",
    "WriteTimeout", "PoolTimeout", "NetworkError", "RemoteProtocolError",
    "DeepSeekTransportError", "deepseek_transport_failed",
})


def _integer(value, name, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"invalid maintenance {name}")
    return value


@dataclass(frozen=True)
class RetryPolicy:
    job_ids: tuple[str, ...]
    part_ids: tuple[int, ...]
    kinds: tuple[str, ...]
    retryable_errors: dict[str, tuple[str, ...]]
    max_retries: int
    delays_seconds: tuple[int, ...]
    interval_seconds: int

    @classmethod
    def load(cls, value: dict):
        expected = {"schema_version", "job_ids", "part_ids", "kinds", "retryable_errors",
                    "max_retries", "delays_seconds", "interval_seconds"}
        if set(value) != expected or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("unknown maintenance policy contract")
        for key in ("job_ids", "part_ids", "kinds", "delays_seconds"):
            if not isinstance(value[key], list) or len(value[key]) > 400:
                raise ValueError("invalid maintenance selector or delays")
        jobs, parts, kinds = value["job_ids"], value["part_ids"], value["kinds"]
        if not (jobs or parts) or not kinds:
            raise ValueError("maintenance requires exact jobs or parts and explicit kinds")
        if any(not isinstance(job, str) or not job or len(job) > 128 for job in jobs):
            raise ValueError("invalid maintenance job ID")
        for part in parts:
            _integer(part, "part ID", minimum=1)
        if any(not isinstance(kind, str) or kind not in {item.value for item in JobKind} for kind in kinds):
            raise ValueError("invalid maintenance kind")
        retries = _integer(value["max_retries"], "retry budget", minimum=1)
        if retries > 10 or len(value["delays_seconds"]) != retries:
            raise ValueError("maintenance retry budget requires one delay per retry, at most ten")
        for delay in value["delays_seconds"]:
            _integer(delay, "delay")
        interval = _integer(value["interval_seconds"], "interval", minimum=1)
        errors = value["retryable_errors"]
        if not isinstance(errors, dict) or set(errors) != set(kinds):
            raise ValueError("maintenance requires error allowlists for every selected kind")
        for codes in errors.values():
            if not isinstance(codes, list) or not codes or any(not isinstance(code, str)
                    or not failure_policy(code).automatic_retry for code in codes):
                raise ValueError("unknown or unsafe maintenance retry error")
        return cls(tuple(jobs), tuple(parts), tuple(kinds),
                   {kind: tuple(codes) for kind, codes in errors.items()}, retries,
                   tuple(value["delays_seconds"]), interval)


def validate_state(state: dict) -> None:
    if state.get("format") == "retry-control/v2":
        from bili_asr.services.retry_control import validate_control
        validate_control(state)
    elif "format" in state:
        from bili_asr.services.retry_control import validate_legacy_typed
        validate_legacy_typed(state)
    if not isinstance(state.get("retry_holds"), dict) or not isinstance(state.get("retry_ledger"), dict):
        raise ValueError("production retry state contract is incomplete")  # noqa: TRY004 - external JSON contract
    for entry in state["retry_ledger"].values():
        if not isinstance(entry, dict) or not {"retries", "last_retry_at"}.issubset(entry):
            raise ValueError("unknown production retry ledger entry")
        _integer(entry["retries"], "existing retries")
        _integer(entry["last_retry_at"], "existing retry time")
        for name in ('next_retry_at', 'max_retries'):
            if name in entry:
                _integer(entry[name], 'existing ' + name)


def _decision(connection, candidate, policy, state, now):
    """Deployment scope, budget and delays only narrow shared automatic policy."""
    job_id = candidate['job_id']
    ledger = {key: {name: value for name, value in entry.items()
                   if name in {'retries', 'last_retry_at', 'next_retry_at', 'max_retries'}}
              for key, entry in state['retry_ledger'].items()}
    entry = ledger.setdefault(job_id, {'retries': 0, 'last_retry_at': 0})
    entry['max_retries'] = min(policy.max_retries, entry.get('max_retries', policy.max_retries))
    spent = max(entry['retries'], candidate['attempt_count'] - 1)
    if spent < len(policy.delays_seconds):
        anchor = candidate['finished_at'] if not entry['retries'] else entry['last_retry_at']
        entry['next_retry_at'] = max(entry.get('next_retry_at', 0), anchor + policy.delays_seconds[spent])
    control = ControlState('known', frozenset(state['retry_holds']), ledger)
    replacements = retry_replacements(connection, (job_id,))[job_id]
    editorial = editorial_retry_facts(connection, (candidate,)).get(job_id)
    return decide_retry({**candidate, 'status': 'failed', 'latest_finished_at': candidate['finished_at'],
                         'editorial_retry': editorial}, control_state=control, now=now,
                        automatic=True, replacements=replacements)


def retry_skip_reason(connection, policy: RetryPolicy, state: dict, candidate: dict, now: int) -> str | None:
    """The shared preview/dispatch guard; evaluating it never reserves a retry."""
    job_id, code = candidate["job_id"], candidate["last_error_code"]
    entry = state["retry_ledger"].get(job_id, {"retries": 0, "last_retry_at": 0})
    if job_id in state["retry_holds"]:
        return "held"
    if code not in policy.retryable_errors[candidate["kind"]]:
        return "error_not_allowlisted"
    if (candidate["outcome"] != "failed" or candidate["error_code"] != code
            or type(candidate["finished_at"]) is not int):
        return "attempt_mismatch"
    if entry.get("reserved_attempt_id") == candidate["attempt_id"]:
        return "reservation_requires_review"
    decision = _decision(connection, candidate, policy, state, now)
    return None if decision['allowed'] else decision['reason']


def validate_maintenance_paths(archive_root, *, policy_path: Path, state_path: Path,
                               lock_path: Path, health_path: Path) -> None:
    """Refuse aliases before opening locks or replacing any operational file."""
    archive = Path(archive_root)
    database = (archive if archive.suffix.lower() in {".db", ".sqlite", ".sqlite3"}
                and not archive.is_dir() else archive / "archive.db")
    archive_directory = database.parent.resolve()
    archive_lock = archive_directory.parent / f".{archive_directory.name}.archive-maintenance.lock"
    targets = [policy_path, state_path, lock_path, health_path,
               health_path.with_suffix(health_path.suffix + ".lock"), database, archive_lock]
    for index, target in enumerate(targets):
        _checked_path(target.absolute())
        if target.is_dir():
            raise ValueError("maintenance files cannot be directories")
        for other in targets[:index]:
            if (str(target.resolve()).casefold() == str(other.resolve()).casefold()
                    or (target.exists() and other.exists() and target.samefile(other))):
                raise ValueError("maintenance files and archive database must differ")


def maintain_failed_jobs(connection, policy: RetryPolicy, state: dict, *, state_path: Path, now: int) -> list[dict]:
    validate_state(state)
    if state.get("format") == "retry-control/v2":
        from bili_asr.services.retry_control import validate_control
        validate_control(state, connection=connection, now=now)
    events = []
    for candidate in failed_candidates(connection, job_ids=policy.job_ids, part_ids=policy.part_ids, kinds=policy.kinds):
        job_id, code = candidate["job_id"], candidate["last_error_code"]
        reason = retry_skip_reason(connection, policy, state, candidate, now)
        entry = state["retry_ledger"].get(job_id, {"retries": 0, "last_retry_at": 0})
        if reason is not None:
            events.append({"job_id": job_id, "action": "skipped", "reason": reason})
            continue
        # File and SQLite cannot share a commit. Charge first and never refund an
        # uncertain dispatch; a crash can cost an allowance, never create one.
        # Capture the admissible precharge ledger. The charge itself must not
        # be mistaken for another completed retry during transaction recheck.
        prior_state = {**state, 'retry_ledger': {key: dict(value) for key, value in state['retry_ledger'].items()}}
        entry.update(retries=max(entry["retries"], candidate['attempt_count'] - 1) + 1, last_retry_at=now,
                     last_error_code=code, reserved_attempt_id=candidate["attempt_id"],
                     dispatch_outcome="reserved")
        state["retry_ledger"][job_id] = entry
        if state.get("format") == "retry-control/v2":
            from bili_asr.services.retry_control import seal_control
            seal_control(state)
        write_json(state_path, state)
        def eligible(candidate=candidate, prior_state=prior_state):
            # The tick lock serializes cooperative updates. Unexpected file
            # changes are denied without refunding a charged uncertain send.
            if read_json(state_path) != state:
                raise ValueError('maintenance control state changed; inspect reservation')
            return _decision(connection, candidate, policy, prior_state, now)['allowed']
        changed = requeue_exact_failure(connection, candidate, now, eligible=eligible)
        entry["dispatch_outcome"] = "requeued" if changed else "stale"
        if state.get("format") == "retry-control/v2":
            seal_control(state)
        write_json(state_path, state)
        events.append({"job_id": job_id, "attempt_id": candidate["attempt_id"],
                       "action": entry["dispatch_outcome"], "retry": entry["retries"]})
    return events


def tick(*, archive_root, policy_path: Path, state_path: Path, lock_path: Path,
         health_path: Path, clock=time.time) -> dict:
    validate_maintenance_paths(archive_root, policy_path=policy_path, state_path=state_path,
                               lock_path=lock_path, health_path=health_path)
    now = int(clock())
    with ExitStack() as locks:
        try:
            locks.enter_context(maintenance_lock(lock_path))
        except MaintenanceBusyError:
            update_health(health_path, last_lock_skipped_at=now)
            return {"status": "lock_skipped", "events": []}
        else:
            update_health(health_path, last_started_at=now, status="running", next_due_at=None)
            try:
                policy = RetryPolicy.load(read_json(policy_path))
                update_health(health_path, next_due_at=now + policy.interval_seconds,
                              interval_seconds=policy.interval_seconds)
                state = read_json(state_path)
                validate_state(state)
                with ArchiveSession(archive_root, mode=ArchiveAccessMode.WRITE) as session:
                    events = maintain_failed_jobs(session.connection, policy, state, state_path=state_path, now=now)
                completed = int(clock())
                update_health(health_path, last_succeeded_at=completed, status="succeeded",
                              error_code=None, next_due_at=completed + policy.interval_seconds)
                return {"status": "succeeded", "events": events}
            except Exception as exc:
                # Exception text can include endpoint URLs or credentials.
                update_health(health_path, last_failed_at=int(clock()), status="failed",
                              error_code=type(exc).__name__)
                raise


def health(*, health_path: Path, stale_after_seconds: int, worker_health_path: Path | None = None,
           clock=time.time) -> dict:
    _integer(stale_after_seconds, "health age", minimum=1)
    now = int(clock())
    value = read_json(health_path) if health_path.exists() else {}
    success = value.get("last_succeeded_at")
    started, failed = value.get("last_started_at", 0), value.get("last_failed_at", 0)
    if value.get("status") == "failed":
        status = "failed"
    elif success is None:
        status = "never_succeeded"
    elif now - success > stale_after_seconds or (value.get("next_due_at") is not None
                                                and now > value["next_due_at"] + stale_after_seconds):
        status = "stale"
    elif started > success and value.get("status") == "running":
        status = "running"
    elif failed > success:
        status = "failed"
    else:
        status = "healthy"
    # Worker health is an independent producer snapshot; never implies retry health.
    worker = read_json(worker_health_path) if worker_health_path is not None else {"status": "not_supplied"}
    return {"maintenance": {**value, "health_status": status, "checked_at": now}, "workers": worker}
