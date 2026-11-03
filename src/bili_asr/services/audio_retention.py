"""Independent audio maintenance: verified compression before opt-in release."""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
import uuid
from contextlib import ExitStack

from bili_asr.archive_maintenance import ArchiveBusyError
from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.canonical_json import digest
from bili_asr.services.artifact_consumer_graph import (
    consumer_part_rows,
    latest_completion,
)
from bili_asr.services.artifact_coordination import (
    reconcile_consumer_pins,
    resource_fence,
)
from bili_asr.services.artifact_inventory_service import (
    ArtifactSelection,
    COPY_PLAN_SCHEMA,
    copy_candidate_inventory,
    inventory_artifacts,
    validate_offload_plan,
)
from bili_asr.services.artifact_io import artifact_io_budget
from bili_asr.services.artifact_transfer import transfer_artifacts
from bili_asr.services.audio_replica_requirement import AudioReplicaRequirement
from bili_asr.storage.artifact_policies import ArtifactPolicyRepository
from bili_asr.storage_targets import open_directory_target


def _clock_and_pending(connection, inventory):
    updated, pending = {}, set()
    for _job_id, part, status, timestamp, running in consumer_part_rows(connection):
        updated[part] = max(updated.get(part, 0), timestamp)
        if status != "succeeded" or running:
            pending.add(part)
    clocks, guarded = {}, set()
    for obj in inventory["objects"]:
        parts = {ref["part_id"] for ref in obj["references"] if ref["part_id"] is not None}
        completion = max((updated.get(part, 0) for part in parts), default=0)
        for copy_id in obj["copies"]:
            clocks[copy_id] = max(clocks.get(copy_id, 0), completion)
            if parts & pending:
                guarded.add(copy_id)
    copies = []
    for copy in inventory["copies"]:
        if copy["fingerprint"] is not None:
            clocks[copy["copy_id"]] = max(clocks.get(copy["copy_id"], 0), copy["fingerprint"][3] / 1e9)
        blockers = copy["blockers"] + (["audio_consumer_unfinished"] if copy["copy_id"] in guarded else [])
        copies.append({**copy, "blockers": sorted(set(blockers)), "candidate": copy["candidate"] and not blockers})
    return {**inventory, "copies": copies}, clocks


def _retarget(plan, target_id):
    body = {key: value for key, value in plan.items() if key != "plan_sha256"}
    body["target_id"] = target_id
    result = {**body, "plan_sha256": digest(body)}
    validate_offload_plan(result)
    return result


def _plans(connection, inventory, config, pressure, now):
    from bili_asr.services.artifact_policy_planning import bounded_plan
    release_inventory, clocks = _clock_and_pending(connection, inventory)
    # Stable content can be copied while queued/failed consumers retain the
    # source. Completion clocks and consumer guards apply only to release.
    targets = {config["target_id"], *config["replica_target_ids"]}
    recorded = {}
    for identity, target in connection.execute(
            "SELECT object_id,target_id FROM artifact_replicas "
            "WHERE presence='present' AND package_id IS NOT NULL"):
        recorded.setdefault(identity, set()).add(target)
    # Receipts only prioritize the bounded queue; they do not prove current
    # target availability or authorize deletion. Execution still rehashes them.
    missing = {obj["sha256"] for obj in inventory["objects"]
               if not targets <= recorded.get(obj["sha256"], set())}
    backup = bounded_plan(copy_candidate_inventory(inventory), config, purpose="copy", preferred_objects=missing)
    backed_up = {item["copy_id"] for item in backup["items"]}
    releases = []
    for copy in release_inventory["copies"]:
        blockers = list(copy["blockers"])
        if clocks.get(copy["copy_id"], now) > now - config["minimum_age_seconds"]:
            blockers.append("audio_minimum_age")
        if not pressure and clocks.get(copy["copy_id"], now) > now - config["local_retention_seconds"]:
            blockers.append("audio_local_retention_age")
        releases.append({**copy, "blockers": sorted(set(blockers)),
            "candidate": copy["candidate"] and not blockers and copy["copy_id"] in backed_up})
    release_inventory = {**release_inventory, "copies": releases}
    release = bounded_plan(release_inventory, config)
    return {"format": "audio-retention-plan/v1", "backup": backup, "release": release,
            "pressure_active": bool(pressure)}


def _local_bytes(inventory):
    return sum({copy["physical_id"]: copy["size"] for copy in inventory["copies"]
                if copy["path"].startswith("audio/") and copy["physical_id"] and copy["size"] is not None}.values())


def _release_verifier(requirement, config, plan):
    def verify(connection, identity):
        guard = requirement.verify(connection, identity)
        items = [item for item in plan["release"]["items"] if item["sha256"] == identity]
        def completion_guard():
            updated = latest_completion(connection, identity)
            observed = max(updated, *(item["fingerprint"][3] / 1e9 for item in items))
            age = config["minimum_age_seconds"] if plan["pressure_active"] else config["local_retention_seconds"]
            if observed > time.time() - age:
                raise ValueError("audio_retention_completion_age_changed")
            guard()
        completion_guard()
        return completion_guard
    return verify


