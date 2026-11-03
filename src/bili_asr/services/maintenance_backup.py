"""Controlled checkpoints and bounded, queryable automatic backup runs.

Backup policy is local operational input. It cannot start workers, recover jobs,
remove retry holds, or delete business data. Notifications are durable events
for a supervisor or operator to consume, with no implicit network messaging.
"""
from __future__ import annotations

import sqlite3
import time
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.services import maintenance_package_core as package
from bili_asr.services.maintenance_chain import (
    apply_retention,
    retention_plan,
    save_delta,
    verify_chain,
)
from bili_asr.services.maintenance_import import _save
from bili_asr.storage.maintenance_scope import scope


def checkpoint_archive(source_root: Path, *, timeout_ms: int = 1000) -> dict:
    root = package.absolute(source_root)
    database = root / "archive.db"
    package.require_regular_file(database)
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 30000:
        raise ValueError("checkpoint timeout must be 1..30000 milliseconds")
    with ArchiveSession(root, mode=ArchiveAccessMode.MAINTENANCE).access():
        journal = root / "archive.db-journal"
        if journal.exists() and journal.stat().st_size:
            raise ValueError("pending rollback journal requires recovery before automatic checkpoint")
        with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=timeout_ms / 1000)) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("BEGIN EXCLUSIVE")
            for table, condition in (("workflow_jobs", "status='running'"), ("workflow_attempts", "outcome='running'"),
                                     ("ingestion_runs", "outcome='running'"), ("acquisition_runs", "outcome='running'"),
                                     ("editorial_model_calls", "finished_at IS NULL")):
                if connection.execute(f'SELECT 1 FROM "{table}" WHERE {condition} LIMIT 1').fetchone():
                    connection.rollback()
                    raise ValueError("checkpoint refuses unfinished execution: " + table)
            connection.rollback()
            busy, log, completed = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy or log != completed:
                raise ValueError("SQLite checkpoint incomplete; stop all readers and writers")
        package._database_state(database)
        package.validate_snapshot_database(database)
        package.refuse_unfinished(database)
    return {"operation": "maintenance-checkpoint", "valid": True, "busy": busy,
            "wal_pages": log, "checkpointed_pages": completed, "business_rows_changed": False}


