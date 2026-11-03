"""Manual, maintenance-window audio offload with durable per-copy release intents."""
from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath

from bili_asr.archive_maintenance import archive_access
from bili_asr.archive_session import ArchiveAccessMode, open_archive_connection
from bili_asr.artifact_inventory import portable_artifact_parts
from bili_asr.artifact_packages import (
    PackageSource,
    capture_source_generation,
    check_artifact_package,
    copy_package_object,
    create_artifact_package,
    package_batches,
    package_manifest,
)
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.services.artifact_consumer_graph import (
    consumer_job_ids,
    has_live_consumer,
)
from bili_asr.services.artifact_inventory_service import (
    ArtifactSelection,
    COPY_PLAN_SCHEMA,
    PLAN_SCHEMA,
    copy_candidate_inventory,
    inventory_artifacts,
    validate_offload_plan,
)
from bili_asr.services.artifact_release import release_copy
from bili_asr.storage.artifact_catalog import ArtifactCatalog
from bili_asr.storage.artifact_releases import ArtifactReleaseRepository
from bili_asr.storage_targets import open_directory_target


def _local_target(roots: ArtifactRoots, root: Path) -> str:
    if root not in roots.read_bases():
        raise ValueError("plan source root is outside this invocation's artifact scope")
    return "local" if root == roots.archive_root else "local-artifacts"


def _selection(plan: dict) -> ArtifactSelection:
    return ArtifactSelection(**{key: tuple(value) for key, value in plan["selection"].items()})


def _quarantine_key(path, operation_id, copy_id):
    return str(PurePosixPath(path).parent / f".artifact-release-{operation_id}-{copy_id}")


def _inventory(roots, plan, external_holds):
    holds = external_holds() if callable(external_holds) else external_holds
    return inventory_artifacts(roots.archive_root / "archive.db", roots, deep=True,
                              selection=_selection(plan), external_holds=holds)


def _revalidate(roots, plan, external_holds, items) -> dict:
    current = _inventory(roots, plan, external_holds)
    if plan["schema"] == COPY_PLAN_SCHEMA:
        current = copy_candidate_inventory(current)
    copies = {copy["copy_id"]: copy for copy in current["copies"]}
    for item in items:
        copy = copies.get(item["copy_id"])
        if copy is None or not copy["candidate"] or any(copy[key] != item[key] for key in ("sha256", "size", "fingerprint")):
            raise ValueError(f"plan source changed or acquired a retention guard: {item['copy_id']}")
    return current


def _local_replica(catalog, roots, item):
    target_id = _local_target(roots, Path(item["root"]))
    catalog.register_target(target_id, kind="local")
    row = catalog.connection.execute(
        "SELECT replica_id,object_id,generation,presence FROM artifact_replicas WHERE target_id=? AND relative_key=? AND member_key='' ORDER BY generation DESC LIMIT 1",
        (target_id, item["path"]),
    ).fetchone()
    generation = row[2] if row and row[1] == item["sha256"] and row[3] == "present" else (row[2] + 1 if row else 1)
    return catalog.record_verified_replica(item["sha256"], target_id, item["path"], sha256=item["sha256"],
                                           byte_size=item["size"], generation=generation)


def _require_complete_text_groups(catalog, items):
    identities = {item["sha256"] for item in items}
    for item in items:
        if item["path"].startswith("audio/"):
            continue
        groups = [row[0] for row in catalog.connection.execute(
            "SELECT p.group_id FROM artifact_group_paths p JOIN artifact_group_members m USING(group_id,role) "
            "WHERE m.object_id=? AND (p.relative_key=? OR ?='documents/artifact-objects/'||m.object_id)",
            (item["sha256"], item["path"], item["path"]))]
        if not groups:
            raise ValueError("text artifact requires a captured immutable group before transfer")
        for group in groups:
            members = {row[0] for row in catalog.connection.execute("SELECT object_id FROM artifact_group_members WHERE group_id=?", (group,))}
            if not members <= identities:
                raise ValueError("text transfer must include every member of its complete group")