def run_audio_retention_once(roots, *, policy_id, target_root, storage_targets, external_holds, checkpoint=None,
                             connection=None):
    """A bounded, journaled cycle runnable independently of inference workers."""
    from bili_asr.services.artifact_policy_configuration import read_policy
    from contextlib import nullcontext
    session_context = (nullcontext(None) if connection is not None else
                       ArchiveSession(roots.archive_root, mode=ArchiveAccessMode.WRITE, artifact_roots=roots))
    with session_context as session:
        connection = connection if session is None else session.connection
        config = read_policy(connection, policy_id)
        if config["mode"] == "off":
            return {"state": "off", "policy_id": policy_id, "released_bytes": 0}
        with ExitStack() as resources:
            try:
                resources.enter_context(resource_fence(roots, "policy:" + policy_id + ":0", exclusive=True))
            except ArchiveBusyError:
                return {"state": "busy", "policy_id": policy_id, "released_bytes": 0}
            now, frozen_hash = int(time.time()), digest(config)
            prior = connection.execute("SELECT * FROM artifact_policy_runs WHERE policy_id=? AND policy_sha256=? "
                "ORDER BY created_at DESC,rowid DESC LIMIT 1", (policy_id, frozen_hash)).fetchone()
            same_policy = prior is not None and prior["policy_sha256"] == frozen_hash
            if same_policy and prior["retry_after"] > now:
                return {"state": "backoff", "policy_id": policy_id, "retry_after": prior["retry_after"], "released_bytes": 0}
            resume = same_policy and prior["state"] in {"running", "failed"} and prior["plan_json"] is not None
            run_id = uuid.uuid4().hex
            repository = ArtifactPolicyRepository(connection)
            with connection:
                repository.begin_run(run_id, policy_id, frozen_hash, config,
                    retry_after=now + config["scan_interval_seconds"], now=now)

            def guard():
                if checkpoint is not None:
                    checkpoint()
                if digest(read_policy(connection, policy_id)) != frozen_hash:
                    raise ValueError("artifact_policy_changed: audio maintenance stopped at a safe boundary")

            report = {"policy_id": policy_id, "run_id": run_id, "policy_sha256": frozen_hash,
                "policy_version": 2, "delete_authorized": config["mode"] == "offload" and config["allow_delete"],
                "released_bytes": 0}
            frozen, meter = None, None
            try:
                guard()
                bindings = dict(storage_targets or {})
                if target_root is not None:
                    bindings[config["target_id"]] = target_root
                target_ids = (config["target_id"], *config["replica_target_ids"])
                targets = []
                for identity in target_ids:
                    if identity not in bindings:
                        raise ValueError("audio_replica_target_unbound: " + identity)
                    targets.append(open_directory_target(bindings[identity], identity, roots=roots))
                directories = {(target.device, target.inode) for target in targets}
                if len(directories) != len(targets):
                    raise ValueError("audio_replica_targets_are_not_distinct")
                for device, inode in sorted(directories):
                    resources.enter_context(resource_fence(roots, f"audio-target:{device}:{inode}", exclusive=True))
                reconcile_consumer_pins(connection, roots)
                requirement = AudioReplicaRequirement(roots, bindings, target_ids, config["minimum_verified_copies"])
                with artifact_io_budget(config["bytes_per_second"], checkpoint=guard) as meter:
                    seen = {}
                    def progress(event):
                        count = event["bytes_read"] - seen.get(event["path"], 0)
                        seen[event["path"]] = event["bytes_read"]
                        meter.observe(count)
                    started = time.monotonic()
                    database_path = roots.archive_root / "archive.db" if session is None else session.database_path
                    inventory = inventory_artifacts(database_path, roots, deep=True,
                        selection=ArtifactSelection(**{key: tuple(values) for key, values in config["selection"].items()}),
                        external_holds=external_holds() if callable(external_holds) else external_holds, progress=progress)
                    local_bytes, free = _local_bytes(inventory), shutil.disk_usage(roots.write_base).free
                    previous_pressure = same_policy and prior["result_json"] and json.loads(prior["result_json"]).get("pressure_active", False)
                    pressure = (local_bytes > config["maximum_local_audio_bytes"] or free < config["trigger_free_bytes"]
                        or previous_pressure and free < config["stop_free_bytes"])
                    frozen = json.loads(prior["plan_json"]) if resume else _plans(connection, inventory, config, pressure, time.time())
                    if frozen.get("format") != "audio-retention-plan/v1":
                        raise ValueError("unsupported persisted audio retention plan")
                    if frozen["backup"]["schema"] != COPY_PLAN_SCHEMA:
                        # Older frozen plans coupled copy admission to release.
                        # Restore journal-owned isolation before re-planning;
                        # never reinterpret an old release intent as a copy.
                        from bili_asr.services.audio_retention_recovery import restore_isolated_audio
                        restored = restore_isolated_audio(connection, roots, frozen["release"],
                            compression_level=config["compression_level"])
                        report["legacy_plan_replanned"] = frozen["backup"]["plan_sha256"]
                        if restored:
                            inventory = inventory_artifacts(database_path, roots, deep=True,
                                selection=ArtifactSelection(**{key: tuple(values) for key, values in config["selection"].items()}),
                                external_holds=external_holds() if callable(external_holds) else external_holds, progress=progress)
                        frozen = _plans(connection, inventory, config, pressure, time.time())
                    report.update(local_audio_bytes_before=local_bytes, free_bytes=free,
                        pressure_active=bool(pressure), scan={"bytes_read": sum(seen.values()), "elapsed_seconds": time.monotonic() - started})
                    report["copy_backlog"] = {"objects": len({item["sha256"] for item in frozen["backup"]["items"]}),
                        "bytes": frozen["backup"]["estimate"]["target_payload_bytes"], "held": frozen["backup"]["held"],
                        "scope": "selected_batch"}
                    report["copy_selection"] = dict(report["copy_backlog"])
                    report["release_blockers"] = frozen["release"]["held"]
                    payload = frozen["backup"]["estimate"]["target_payload_bytes"]
                    report["target_temporary_space_upper_bound_bytes"] = payload + (payload + 99) // 100 + max(
                        64 * 1024**2, len(frozen["backup"]["items"]) * 4096)
                    with connection:
                        repository.freeze_plan(run_id, frozen)
                    backup = frozen["backup"]
                    if not backup["items"]:
                        report.update(state="blocked", reason="no_eligible_audio", held=backup["held"])
                    else:
                        transfers = []
                        for target in targets:
                            guard()
                            transfers.append(transfer_artifacts(roots, _retarget(backup, target.target_id),
                                target_root=target.root, mode="copy", external_holds=external_holds,
                                max_bytes=config["batch_bytes"], max_objects=config["batch_objects"], _online=True,
                                _policy_guard=guard, _package_version=2, _compression_level=config["compression_level"]))
                        # All original bytes on all requested targets are checked
                        # before isolation, then again at each final deletion fence.
                        for identity in {item["sha256"] for item in backup["items"]}:
                            requirement.verify(connection, identity)()
                        report.update(state="complete", backups=transfers, object_receipts=[
                            {"object_id": identity, "original_byte_size": size, "verified_target_ids": list(target_ids)}
                            for identity, size in sorted({item["sha256"]: item["size"] for item in backup["items"]}.items())])
                        report["copy_backlog"].update(objects=0, bytes=0)
                        copied_ids = {item["copy_id"] for item in backup["items"]}
                        release_guarded = any(item["copy_id"] in copied_ids and reason != "audio_local_retention_age"
                            for item in frozen["release"]["held"] for reason in item["reasons"])
                        if report["delete_authorized"] and release_guarded and not frozen["release"]["items"]:
                            report.update(state="blocked", reason="audio_release_guarded")
                        if report["delete_authorized"] and frozen["release"]["items"]:
                            guard()
                            def released(outcome):
                                report["released_bytes"] += outcome["released_bytes"]
                            result = transfer_artifacts(roots, frozen["release"], target_root=targets[0].root,
                                mode="offload", external_holds=external_holds, max_bytes=config["batch_bytes"],
                                max_objects=config["batch_objects"], _online=True, _policy_guard=guard,
                                _package_version=2, _compression_level=config["compression_level"],
                                _release_verifier=_release_verifier(requirement, config, frozen), _release_progress=released)
                            report.update(release=result, released_bytes=result["released_bytes_this_run"],
                                state="complete" if result["operation"]["state"] == "complete" else "blocked")
                        report["compression"] = requirement.statistics()
                        report["local_audio_bytes_after"] = local_bytes - report["released_bytes"]
                    report["io"] = meter.report()
            except (ValueError, OSError, sqlite3.Error) as error:
                report.update(state="failed", reason=str(error)[:512])
            except KeyboardInterrupt:
                report.update(state="failed", reason="audio_retention_interrupted")
            if report["state"] == "failed" and frozen is not None:
                from bili_asr.services.audio_retention_recovery import (
                    restore_isolated_audio,
                )
                try:
                    report["restored_isolated_copies"] = restore_isolated_audio(connection, roots, frozen["release"],
                        compression_level=config["compression_level"])
                except (ValueError, OSError, sqlite3.Error) as error:
                    report["recovery_pending"] = type(error).__name__
            if meter is not None:
                report["io"] = meter.report()
            if frozen is not None:
                operation_id = f"offload-{frozen['release']['plan_sha256']}-v2-deflate{config['compression_level']}"
                report["release_receipts"] = [dict(row) for row in connection.execute(
                    "SELECT copy_id,object_id,source_key,state,updated_at FROM artifact_release_intents WHERE transfer_id=? ORDER BY copy_id",
                    (operation_id,))]
            retry_after = int(time.time()) + config["scan_interval_seconds" if report["state"] == "complete" else "backoff_seconds"]
            report["retry_after"] = retry_after
            with connection:
                repository.finish_run(run_id, report, retry_after=retry_after, now=int(time.time()))
            return report