def policy(source_root: Path, repository: Path, *, interval_seconds: int = 3600, keep_count: int = 5,
           keep_days: int = 30, alert_after: int = 3, artifact_root: Path | None = None,
           selection: dict | None = None, mode: str = "incremental", compression: str = "stored",
           profile: str = "archive", external_components: tuple | list = (),
           no_external_control_state: bool = False, media_policy: str = "local-required") -> dict:
    source, destination = package.absolute(source_root), package.absolute(repository)
    if source == destination or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("backup repository and source must be disjoint")
    if artifact_root is not None:
        product = package.absolute(artifact_root)
        if destination == product or destination.is_relative_to(product) or product.is_relative_to(destination):
            raise ValueError("backup repository and external artifact root must be disjoint")
    if any(type(value) is not int or value < minimum for value, minimum in
           ((interval_seconds, 1), (keep_count, 1), (keep_days, 0), (alert_after, 1))):
        raise ValueError("invalid automatic backup interval, retention or alert threshold")
    if selection is not None and scope(**selection) != selection:
        raise ValueError("invalid automatic backup scope")
    if mode not in {"full", "incremental", "differential"} or compression not in {"stored", "deflate"}:
        raise ValueError("unsupported automatic backup mode or compression")
    if mode == "full" and compression != "stored":
        raise ValueError("full maintenance backups use stored containers; deflate is supported only for differential/incremental payloads")
    if selection is not None and selection["profile"] == "publication" and mode != "full":
        raise ValueError("public publication backup requires full mode; its projection has no database dependency chain")
    if profile not in {"archive", "task-state"} or type(no_external_control_state) is not bool:
        raise ValueError("invalid backup state profile or control declaration")
    if media_policy not in {"local-required", "verified-references"} or profile != "task-state" and media_policy != "local-required":
        raise ValueError("verified-references media policy requires task-state profile")
    if profile == "archive" and (external_components or no_external_control_state):
        raise ValueError("external state components require task-state profile")
    if profile == "task-state":
        if mode != "full" or compression != "stored" or selection is not None:
            raise ValueError("task-state backups require full stored containers without row selectors")
        if not isinstance(external_components, (tuple, list)):
            raise ValueError("state components must be a typed list")
        if bool(external_components) == no_external_control_state:
            raise ValueError("declare typed external control state or explicitly declare none")
        for component in external_components:
            if (not isinstance(component, dict) or set(component) != {"kind", "path", "lock_path"}
                    or component["kind"] not in {"retry-control/v1", "retry-control/v2", "backup-job-state/v1"}
                    or not isinstance(component["path"], str)
                    or not isinstance(component["lock_path"], str)
                    or str(package.absolute(Path(component["path"]))) != component["path"]
                    or str(package.absolute(Path(component["lock_path"]))) != component["lock_path"]
                    or component["path"] == component["lock_path"]):
                raise ValueError("invalid typed external component declaration")
        if len({component["path"] for component in external_components}) != len(external_components):
            raise ValueError("duplicate external state component paths")
    value = {"format": "maintenance-backup-policy/v1", "source_root": str(source), "repository": str(destination),
             "artifact_root": None if artifact_root is None else str(package.absolute(artifact_root)),
             "interval_seconds": interval_seconds, "keep_count": keep_count, "keep_days": keep_days,
             "alert_after": alert_after, "scope": selection, "mode": mode, "compression": compression}
    if profile == "task-state":
        value.update({"profile": profile, "external_components": list(external_components),
                      "no_external_control_state": no_external_control_state})
        if media_policy == "verified-references":
            value["media_policy"] = media_policy
    return {**value, "policy_id": package.digest(value)}


def _validate(document: dict) -> None:
    if (document.get("format") != "maintenance-backup-policy/v1" or document.get("policy_id") != package.digest(
            {key: value for key, value in document.items() if key != "policy_id"})):
        raise ValueError("invalid automatic backup policy identity")
    rebuilt = policy(Path(document["source_root"]), Path(document["repository"]),
                     interval_seconds=document["interval_seconds"], keep_count=document["keep_count"],
                     keep_days=document["keep_days"], alert_after=document["alert_after"],
                     artifact_root=None if document["artifact_root"] is None else Path(document["artifact_root"]),
                     selection=document["scope"], mode=document["mode"], compression=document["compression"],
                     profile=document.get("profile", "archive"), external_components=document.get("external_components", ()),
                     no_external_control_state=document.get("no_external_control_state", False),
                     media_policy=document.get("media_policy", "local-required"))
    if rebuilt != document:
        raise ValueError("backup policy contains unsupported fields or paths")