def _register_package(catalog, target, checked, operation_id, roots, items):
    members = {entry["object_id"]: entry for entry in checked["objects"]}
    with catalog.connection:
        catalog.record_package(checked["package_id"], target.target_id, checked["package_key"],
                               sha256=checked["package_sha256"], byte_size=checked["package_size_bytes"],
                               manifest_sha256=checked["manifest_sha256"])
        for item in items:
            identity = catalog.register_object(item["sha256"], item["size"])
            for audio_id, in catalog.connection.execute("SELECT audio_id FROM audio_objects WHERE sha256=? AND byte_size=?", (identity, item["size"])):
                catalog.bind_audio(audio_id, identity)
            source_id = _local_replica(catalog, roots, item)
            entry = members[identity]
            replica_id = catalog.record_verified_replica(identity, target.target_id, checked["package_key"],
                                                         sha256=identity, byte_size=item["size"],
                                                         package_id=checked["package_id"], member_key=entry["member"])
            catalog.record_transfer_item(operation_id, identity, state="verified", source_replica_id=source_id, target_replica_id=replica_id)
        catalog.set_transfer_state(operation_id, "verified")


def _package_evidence(catalog, target, operation_id):
    rows = list(catalog.connection.execute(
        "SELECT DISTINCT p.* FROM artifact_transfer_items i JOIN artifact_replicas r ON r.replica_id=i.target_replica_id "
        "JOIN artifact_packages p ON p.package_id=r.package_id AND p.target_id=r.target_id AND p.relative_key=r.relative_key "
        "WHERE i.transfer_id=?", (operation_id,)))
    identities = set()
    for row in rows:
        if row["target_id"] != target.target_id:
            raise ValueError("operation belongs to another target")
        target.check()
        checked = check_artifact_package(target.root / row["relative_key"], expected_sha256=row["sha256"])
        if checked["package_id"] != row["package_id"] or checked["manifest_sha256"] != row["manifest_sha256"]:
            raise ValueError("catalog and package manifest identity differ")
        identities.update(entry["object_id"] for entry in checked["objects"])
    return identities


def _reusable_package(catalog, target, batch, version, level):
    if version == 1:
        return None
    rows = catalog.connection.execute(
        "SELECT DISTINCT p.* FROM artifact_packages p JOIN artifact_replicas r "
        "ON p.package_id=r.package_id AND p.target_id=r.target_id AND p.relative_key=r.relative_key "
        "WHERE r.object_id=? AND r.target_id=? AND r.presence='present' ORDER BY p.verified_at DESC",
        (batch[0].object_id, target.target_id))
    for row in rows:
        target.check()
        checked = check_artifact_package(target.root / row["relative_key"], expected_sha256=row["sha256"])
        if checked["format_version"] != version or checked.get("compression", {}).get("level") != level:
            continue
        members = {entry["object_id"]: entry for entry in checked["objects"]}
        if all(source.object_id in members and members[source.object_id]["size_bytes"] == source.size_bytes for source in batch):
            return checked
    return None


def _result(connection, operation_id, *, released_bytes=0):
    operation = dict(connection.execute("SELECT * FROM artifact_transfers WHERE transfer_id=?", (operation_id,)).fetchone())
    counts = dict(connection.execute("SELECT state,COUNT(*) FROM artifact_transfer_items WHERE transfer_id=? GROUP BY state", (operation_id,)))
    packages = list(connection.execute(
        "SELECT DISTINCT p.relative_key,p.byte_size FROM artifact_transfer_items i JOIN artifact_replicas r ON r.replica_id=i.target_replica_id "
        "JOIN artifact_packages p ON p.package_id=r.package_id AND p.target_id=r.target_id AND p.relative_key=r.relative_key "
        "WHERE i.transfer_id=?", (operation_id,)))
    return {"operation": operation, "items_by_state": counts, "packages": [dict(row) for row in packages],
            "verified_payload_bytes": connection.execute(
                "SELECT COALESCE(SUM(o.byte_size),0) FROM artifact_transfer_items i JOIN artifact_objects o USING(object_id) WHERE i.transfer_id=? AND i.target_replica_id IS NOT NULL", (operation_id,)).fetchone()[0],
            "released_bytes_this_run": released_bytes,
            "released_copies": connection.execute("SELECT COUNT(*) FROM artifact_release_intents WHERE transfer_id=? AND state='released'", (operation_id,)).fetchone()[0]}


