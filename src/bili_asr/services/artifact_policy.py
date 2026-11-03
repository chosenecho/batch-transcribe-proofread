"""Explicit, bounded online policy runs using the shared inventory and executor."""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
import uuid
from contextlib import ExitStack, contextmanager

from bili_asr.archive_maintenance import ArchiveBusyError
from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.canonical_json import digest
from bili_asr.services.artifact_coordination import (
    reconcile_consumer_pins,
    reserve_local_space,
    resource_fence,
)
from bili_asr.services.artifact_inventory_service import (
    ArtifactSelection,
    inventory_artifacts,
)
from bili_asr.services.artifact_io import artifact_io_budget
from bili_asr.services.artifact_policy_configuration import read_policy, validate_policy
from bili_asr.services.artifact_policy_defaults import DEFAULT_POLICY
from bili_asr.services.artifact_policy_planning import bounded_plan
from bili_asr.services.artifact_transfer import transfer_artifacts
from bili_asr.storage.artifact_catalog import ArtifactCatalog
from bili_asr.storage.artifact_online import require_artifact_online
from bili_asr.storage.artifact_policies import ArtifactPolicyRepository

__all__ = [
    "DEFAULT_POLICY",
    "configure_policy",
    "download_reservation",
    "minimum_free_space",
    "read_policy",
    "run_policy_once",
    "validate_policy",
]


def configure_policy(connection, policy_id: str, configuration: dict):
    require_artifact_online(connection, required=True)
    config = validate_policy(configuration)
    catalog = ArtifactCatalog(connection)
    if not isinstance(policy_id, str) or not policy_id or len(policy_id) > 128:
        raise ValueError("policy identity must be bounded nonempty text")
    with connection:
        if config["target_id"] is not None:
            catalog.register_target(config["target_id"])
        for identity in config.get("replica_target_ids", []):
            catalog.register_target(identity)
        ArtifactPolicyRepository(connection).configure(policy_id, config, now=int(time.time()))
    return {"policy_id": policy_id, "policy_sha256": digest(config), "configuration": config}


def minimum_free_space(connection):
    return max((validate_policy(json.loads(row[0]))["minimum_free_bytes"] for row in connection.execute(
        "SELECT config_json FROM artifact_policies WHERE mode!='off'")), default=0)