def run_backup(document: dict, *, trigger: str = "manual", event_id: str | None = None,
               now: int | None = None, local_targets=None, remote_backends=None) -> dict:
    _validate(document)
    if document.get("media_policy") != "verified-references" and (local_targets or remote_backends):
        raise ValueError("target bindings require explicit verified-references task-state policy")
    if trigger not in {"manual", "time", "event"}:
        raise ValueError("unknown backup trigger")
    if trigger == "event" and (not isinstance(event_id, str) or not event_id or len(event_id) > 256):
        raise ValueError("event trigger requires a bounded durable event identity")
    timestamp = int(time.time()) if now is None else now
    if type(timestamp) is not int or timestamp < 0:
        raise ValueError("invalid backup run time")
    source, repository = Path(document["source_root"]), Path(document["repository"])
    package.absolute(repository)
    repository.mkdir(parents=True, exist_ok=True)
    state_root = repository / "runs" / document["policy_id"]
    package.absolute(state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    state_path = state_root / "state.json"
    with ArchiveSession(repository, mode=ArchiveAccessMode.MAINTENANCE).access():
        state = package._json(state_path) if state_path.exists() else {"last_success": None, "failures": 0, "events": [], "latest": None, "baseline": None}
        if trigger == "time" and state["last_success"] is not None and timestamp - state["last_success"] < document["interval_seconds"]:
            return {"operation": "maintenance-backup", "valid": True, "skipped": "not_due", "policy_id": document["policy_id"]}
        if trigger == "event":
            for existing_path in state_root.glob("*.json"):
                if existing_path.name == "state.json":
                    continue
                existing = package._json(existing_path)
                _verify_run_receipt(existing, document, existing_path.stem)
                if existing.get("status") == "success" and existing.get("event_id") == event_id:
                    return {"operation": "maintenance-backup", "valid": True, "skipped": "event_already_backed_up", "policy_id": document["policy_id"]}
        identity = package.digest({"policy": document["policy_id"], "timestamp": timestamp, "trigger": trigger, "event": event_id})
        receipt_path = state_root / (identity + ".json")
        if receipt_path.exists():
            existing = package._json(receipt_path)
            _verify_run_receipt(existing, document, identity)
            if (existing.get("run_id") != identity or existing.get("policy_id") != document["policy_id"]
                    or existing.get("policy") != document or (existing.get("receipt_id") is not None and
                    existing["receipt_id"] != package.digest({k: v for k, v in existing.items() if k != "receipt_id"}))):
                raise ValueError("backup run receipt identity changed")
            if existing.get("status") == "success":
                verified = _verify_backup(repository / existing["snapshot"], document)
                _verify_profile(document, verified)
                return {**existing, "reused": True}
        output = repository / (datetime.fromtimestamp(timestamp, UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + identity[:16] + ".zip")
        start = {"operation": "maintenance-backup", "format": "maintenance-backup-run/v1", "run_id": identity,
                 "policy_id": document["policy_id"], "trigger": trigger, "event_id": event_id,
                 "started_at": timestamp, "snapshot": output.name, "status": "running", "policy": document}
        _save(receipt_path, start)
        try:
            if document.get("profile") == "task-state":
                checkpoint, verification, cleaned = _run_task_state(document, start, state, state_root, output, timestamp,
                                                                   local_targets, remote_backends)
                rollover = False
            else:
                checkpoint, verification, cleaned, rollover = _run_archive(document, source, repository, state, output)
            state.update({"last_success": timestamp, "failures": 0, "latest": output.name})
            if event_id is not None:
                state["events"] = [*state["events"], event_id][-10000:]
            result = {**start, "status": "success", "valid": True, "checkpoint": checkpoint,
                      "rolled_full_baseline": rollover,
                      "verification": verification, "restoration_verified": True,
                      "verification_kind": "detached-chain-materialization-database-contract-fk-typed-rows-file-hashes",
                      "notification": "backup_success", "retention": cleaned}
            if document.get("profile") == "task-state":
                from bili_asr.services.maintenance_media import media_availability
                result.update({"restoration_verified": False, "state_evidence_complete": True,
                               "media_self_contained": False, "worker_started": False,
                               **media_availability(verification["manifest"]),
                               "verification": {"valid": True, "manifest_id": package.digest(verification["manifest"]),
                                                "profile": "task-state", "state_evidence_complete": True},
                               "verification_kind": "sqlite-consistent-readonly-state-and-observed-component-evidence"})
            if document["scope"] is not None and document["scope"]["profile"] == "publication":
                result.update({"restoration_verified": False, "database_recoverable": False,
                               "verification_kind": "public-projection-rights-gated-export-and-file-hashes"})
            _save(state_path, state)
        except (ValueError, OSError, sqlite3.Error, KeyError, zipfile.BadZipFile) as exc:
            state["failures"] += 1
            result = {**start, "status": "failed", "valid": False, "error": str(exc),
                      "consecutive_failures": state["failures"], "restoration_verified": False,
                      "notification": "backup_failure_alert" if state["failures"] >= document["alert_after"] else "backup_failure"}
            _save(state_path, state)
        result["receipt_id"] = package.digest(result)
        _save(receipt_path, result)
        if document.get("profile") == "task-state" and result["status"] == "success":
            # Publish the completed receipt and last-success pointer before
            # pruning. A crash can leave excess backups, never a deleted last
            # successful point paired with a failed current invocation.
            from bili_asr.services.maintenance_state_retention import (
                retain_state_packages,
            )
            try:
                completed = backup_status(repository, document["policy_id"])["runs"]
                result["retention"] = retain_state_packages(repository, document, completed, now=timestamp)
            except (ValueError, OSError, sqlite3.Error, KeyError, zipfile.BadZipFile) as exc:
                result["retention"] = {"valid": False, "error": str(exc), "business_rows_changed": False}
                result["notification"] = "backup_retention_failure"
            result["receipt_id"] = package.digest({key: value for key, value in result.items() if key != "receipt_id"})
            _save(receipt_path, result)
    return result


def _run_archive(document, source, repository, state, output):
    with ArchiveSession(source, mode=ArchiveAccessMode.MAINTENANCE).access():
        checkpoint = checkpoint_archive(source)
        inventory, _ = package._inventory(source, source if document["artifact_root"] is None else Path(document["artifact_root"]))
        package._free_space(output, sum(item["size"] for item in inventory["files"]) * 3 + 64 * 1024**2)
        baseline = state["baseline"] if document["mode"] == "differential" else state["latest"]
        rollover = False
        if baseline is not None and document["mode"] == "incremental":
            from bili_asr.services.maintenance_chain import MAX_CHAIN_POINTS
            rollover = len(verify_chain(repository / baseline)["chain"]) >= MAX_CHAIN_POINTS
        if output.exists():
            # An interrupted run may already have atomically published
            # its exact content. Verify it, never overwrite it.
            verification = verify_chain(output)
            _verify_profile(document, verification)
            if verification.get("source_fingerprint") is not None and verification["source_fingerprint"] != inventory["fingerprint"]:
                raise ValueError("existing backup source fingerprint differs from the current frozen source")
        else:
            if document["mode"] == "full" or baseline is None or rollover:
                if document["scope"] is not None and document["scope"]["profile"] == "publication":
                    from bili_asr.services.maintenance_publication import (
                        save_public_backup,
                    )
                    save_public_backup(source, output, artifact_root=None if document["artifact_root"] is None else Path(document["artifact_root"]))
                else:
                    package.export_package(source, output, artifact_root=None if document["artifact_root"] is None else Path(document["artifact_root"]), scope=document["scope"])
                state["baseline"] = output.name
            else:
                save_delta(source, output, repository / baseline,
                           artifact_root=None if document["artifact_root"] is None else Path(document["artifact_root"]),
                           scope=document["scope"], compression=document["compression"])
            verification = verify_chain(output)
            _verify_profile(document, verification)
    retention = retention_plan(repository, keep_count=document["keep_count"], keep_days=document["keep_days"])
    # Protect the differential baseline even when a full future point
    # is selected by retention; subsequent scheduled runs depend on it.
    if state["baseline"]:
        for item in retention["delete"]:
            if item["path"] == state["baseline"]:
                retention["delete"].remove(item)
                retention["protected_dependencies"].append(item["path"])
                retention["selected_restore_points"].append(item["path"])
                retention["plan_id"] = package.digest({k: v for k, v in retention.items() if k != "plan_id"})
                break
    cleaned = apply_retention(retention)
    return checkpoint, verification, cleaned, rollover

def _verify_backup(path: Path, document: dict) -> dict:
    if document.get("profile") == "task-state":
        from bili_asr.services.maintenance_task_state import verify_task_state
        return verify_task_state(path)
    return verify_chain(path)


def _run_task_state(document, start, state, state_root, output, timestamp, local_targets=None, remote_backends=None):
    from bili_asr.services.maintenance_task_state import save_task_state
    prior = []
    for path in sorted(state_root.glob("*.json")):
        if path.name == "state.json" or path.stem == start["run_id"]:
            continue
        value = package._json(path)
        _verify_run_receipt(value, document, path.stem)
        prior.append(value)
    job_state = {"format": "backup-job-state/v1", "policy": document, "current_run": start,
                 "prior_runs": prior, "state": dict(state)}
    if not output.exists():
        save_task_state(Path(document["source_root"]), output,
                        artifact_root=None if document["artifact_root"] is None else Path(document["artifact_root"]),
                        external_components=document["external_components"],
                        no_external_control_state=document["no_external_control_state"], backup_job_state=job_state,
                        media_policy=document.get("media_policy", "local-required"),
                        local_targets=local_targets, remote_backends=remote_backends)
    verification = _verify_backup(output, document)
    _verify_profile(document, verification)
    if verification["manifest"]["backup_job_state"]["current_run"] != start:
        raise ValueError("published task-state backup belongs to a different invocation")
    cleaned = {"operation": "task-state-retention", "valid": True, "status": "pending_completed_receipt"}
    return {"operation": "task-state-consistent-observation", "valid": True,
            "wal_truncated": False, "business_rows_changed": False}, verification, cleaned


def _verify_profile(document: dict, verified: dict) -> None:
    if document.get("profile") == "task-state":
        manifest = verified["manifest"]
        if (manifest.get("profile") != "task-state"
                or not isinstance(manifest.get("backup_job_state"), dict)
                or manifest["backup_job_state"].get("policy") != document):
            raise ValueError("task-state package does not match its policy")
        from bili_asr.services.maintenance_task_state import FORMAT, REFERENCE_FORMAT
        expected_format = REFERENCE_FORMAT if document.get("media_policy") == "verified-references" else FORMAT
        if manifest["format"] != expected_format:
            raise ValueError("task-state media version does not match its explicit policy")
        return
    public = document["scope"] is not None and document["scope"]["profile"] == "publication"
    if public != (verified.get("database_recoverable") is False):
        raise ValueError("backup container profile does not match its public/private policy")
    if not public:
        specification = document["scope"]
        if specification is None and verified.get("selection", {}).get("mode") != "full":
            raise ValueError("backup scope is not the policy's full private history")
        if specification is not None and verified.get("selection", {}).get("scope") != specification:
            raise ValueError("backup container scope differs from the frozen policy selectors")


def _verify_run_receipt(value: dict, document: dict, identity: str) -> None:
    if (value.get("format") != "maintenance-backup-run/v1" or value.get("run_id") != identity
            or value.get("policy_id") != document["policy_id"] or value.get("policy") != document
            or value.get("status") not in {"running", "success", "failed"}
            or value.get("run_id") != package.digest({"policy": document["policy_id"], "timestamp": value.get("started_at"),
                    "trigger": value.get("trigger"), "event": value.get("event_id")})):
        raise ValueError("backup run receipt policy or invocation identity changed")
    if value["status"] != "running" and value.get("receipt_id") != package.digest({k: v for k, v in value.items() if k != "receipt_id"}):
        raise ValueError("completed backup run receipt identity changed")


def backup_status(repository: Path, policy_id: str) -> dict:
    root = package.absolute(repository)
    if not package._HASH.fullmatch(policy_id):
        raise ValueError("invalid backup policy identity")
    directory = root / "runs" / policy_id
    runs = []
    for path in sorted(directory.glob("*.json")):
        if path.name == "state.json":
            continue
        value = package._json(path)
        if value.get("receipt_id") is not None and value["receipt_id"] != package.digest({k: v for k, v in value.items() if k != "receipt_id"}):
            raise ValueError("backup run receipt changed")
        runs.append(value)
    return {"operation": "maintenance-backup-status", "valid": True, "policy_id": policy_id, "runs": runs}