def _live_hold(connection, obj, external_holds):
    holds = external_holds() if callable(external_holds) else external_holds
    if holds is None:
        return True
    parts = {ref["part_id"] for ref in obj["references"] if ref["part_id"] is not None}
    keys = {obj["object_id"], *(f"part:{part}" for part in parts),
            *(f"path:{ref['path']}" for ref in obj["references"])}
    for part in parts:
        keys.update(consumer_job_ids(connection, part))
    return any(holds.get(key) for key in keys)


def _live_consumers(connection, obj):
    parts = {ref["part_id"] for ref in obj["references"] if ref["part_id"] is not None}
    for part in parts:
        if has_live_consumer(connection, part):
            return True
    return False


def _recheck_target_object(catalog, target, operation_id, identity):
    class Discard:
        def write(self, block):
            return len(block)
    row = catalog.connection.execute(
        "SELECT r.*,p.manifest_sha256 FROM artifact_transfer_items i JOIN artifact_replicas r ON r.replica_id=i.target_replica_id "
        "JOIN artifact_packages p ON p.package_id=r.package_id AND p.target_id=r.target_id AND p.relative_key=r.relative_key "
        "WHERE i.transfer_id=? AND i.object_id=?", (operation_id, identity)).fetchone()
    if row is None or row["target_id"] != target.target_id:
        raise ValueError("release requires a current verified external object")
    target.check()
    path = target.root / row["relative_key"]
    generation = capture_source_generation(path)
    copy_package_object(path, identity, Discard(),
                        expected_size=row["verified_byte_size"], package_id=row["package_id"],
                        manifest_sha256=row["manifest_sha256"])
    target.check()
    if capture_source_generation(path) != generation:
        raise ValueError("target generation changed during release verification")
    return path, generation


def _require_release_authority(catalog, plan, operation_id, *, mode):
    if mode != "offload" or plan["schema"] != PLAN_SCHEMA:
        raise ValueError("copy transfer cannot execute release intents")
    operation = catalog.connection.execute(
        "SELECT kind,plan_sha256,target_id FROM artifact_transfers WHERE transfer_id=?", (operation_id,)).fetchone()
    if (operation is None or operation["kind"] != "offload" or operation["plan_sha256"] != plan["plan_sha256"]
            or operation["target_id"] != plan["target_id"]):
        raise ValueError("release intent lacks matching persisted offload authority")