@contextmanager
def download_reservation(connection, roots, part_id, *, owner):
    policies = [validate_policy(json.loads(row[0])) for row in connection.execute("SELECT config_json FROM artifact_policies WHERE mode!='off'")]
    if not policies:
        yield
        return
    duration = connection.execute("SELECT duration_ms FROM video_parts WHERE video_part_id=?", (part_id,)).fetchone()
    if duration is None or duration[0] <= 0:
        raise ValueError("artifact_download_backpressure: unknown input duration")
    floor = max(policy["minimum_free_bytes"] for policy in policies)
    if any(shutil.disk_usage(roots.write_base).free < policy["trigger_free_bytes"] for policy in policies):
        raise ValueError("artifact_download_backpressure: storage pressure requires verified release first")
    needed = max((duration[0] * policy["download_bytes_per_second"] + 999) // 1000 * policy["download_workspace_multiplier"] for policy in policies)
    with reserve_local_space(connection, roots, needed, owner="download:" + owner, minimum_free_bytes=floor):
        yield


def run_policy_once(roots, *, policy_id="default", target_root=None, external_holds=None, storage_targets=None, checkpoint=None,
                    connection=None):
    """Run at most one frozen batch; callers may poll using the returned deadline."""
    from contextlib import nullcontext
    supplied_connection = connection
    session_context = (nullcontext(None) if supplied_connection is not None else
                       ArchiveSession(roots.archive_root, mode=ArchiveAccessMode.WRITE, artifact_roots=roots))
    with session_context as session:
        connection = supplied_connection if session is None else session.connection
        require_artifact_online(connection, required=True)
        config = read_policy(connection, policy_id)
        if config["version"] == 2:
            from bili_asr.services.audio_retention import run_audio_retention_once
            return run_audio_retention_once(roots, policy_id=policy_id, target_root=target_root,
                storage_targets=storage_targets, external_holds=external_holds, checkpoint=checkpoint,
                connection=connection)
        if config["mode"] == "off":
            return {"state": "off", "policy_id": policy_id, "released_bytes": 0}
        with ExitStack() as resources:
            for lane in range(config["max_concurrency"]):
                try:
                    resources.enter_context(resource_fence(roots, f"policy:{policy_id}:{lane}", exclusive=True))
                    break
                except ArchiveBusyError:
                    continue
            else:
                return {"state": "busy", "policy_id": policy_id, "released_bytes": 0}
            now = int(time.time())
            prior = connection.execute("SELECT * FROM artifact_policy_runs WHERE policy_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (policy_id,)).fetchone()
            if prior is not None and prior["policy_sha256"] == digest(config) and prior["retry_after"] > now:
                return {"state": "backoff", "policy_id": policy_id, "retry_after": prior["retry_after"], "released_bytes": 0}
            resume = (prior is not None and prior["policy_sha256"] == digest(config)
                      and prior["state"] in {"running", "failed"} and prior["plan_json"] is not None
                      and bool(json.loads(prior["plan_json"])["items"]))
            free = shutil.disk_usage(roots.write_base).free
            pressure = free < config["trigger_free_bytes"] or (prior is not None and prior["result_json"] is not None
                and json.loads(prior["result_json"]).get("pressure_active", False) and free < config["stop_free_bytes"])
            if config["mode"] == "offload" and not pressure and not resume:
                return {"state": "watermark_satisfied", "policy_id": policy_id, "free_bytes": free, "released_bytes": 0}
            frozen_hash, run_id = digest(config), uuid.uuid4().hex
            with connection:
                ArtifactPolicyRepository(connection).begin_run(run_id, policy_id, frozen_hash, config,
                    retry_after=now + config["scan_interval_seconds"], now=now)
            def guard():
                current = connection.execute("SELECT config_json FROM artifact_policies WHERE policy_id=?", (policy_id,)).fetchone()
                if current is None or digest(validate_policy(json.loads(current[0]))) != frozen_hash:
                    raise ValueError("artifact_policy_changed: frozen execution stopped at a safe boundary")
            report = {"policy_id": policy_id, "run_id": run_id, "policy_sha256": frozen_hash, "pressure_active": bool(pressure), "released_bytes": 0}
            try:
                reconcile_consumer_pins(connection, roots)
                measured = {"bytes_read": 0}
                def progress(event):
                    measured["bytes_read"] += event["bytes_read"] - measured.get(event["path"], 0)
                    measured[event["path"]] = event["bytes_read"]
                started = time.monotonic()
                inventory = inventory_artifacts(session.database_path, roots, deep=True,
                    selection=ArtifactSelection(**{key: tuple(values) for key, values in config["selection"].items()}),
                    external_holds=external_holds() if callable(external_holds) else external_holds,
                    progress=progress, max_bytes_per_second=config["bytes_per_second"] or None)
                plan = bounded_plan(inventory, config)
                if resume:
                    plan = json.loads(prior["plan_json"])  # Resume the exact journaled operation.
                report["scan"] = {"bytes_read": measured["bytes_read"], "elapsed_seconds": time.monotonic() - started}
                with connection:
                    ArtifactPolicyRepository(connection).freeze_plan(run_id, plan)
                if not plan["items"]:
                    report.update(state="blocked", reason="no_eligible_complete_group")
                else:
                    guard()
                    if target_root is None:
                        raise ValueError("artifact_target_unbound")
                    with artifact_io_budget(config["bytes_per_second"]) as meter:
                        result = transfer_artifacts(roots, plan, target_root=target_root, mode=config["mode"], external_holds=external_holds,
                            max_bytes=config["batch_bytes"], max_objects=config["batch_objects"], _online=True, _policy_guard=guard)
                    report.update(state="complete" if result["operation"]["state"] == "complete" else "blocked",
                                  transfer=result, io=meter.report(), released_bytes=result["released_bytes_this_run"])
                    if report["state"] == "blocked":
                        report["reason"] = "release_retention_guard"
                    report["pressure_active"] = pressure and shutil.disk_usage(roots.write_base).free < config["stop_free_bytes"]
            except (ValueError, OSError, sqlite3.Error) as error:
                report.update(state="failed", reason=str(error)[:512])
            retry_after = int(time.time()) + config["scan_interval_seconds" if report["state"] == "complete" else "backoff_seconds"]
            report["retry_after"] = retry_after
            with connection:
                ArtifactPolicyRepository(connection).finish_run(run_id, report, retry_after=retry_after, now=int(time.time()))
            return report
