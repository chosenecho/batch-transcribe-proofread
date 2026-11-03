"""Versioned, explicitly non-self-contained state backups.

Offline validation verifies embedded bytes and catalog references only. It
never opens an external target, recovers jobs, clears holds, or starts workers.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
import zipfile
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

from bili_asr.archive_maintenance import archive_access
from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_inventory import (
    check_artifact_collisions,
    collect_artifacts,
    portable_artifact_parts,
    require_no_links,
    require_regular_file,
    stream_hash,
)
from bili_asr.artifact_packages import (
    _bounded_zip_directory,
    _open_regular,
    check_artifact_package,
)
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.contracts.json_schema import validate_json
from bili_asr.services.archive_snapshot import _check_bundle_markers, _empty_destination
from bili_asr.services.retry_control import capture_control, validate_control
from bili_asr.services.retry_maintenance_io import maintenance_lock
from bili_asr.storage.artifact_catalog import ArtifactCatalog
from bili_asr.storage.database import connect_database
from bili_asr.storage.snapshots import (
    create_database_snapshot,
    required_artifacts,
    validate_snapshot_database,
)
from bili_asr.storage_targets import (
    open_directory_target,
    sync_directory,
    unique_json_object,
)

FORMAT = "archive-reference-backup"
VERSION = 1
CONTRACT = "archive-reference-backup/v1"
MANIFEST = "reference-backup.json"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_REPLICA_FIELDS = ("target_id", "relative_key", "package_id", "member_key", "generation", "package_sha256",
                   "package_byte_size", "manifest_sha256")


class ReferenceBackupError(ValueError):
    pass


def _encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def operational_holds(value: dict | None) -> dict:
    """Whitelist one versioned control structure; arbitrary state is rejected."""
    if value is None:
        return {"verified": False, "holds": {}}
    if not isinstance(value, dict) or set(value) != {"version", "holds"} or type(value["version"]) is not int or value["version"] != 1 or not isinstance(value["holds"], dict):
        raise ReferenceBackupError("operational state must contain only version 1 and holds")
    holds = value["holds"]
    if len(_encoded(holds)) > 1024**2:
        raise ReferenceBackupError("operational holds exceed 1 MiB")
    for key, reasons in holds.items():
        if not isinstance(key, str) or not key or len(key) > 512 or any(char in key for char in "\r\n@?="):
            raise ReferenceBackupError("invalid operational hold identity")
        if not isinstance(reasons, list) or not reasons or any(not isinstance(reason, str) or not reason.strip() or len(reason) > 2048 or "://" in reason for reason in reasons):
            raise ReferenceBackupError("hold reasons must be bounded text without access URLs")
    return {"verified": True, "holds": holds}


def _dependency(catalog, key, identity):
    try:
        obj = catalog.object(identity)
    except ValueError:
        return None
    replicas = [{field: row[field] for field in _REPLICA_FIELDS} for row in catalog.replicas_for_object(identity)
                if row["package_id"] and row["presence"] not in {"released", "missing"}]
    if not replicas:
        return None
    return {"path": key, "object_id": identity, "size": obj["byte_size"], "replicas": replicas}


def _consumers(connection, dependencies):
    result = {}
    for dep in dependencies:
        rows = connection.execute(
            "SELECT DISTINCT j.job_id,j.kind,j.status FROM workflow_jobs j WHERE j.video_part_id IN ("
            "SELECT p.video_part_id FROM audio_objects a JOIN part_audio_objects p USING(audio_id) WHERE a.sha256=? "
            "UNION SELECT i.video_part_id FROM document_artifacts d JOIN editorial_revisions r USING(revision_id) "
            "JOIN editorial_inputs i USING(input_id) WHERE d.content_sha256=? "
            "UNION SELECT video_part_id FROM publication_releases WHERE artifact_sha256=?) ORDER BY j.job_id",
            (dep["object_id"],) * 3)
        jobs = {row["job_id"]: dict(row) for row in rows}
        for row in connection.execute(
            "SELECT DISTINCT j.job_id,j.kind,j.status FROM artifact_group_members m "
            "JOIN artifact_publication_groups g USING(group_id) JOIN workflow_publications p USING(publication_id) "
            "JOIN workflow_jobs j USING(video_part_id) WHERE m.object_id=? ORDER BY j.job_id", (dep["object_id"],)):
            jobs[row["job_id"]] = dict(row)
        result[dep["object_id"]] = [jobs[key] for key in sorted(jobs)]
    return result


def save_reference_backup(roots: ArtifactRoots, output: Path, *, holds: dict | None = None,
                          target_instances: dict[str, str] | None = None,
                          retry_control_path: Path | None = None, retry_control_lock: Path | None = None) -> dict:
    if ((retry_control_path is None) != (retry_control_lock is None)
            or retry_control_path is not None and holds is not None):
        raise ReferenceBackupError("retry controls require their declared coordination lock and exclude holds-only input")
    if retry_control_path is not None:
        retry_control_path, retry_control_lock = Path(retry_control_path).absolute(), Path(retry_control_lock).absolute()
        require_no_links(retry_control_path)
        require_no_links(retry_control_lock)
        if (retry_control_path == retry_control_lock or retry_control_lock.is_relative_to(roots.archive_root)
                or retry_control_path.exists() and retry_control_lock.exists() and retry_control_path.samefile(retry_control_lock)):
            raise ReferenceBackupError("control lock must be distinct and outside the archive")
    controls = operational_holds(holds)
    instances = dict(target_instances or {})
    if any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", key)
           or not isinstance(value, str) or not re.fullmatch("[0-9a-f]{32}", value) for key, value in instances.items()):
        raise ReferenceBackupError("target instances require logical identities and persistent instance IDs")
    output = Path(os.path.abspath(output))
    require_no_links(output)
    if output.exists() or any(output.is_relative_to(base) for base in roots.read_bases()):
        raise ReferenceBackupError("backup output must be new and outside both archive roots")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as resources:
        if retry_control_path is not None:
            resources.enter_context(maintenance_lock(retry_control_lock))
        session = resources.enter_context(ArchiveSession(roots.archive_root, mode=ArchiveAccessMode.MAINTENANCE, artifact_roots=roots))
        artifacts = collect_artifacts(roots.read_bases())
        with tempfile.TemporaryDirectory(prefix=".reference-save-", dir=output.parent) as temporary:
            stage = Path(temporary)
            database = stage / "archive.db"
            contract = create_database_snapshot(session.database_path, database)
            # Exclusive archive ownership stops supported live writers. Recorded
            # running/failed attempts remain exact history; do not recover them.
            with closing(connect_database(database, readonly=True)) as connection:
                if retry_control_path is not None:
                    with database.open("rb") as source:
                        _size, database_digest = stream_hash(source)
                    controls = {"verified": True, "retry_control": capture_control(retry_control_path, connection,
                                database_sha256=database_digest, captured_at=int(time.time()))}
                catalog = ArtifactCatalog(connection)
                dependencies = []
                for key, expected in required_artifacts(database).items():
                    dep = _dependency(catalog, key, expected) if expected else None
                    if dep:
                        dependencies.append(dep)
                        artifacts.pop(key, None)
                    elif key not in artifacts:
                        raise ReferenceBackupError(f"required bytes have no embedded file or retained replica: {key}")
                consumers = _consumers(connection, dependencies)
            used_targets = {replica["target_id"] for dep in dependencies for replica in dep["replicas"]}
            if not set(instances) <= used_targets:
                raise ReferenceBackupError("instance binding names a target outside the dependency manifest")
            archive = stage / "reference.zip"
            entries = []
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
                for key, path in sorted({"archive.db": database, **artifacts}.items()):
                    with _open_regular(path) as source, bundle.open(key, "w", force_zip64=True) as destination:
                        size, digest = stream_hash(source, destination)
                    entries.append({"path": key, "size": size, "sha256": digest})
                version = 2 if retry_control_path is not None else VERSION
                manifest = {"format": FORMAT, "format_version": version, "self_contained": False,
                            "backup_id": uuid.uuid4().hex, "created_at": int(time.time()), "database_contract": contract,
                            "files": entries, "dependencies": dependencies, "target_instances": instances,
                            "affected_jobs": consumers, "operational_state": controls,
                            "handoff": "stop-old-workers-before-new-workers; no-automatic-recovery-or-retry"}
                validate_json(f"{FORMAT}/v{version}", manifest)
                if len(_encoded(manifest)) > 16 * 1024**2:
                    raise ReferenceBackupError("reference manifest exceeds 16 MiB")
                bundle.writestr(MANIFEST, _encoded(manifest))
            # Independently read the finished container before publishing it.
            with verified_reference(archive) as (_database, verified):
                result = _report(verified)
            if retry_control_path is not None:
                with _open_regular(retry_control_path) as source:
                    _size, digest = stream_hash(source)
                if digest != controls["retry_control"]["source"]["sha256"]:
                    raise ReferenceBackupError("retry control source changed during capture")
            with archive.open("r+b") as sealed:
                os.fsync(sealed.fileno())
            if os.name == "nt":
                os.rename(archive, output)
            else:
                os.link(archive, output)
            sync_directory(output.parent)
            return {"operation": "reference-save", **result}


def _read_manifest(bundle):
    members = bundle.infolist()
    names = [member.filename for member in members]
    if len(names) != len(set(names)) or names.count(MANIFEST) != 1 or len(names) > 100001:
        raise ReferenceBackupError("duplicate or excessive backup members")
    for member in members:
        mode = member.external_attr >> 16
        if member.is_dir() or stat.S_ISLNK(mode) or member.flag_bits & 1 or member.compress_type != zipfile.ZIP_STORED:
            raise ReferenceBackupError("reference backups require ordinary unencrypted stored files")
    if bundle.getinfo(MANIFEST).file_size > 16 * 1024**2:
        raise ReferenceBackupError("reference manifest exceeds 16 MiB")
    manifest = json.loads(bundle.read(MANIFEST), object_pairs_hook=unique_json_object)
    version = manifest.get("format_version") if isinstance(manifest, dict) else None
    if type(version) is not int or version not in {1, 2}:
        raise ReferenceBackupError("unsupported reference backup contract")
    validate_json(f"{FORMAT}/v{version}", manifest)
    fields = {"format", "format_version", "self_contained", "backup_id", "created_at", "database_contract", "files",
              "dependencies", "target_instances", "affected_jobs", "operational_state", "handoff"}
    if set(manifest) != fields or manifest["format"] != FORMAT or manifest["self_contained"] is not False:
        raise ReferenceBackupError("unsupported reference backup contract")
    if not isinstance(manifest["backup_id"], str) or not re.fullmatch("[0-9a-f]{32}", manifest["backup_id"]) or type(manifest["created_at"]) is not int or manifest["created_at"] < 0:
        raise ReferenceBackupError("invalid reference backup identity")
    entries = manifest["files"]
    if not isinstance(entries, list) or not entries:
        raise ReferenceBackupError("missing embedded database")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"} or type(entry["size"]) is not int or entry["size"] < 0 or not isinstance(entry["sha256"], str) or not _HASH.fullmatch(entry["sha256"]):
            raise ReferenceBackupError("invalid embedded file identity")
    keys = [entry["path"] for entry in entries]
    check_artifact_collisions(keys)
    if keys.count("archive.db") != 1 or set(names) != {MANIFEST, *keys}:
        raise ReferenceBackupError("unlisted or missing embedded files")
    controls = manifest["operational_state"]
    if version == 2:
        if set(controls) != {"verified", "retry_control"} or controls["verified"] is not True:
            raise ReferenceBackupError("invalid typed retry operational state")
        validate_control(controls["retry_control"])
    else:
        if not isinstance(controls, dict) or set(controls) != {"verified", "holds"} or type(controls["verified"]) is not bool:
            raise ReferenceBackupError("invalid operational whitelist")
        expected = operational_holds({"version": 1, "holds": controls["holds"]}) if controls["verified"] else operational_holds(None)
        if controls != expected:
            raise ReferenceBackupError("unverified holds cannot carry unvalidated state")
    if manifest["handoff"] != "stop-old-workers-before-new-workers; no-automatic-recovery-or-retry":
        raise ReferenceBackupError("unknown execution handoff contract")
    instances = manifest["target_instances"]
    if not isinstance(instances, dict) or any(not isinstance(key, str) or not isinstance(value, str) or not re.fullmatch("[0-9a-f]{32}", value) for key, value in instances.items()):
        raise ReferenceBackupError("invalid target instance identities")
    return manifest


def _validate_into(path, stage, *, extract_artifacts=False):
    require_regular_file(path)
    with _open_regular(path) as source:
        _bounded_zip_directory(source)
        with zipfile.ZipFile(source) as bundle:
            manifest = _read_manifest(bundle)
            for entry in manifest["files"]:
                key = entry["path"]
                member = bundle.getinfo(key)
                if member.file_size != entry["size"]:
                    raise ReferenceBackupError("embedded file size mismatch")
                parts = portable_artifact_parts(key)
                with bundle.open(member) as incoming:
                    if key == "archive.db" or extract_artifacts:
                        destination = stage.joinpath(*parts)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with destination.open("xb") as output:
                            actual = stream_hash(incoming, output)
                            output.flush()
                            os.fsync(output.fileno())
                    else:
                        actual = stream_hash(incoming)
                if actual != (entry["size"], entry["sha256"]):
                    raise ReferenceBackupError("embedded checksum mismatch")
            _check_bundle_markers(bundle, {entry["path"]: entry for entry in manifest["files"]})
    database = stage / "archive.db"
    validate_snapshot_database(database, expected_contract=manifest["database_contract"])
    required = required_artifacts(database)
    available = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    dependencies = manifest["dependencies"]
    if not isinstance(dependencies, list):
        raise ReferenceBackupError("invalid dependency list")
    seen = set(available)
    with closing(connect_database(database, readonly=True)) as connection:
        if manifest["format_version"] == 2:
            database_digest = available["archive.db"]
            validate_control(manifest["operational_state"]["retry_control"], connection=connection, database_sha256=database_digest)
        catalog = ArtifactCatalog(connection)
        for dep in dependencies:
            if not isinstance(dep, dict) or set(dep) != {"path", "object_id", "size", "replicas"}:
                raise ReferenceBackupError("invalid dependency identity")
            key = dep["path"]
            portable_artifact_parts(key)
            if key in seen or key not in required or required[key] != dep["object_id"] or not isinstance(dep["object_id"], str) or not _HASH.fullmatch(dep["object_id"]):
                raise ReferenceBackupError("dependency does not match retained database reference")
            if type(dep["size"]) is not int or dep != _dependency(catalog, key, dep["object_id"]):
                raise ReferenceBackupError("dependency does not match retained replica catalog")
            seen.add(key)
            available[key] = dep["object_id"]
        if manifest["affected_jobs"] != _consumers(connection, dependencies):
            raise ReferenceBackupError("affected tasks differ from retained database")
    check_artifact_collisions(list(seen))
    targets = {replica["target_id"] for dep in dependencies for replica in dep["replicas"]}
    if not set(manifest["target_instances"]) <= targets:
        raise ReferenceBackupError("unbound target instance in manifest")
    for key, digest in required.items():
        if key not in available or digest is not None and available[key] != digest:
            raise ReferenceBackupError("required reference is neither embedded nor preserved externally")
    return manifest


@contextmanager
def verified_reference(path: Path):
    """Stream-verify every member; materialize only SQLite for offline readers."""
    with tempfile.TemporaryDirectory(prefix="bili-reference-check-") as temporary:
        stage = Path(temporary)
        manifest = _validate_into(Path(path), stage)
        yield stage / "archive.db", manifest


def _report(manifest):
    dependencies = manifest["dependencies"]
    controls = manifest["operational_state"]
    typed = manifest["format_version"] == 2
    return {"valid": True, "format": f"{FORMAT}/v{manifest['format_version']}", "backup_id": manifest["backup_id"], "self_contained": False,
            "database_contract": manifest["database_contract"], "embedded_files": len(manifest["files"]),
            "read_only_temporary_bytes": next(entry["size"] for entry in manifest["files"] if entry["path"] == "archive.db"),
            "read_only_media_extracted": False,
            "verification_scope": "all-embedded-member-bytes-streamed-and-database-references",
            "dependency_count": len(dependencies), "external_state": "unverified", "external_verified": False,
            "blocking_objects": [{"path": dep["path"], "object_id": dep["object_id"], "reason": "external_unverified",
                                  "jobs": manifest["affected_jobs"].get(dep["object_id"], [])} for dep in dependencies],
            "holds_preserved": len(controls["retry_control"]["retry_holds"] if typed else controls["holds"]),
            "retry_ledger_preserved": len(controls["retry_control"]["retry_ledger"]) if typed else None,
            "control_recovery_capability": "scope-bound-retry-controls" if typed else "limited-holds-only" if controls["verified"] else "unavailable",
            "external_holds_verified": controls["verified"],
            "execution_handoff_required": True, "worker_started": False, "retry_started": False}


def check_reference_backup(path: Path, *, local_targets: dict[str, Path] | None = None,
                           remote_backends: dict | None = None, online: bool = False) -> dict:
    with verified_reference(path) as (database, manifest):
        report = _report(manifest)
        if not online:
            return {"operation": "reference-check", **report}
        checked, blockers, cache = [], [], {}
        roots = ArtifactRoots.of(database.parent)
        with tempfile.TemporaryDirectory(prefix="bili-reference-network-") as temporary:
            for dep in manifest["dependencies"]:
                failures, valid = [], False
                for replica in dep["replicas"]:
                    identity = replica["target_id"]
                    cache_key = (identity, replica["relative_key"], replica["package_sha256"])
                    try:
                        if cache_key not in cache:
                            backend = (remote_backends or {}).get(identity)
                            if backend:
                                expected = manifest["target_instances"].get(identity)
                                if expected is None or backend.binding.instance_id != expected:
                                    raise ReferenceBackupError("target_instance_unbound_or_mismatch")
                                from bili_asr.services.remote_artifacts import (
                                    fetch_remote_package,
                                )
                                destination = Path(temporary) / (uuid.uuid4().hex + ".zip")
                                cache[cache_key] = fetch_remote_package(backend, replica, destination)
                            elif identity in (local_targets or {}):
                                if identity in manifest["target_instances"]:
                                    raise ReferenceBackupError("target_backend_mismatch")
                                target = open_directory_target(local_targets[identity], identity, roots=roots)
                                target.check()
                                result = check_artifact_package(target.root / replica["relative_key"], expected_sha256=replica["package_sha256"])
                                target.check()
                                cache[cache_key] = result
                            else:
                                raise ReferenceBackupError("target_unbound")
                        package = cache[cache_key]
                        entry = next((item for item in package["objects"] if item["object_id"] == dep["object_id"]), None)
                        if package["package_id"] != replica["package_id"] or package["manifest_sha256"] != replica["manifest_sha256"] or entry is None or entry["member"] != replica["member_key"] or entry["size_bytes"] != dep["size"]:
                            raise ReferenceBackupError("package_identity_mismatch")
                        checked.append({"object_id": dep["object_id"], "target_id": identity, "scope": "full-package-sha256-and-member"})
                        valid = True
                        break
                    except (OSError, ValueError) as error:
                        code = getattr(error, "code", "unavailable_or_identity_mismatch")
                        failures.append({"target_id": identity, "code": code})
                if not valid:
                    blockers.append({"path": dep["path"], "object_id": dep["object_id"], "failures": failures,
                                     "jobs": manifest["affected_jobs"].get(dep["object_id"], [])})
        report.update(external_state="complete" if not blockers else "partial" if checked else "unverifiable",
                      external_verified=not blockers, blocking_objects=blockers,
                      external_checks=checked, checked_at=int(time.time()), verification_scope="explicit-external-package-readback")
        return {"operation": "reference-check", **report}


def plan_reference_restore(path: Path, target: Path) -> dict:
    target = Path(os.path.abspath(target))
    require_no_links(target)
    _empty_destination(target)
    with verified_reference(path) as (_database, manifest):
        needed = sum(entry["size"] for entry in manifest["files"]) + 16 * 1024**2
        ancestor = target.parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        free = shutil.disk_usage(ancestor).free
        with _open_regular(path) as source:
            _size, digest = stream_hash(source)
        return {"operation": "reference-plan", **_report(manifest), "backup_sha256": digest,
                "required_free_bytes": needed, "available_free_bytes": free, "target_ready": free >= needed,
                "restore_changes_workflow_state": False, "apply_revalidates_all_embedded_bytes": True}


def restore_reference_backup(path: Path, target: Path, *, source_workers_stopped: bool = False) -> dict:
    path, target = Path(os.path.abspath(path)), Path(os.path.abspath(target))
    if path.is_relative_to(target):
        raise ReferenceBackupError("restore destination contains the backup")
    plan = plan_reference_restore(path, target)
    if not plan["target_ready"]:
        raise ReferenceBackupError("insufficient restore space")
    target.parent.mkdir(parents=True, exist_ok=True)
    with archive_access(target, exclusive=True), tempfile.TemporaryDirectory(prefix=".reference-restore-", dir=target.parent) as temporary:
        _empty_destination(target)
        stage = Path(temporary) / "archive"
        stage.mkdir()
        manifest = _validate_into(path, stage, extract_artifacts=True)
        controls_directory = stage / "documents" / "reference-recovery" / manifest["backup_id"]
        controls_directory.mkdir(parents=True, exist_ok=True)
        verified_holds = manifest["operational_state"]["verified"]
        typed = manifest["format_version"] == 2
        controls = controls_directory / ("retry-control.json" if typed else "holds.json" if verified_holds else "holds-unverified.json")
        with controls.open("xb") as output:
            output.write(_encoded(manifest["operational_state"]["retry_control"] if typed else {"version": 1, "holds": manifest["operational_state"]["holds"]} if verified_holds
                                  else {"version": 1, "verified": False, "action": "verify-external-hold-source-before-use"}))
            output.flush()
            os.fsync(output.fileno())
        for directory, _children, _files in os.walk(stage, topdown=False):
            sync_directory(Path(directory))
        if _empty_destination(target):
            target.rmdir()
        stage.rename(target)
        sync_directory(target.parent)
    from bili_asr.services.archive_recovery import doctor_archive
    from bili_asr.services.artifact_state import artifact_state
    doctor = doctor_archive(target)
    with ArchiveSession(target, mode=ArchiveAccessMode.READ) as session:
        readiness = artifact_state(session.connection, ArtifactRoots.of(target))
    return {"operation": "reference-restore", **_report(manifest), "installed": True,
            "control_state_path": str(target / controls.relative_to(stage)),
            "source_workers_stopped_attested": source_workers_stopped,
            "ready_to_run": False, "next_steps": ["rebind-and-verify-targets", "restore-required-inputs", "doctor", "explicit-worker-handoff"],
            "workflow_recovery_changes": [], "doctor": doctor, "artifact_readiness": readiness}