def _release_intents(catalog, roots, target, plan, operation_id, external_holds, *, mode, current_inventory=None, policy_guard=None,
                     release_verifier=None, release_progress=None):
    _require_release_authority(catalog, plan, operation_id, mode=mode)
    identities = _package_evidence(catalog, target, operation_id)
    current = current_inventory if current_inventory is not None else _inventory(roots, plan, external_holds)
    retention = {obj["sha256"]: obj["retention_reasons"] for obj in current["objects"] if obj["sha256"]}
    object_facts = {obj["sha256"]: obj for obj in current["objects"] if obj["sha256"]}
    copies = {copy["copy_id"]: copy for copy in current["copies"]}
    planned = {item["copy_id"]: item for item in plan["items"]}
    released_bytes = 0
    for intent in list(catalog.connection.execute("SELECT * FROM artifact_release_intents WHERE transfer_id=? ORDER BY copy_id", (operation_id,))):
        if intent["state"] == "released":
            continue
        item = planned.get(intent["copy_id"])
        if item is None or intent["object_id"] != item["sha256"] or intent["source_key"] != item["path"] or intent["quarantine_key"] != _quarantine_key(item["path"], operation_id, intent["copy_id"]):
            raise ValueError("release intent differs from frozen plan")
        root = Path(item["root"])
        if _local_target(roots, root) != intent["source_target_id"] or intent["object_id"] not in identities:
            raise ValueError("release intent lacks verified target evidence")
        copy = copies.get(intent["copy_id"])
        reasons = set(retention.get(intent["object_id"], ()))
        if "complete_group_unverified" in reasons:
            groups = [row[0] for row in catalog.connection.execute("SELECT group_id FROM artifact_group_members WHERE object_id=?", (intent["object_id"],))]
            if groups and all({row[0] for row in catalog.connection.execute("SELECT object_id FROM artifact_group_members WHERE group_id=?", (group,))} <= identities for group in groups):
                reasons.discard("complete_group_unverified")
        held = (external_holds is None or intent["object_id"] not in retention
                or bool(reasons) or catalog.pinned(intent["object_id"]))
        if copy and copy["state"] not in {"missing", "verified"}:
            held = True
        if copy and copy["state"] == "verified" and copy["sha256"] != intent["object_id"]:
            raise ValueError("release source was replaced; preserve source and quarantine for inspection")
        target.check()

        def isolated(copy_id=intent["copy_id"], identity=intent["object_id"]):
            with catalog.connection:
                ArtifactReleaseRepository(catalog.connection).isolate(operation_id, copy_id)

        verified_target = []
        replica_guard = []
        def before_delete(identity=intent["object_id"], evidence=verified_target, replicas=replica_guard):
            evidence[:] = _recheck_target_object(catalog, target, operation_id, identity)
            if release_verifier is not None:
                replicas[:] = [release_verifier(catalog.connection, identity)]

        @contextmanager
        def deletion_guard(identity=intent["object_id"], evidence=verified_target, replicas=replica_guard):
            # Hashing has finished. Serialize the final queue/pin observation
            # and short filesystem commit against newly enqueued consumers.
            catalog.connection.execute("BEGIN IMMEDIATE")
            try:
                _require_release_authority(catalog, plan, operation_id, mode=mode)
                if policy_guard is not None:
                    policy_guard()
                if (catalog.pinned(identity) or _live_hold(catalog.connection, object_facts[identity], external_holds)
                        or _live_consumers(catalog.connection, object_facts[identity])):
                    raise ValueError("release acquired a retention guard after isolation")
                target.check()
                if not evidence or capture_source_generation(evidence[0]) != evidence[1]:
                    raise ValueError("verified target generation changed before deletion")
                if replicas:
                    replicas[0]()
                yield
            finally:
                catalog.connection.rollback()

        outcome = release_copy(root, intent["source_key"], intent["quarantine_key"], intent["object_id"],
                               json.loads(intent["source_generation_json"]), allow_delete=not held,
                               isolated=isolated, before_delete=before_delete, deletion_guard=deletion_guard)
        released_bytes += outcome["released_bytes"]
        if release_progress is not None:
            release_progress(outcome)
        with catalog.connection:
            ArtifactReleaseRepository(catalog.connection).record_outcome(operation_id, intent["copy_id"], outcome["state"])
            if outcome["state"] == "released":
                catalog.observe_replica(intent["source_replica_id"], presence="released")
    with catalog.connection:
        ArtifactReleaseRepository(catalog.connection).complete_released_items(operation_id)
        cancelled = catalog.connection.execute("SELECT 1 FROM artifact_release_intents WHERE transfer_id=? AND state='cancelled'", (operation_id,)).fetchone()
        catalog.set_transfer_state(operation_id, "failed" if cancelled else "complete", error_code="retention_guard" if cancelled else None)
    return _result(catalog.connection, operation_id, released_bytes=released_bytes)


@contextmanager
def _transfer_fences(roots, plan, *, online, external_holds, mode):
    with ExitStack() as resources:
        resources.enter_context(archive_access(roots.archive_root, exclusive=not online, create_root=False))
        if online:
            from bili_asr.services.artifact_coordination import (
                object_fence,
                resource_fence,
            )
            # Publication slots are mutable; fence their owner as well as bytes.
            observed = _inventory(roots, plan, external_holds)
            selected = {item["sha256"] for item in plan["items"]}
            parts = {ref["part_id"] for obj in observed["objects"] if obj["sha256"] in selected
                     for ref in obj["references"] if ref["kind"] != "audio" and ref["part_id"] is not None}
            for part in sorted(parts):
                resources.enter_context(resource_fence(roots, f"publication-part:{part}", exclusive=True))
            for identity in sorted(selected):
                resources.enter_context(object_fence(roots, identity, exclusive=mode != "copy"))
        yield


def transfer_artifacts(roots: ArtifactRoots, plan: dict, *, target_root: Path, mode: str = "copy",
                       external_holds=None, max_bytes: int = 1024**3, max_objects: int = 1000,
                       _online: bool = False, _policy_guard=None, _package_version: int = 1,
                       _compression_level: int = 6, _release_verifier=None, _release_progress=None) -> dict:
    """Execute a frozen audio plan with exclusive archive access and bounded batches."""
    validate_offload_plan(plan)
    if mode not in {"copy", "offload"}:
        raise ValueError("transfer mode must be copy or offload")
    if mode == "offload" and plan["schema"] == COPY_PLAN_SCHEMA:
        raise ValueError("copy plan cannot authorize source release")
    for item in plan["items"]:
        _local_target(roots, Path(item["root"]))
        portable_artifact_parts(item["path"])
    if not plan["items"]:
        raise ValueError("plan contains no eligible artifact copies")
    target = open_directory_target(target_root, plan["target_id"], roots=roots)
    package_options = {"format_version": _package_version, "compression_level": _compression_level}
    operation_id = f"{mode}-{plan['plan_sha256']}"
    if _package_version != 1:
        operation_id += f"-v{_package_version}-deflate{_compression_level}"
    with _transfer_fences(roots, plan, online=_online, external_holds=external_holds, mode=mode):
        connection = open_archive_connection(roots.archive_root, mode=ArchiveAccessMode.WRITE, artifact_roots=roots)
        try:
            if _online:
                from bili_asr.storage.artifact_online import require_artifact_online
                require_artifact_online(connection, required=True)
            catalog = ArtifactCatalog(connection)
            _require_complete_text_groups(catalog, plan["items"])
            existing = connection.execute("SELECT state,kind FROM artifact_transfers WHERE transfer_id=?", (operation_id,)).fetchone()
            if existing and existing["kind"] != mode:
                raise ValueError("persisted transfer kind differs from requested mode")
            if connection.execute("SELECT 1 FROM artifact_release_intents WHERE transfer_id=?", (operation_id,)).fetchone():
                return _release_intents(catalog, roots, target, plan, operation_id, external_holds, mode=mode, policy_guard=_policy_guard,
                                        release_verifier=_release_verifier, release_progress=_release_progress)
            if existing and existing["state"] == "complete":
                _package_evidence(catalog, target, operation_id)
                return _result(connection, operation_id)
            _revalidate(roots, plan, external_holds, plan["items"])
            if mode == "offload" and any(catalog.pinned(item["sha256"]) for item in plan["items"]):
                raise ValueError("selected audio is pinned")
            sources = {}
            for item in plan["items"]:
                path = Path(item["root"]) / item["path"]
                sources.setdefault(item["sha256"], PackageSource(item["sha256"], path, item["path"], item["sha256"], item["size"], capture_source_generation(path)))
            batches = package_batches(sources.values(), max_bytes=max_bytes, max_objects=max_objects)
            reusable = {index: _reusable_package(catalog, target, batch, _package_version, _compression_level)
                        for index, batch in enumerate(batches)}
            missing_batches = [batch for index, batch in enumerate(batches) if reusable[index] is None and not (
                target.root / "packages" / f"artifact-{package_manifest(batch, operation_id, plan['plan_sha256'], index, **package_options)['package_id']}.zip"
            ).exists()]
            missing_objects = [source for batch in missing_batches for source in batch]
            payload = sum(source.size_bytes for source in missing_objects)
            # DEFLATE can expand incompressible input. Reserve a deliberately
            # conservative 1% beyond payload, separately from ZIP/manifest cost.
            expansion = (payload + 99) // 100 if _package_version == 2 else 0
            needed = payload + expansion + max(64 * 1024**2, len(missing_objects) * 4096)
            if missing_batches and shutil.disk_usage(target.root).free < needed:
                raise ValueError("storage target lacks space for payload and package overhead")
            with connection:
                catalog.register_target(target.target_id)
                catalog.begin_transfer(operation_id, kind=mode, plan_sha256=plan["plan_sha256"], target_id=target.target_id)
                catalog.set_transfer_state(operation_id, "copying")
            try:
                for index, batch in enumerate(batches):
                    if _policy_guard is not None:
                        _policy_guard()
                    target.check()
                    checked = reusable[index] or create_artifact_package(target.root, batch, operation_id=operation_id,
                                                      plan_sha256=plan["plan_sha256"], batch_index=index, **package_options)
                    selected = [item for item in plan["items"] if item["sha256"] in {source.object_id for source in batch}]
                    _register_package(catalog, target, checked, operation_id, roots, selected)
                if mode == "copy":
                    with connection:
                        catalog.set_transfer_state(operation_id, "complete")
                    return _result(connection, operation_id)
                # The expensive target copy/verification precedes this second live
                # inventory. Persist all intents before any source is isolated.
                current = _revalidate(roots, plan, external_holds, plan["items"])
                with connection:
                    for item in plan["items"]:
                        source_id = _local_replica(catalog, roots, item)
                        ArtifactReleaseRepository(connection).plan(transfer_id=operation_id,
                            copy_id=item["copy_id"], object_id=item["sha256"],
                            source_target_id=_local_target(roots, Path(item["root"])), source_key=item["path"],
                            quarantine_key=_quarantine_key(item["path"], operation_id, item["copy_id"]),
                            source_replica_id=source_id,
                            source_generation=capture_source_generation(Path(item["root"]) / item["path"]))
                    catalog.set_transfer_state(operation_id, "releasing")
                return _release_intents(catalog, roots, target, plan, operation_id, external_holds, mode=mode, current_inventory=current,
                                        policy_guard=_policy_guard, release_verifier=_release_verifier, release_progress=_release_progress)
            except (ValueError, OSError, sqlite3.Error):
                with connection:
                    catalog.set_transfer_state(operation_id, "failed", error_code="transfer_interrupted")
                raise
        finally:
            connection.close()


def reconcile_artifact_transfer(roots: ArtifactRoots, plan: dict, *, target_root: Path, external_holds=None) -> dict:
    """Resume a journaled offload without interpreting an old plan as permission."""
    validate_offload_plan(plan)
    if plan["schema"] == COPY_PLAN_SCHEMA:
        raise ValueError("copy plan cannot authorize source release")
    target = open_directory_target(target_root, plan["target_id"], roots=roots)
    operation_id = f"offload-{plan['plan_sha256']}"
    with archive_access(roots.archive_root, exclusive=True, create_root=False):
        connection = open_archive_connection(roots.archive_root, mode=ArchiveAccessMode.WRITE, artifact_roots=roots)
        try:
            catalog = ArtifactCatalog(connection)
            if not connection.execute("SELECT 1 FROM artifact_release_intents WHERE transfer_id=?", (operation_id,)).fetchone():
                raise ValueError("no persisted release intents; resume transfer with the same plan instead")
            return _release_intents(catalog, roots, target, plan, operation_id, external_holds, mode="offload")
        finally:
            connection.close()
