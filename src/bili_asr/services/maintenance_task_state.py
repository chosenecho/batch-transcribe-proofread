"""Consistent inactive task evidence without copying media or reviving workers.

The SQLite read transaction is the database observation point. External controls
and evidence are independently observed before/after capture; their stable byte
versions are recorded without claiming a cross-component transaction.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import time
import zipfile
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from pathlib import Path

from bili_asr import __version__
from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_inventory import (
    collect_artifacts,
    portable_artifact_parts,
    stream_hash,
)
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.services import maintenance_package_core as package
from bili_asr.services.migration_preflight import _root_entries
from bili_asr.services.retry_maintenance_io import maintenance_lock
from bili_asr.services.runtime_root_policy import (
    RUNTIME_POLICY,
    RuntimeRootPolicyError,
    inspect_runtime_roots,
    validate_runtime_exclusions,
)
from bili_asr.storage.archive_contracts import runtime_contract
from bili_asr.storage.maintenance_selection import table_ledger
from bili_asr.storage.snapshots import required_artifacts, validate_snapshot_database

NAME = "task-state.json"
FORMAT = "archive-task-state-backup/v1"
REFERENCE_FORMAT = "archive-task-state-backup/v2"
PROTOCOL = "sqlite-read-transaction-and-stable-components/v1"
RESTORE_POLICY = "read-only-evidence-never-start-workers/v1"


def _parts(key: str) -> tuple[str, ...]:
    if key.startswith("control-state/"):
        parts = portable_artifact_parts("documents/" + key.removeprefix("control-state/"))
        if len(parts) != 2:
            raise ValueError("task-state control files must be plain component names")
        return ("control-state", parts[1])
    return portable_artifact_parts(key)


def validate_components(components) -> list[dict]:
    if not isinstance(components, (list, tuple)):
        raise ValueError("external components must be explicit typed declarations")  # noqa: TRY004
    result = []
    for value in components:
        if (not isinstance(value, dict) or set(value) != {"kind", "path", "lock_path"}
                or value["kind"] not in {"retry-control/v1", "retry-control/v2", "backup-job-state/v1"}
                or any(not isinstance(value[key], str) or not Path(value[key]).is_absolute() for key in ("path", "lock_path"))):
            raise ValueError("unknown external component contract or nonabsolute path")
        path = package.absolute(Path(value["path"]))
        if str(path) != value["path"]:
            raise ValueError("component paths must be canonical absolute paths")
        lock = package.absolute(Path(value["lock_path"]))
        if str(lock) != value["lock_path"] or lock == path:
            raise ValueError("component coordination lock must be canonical and distinct from its data")
        result.append(dict(value))
    if len({item["path"] for item in result}) != len(result):
        raise ValueError("external components cannot duplicate paths")
    return result


def _control(value: dict, kind: str) -> None:
    if kind == "backup-job-state/v1":
        if (set(value) != {"format", "policy", "current_run", "prior_runs", "state"}
                or value["format"] != kind or not all(isinstance(value[key], dict) for key in ("policy", "current_run", "state"))
                or not isinstance(value["prior_runs"], list) or any(not isinstance(run, dict) for run in value["prior_runs"])):
            raise ValueError("invalid backup job evidence contract")
        policy, run = value["policy"], value["current_run"]
        if (policy.get("format") != "maintenance-backup-policy/v1" or policy.get("policy_id") != package.digest({k: v for k, v in policy.items() if k != "policy_id"})
                or run.get("format") != "maintenance-backup-run/v1" or run.get("policy_id") != policy["policy_id"]
                or run.get("policy") != policy or run.get("run_id") != package.digest({"policy": policy["policy_id"], "timestamp": run.get("started_at"), "trigger": run.get("trigger"), "event": run.get("event_id")})):
            raise ValueError("unknown backup policy or invocation identity")
        for previous in value["prior_runs"]:
            if (previous.get("format") != "maintenance-backup-run/v1" or previous.get("policy_id") != policy["policy_id"]
                    or previous.get("policy") != policy or previous.get("status") not in {"success", "failed", "running"}
                    or previous.get("run_id") != package.digest({"policy": policy["policy_id"], "timestamp": previous.get("started_at"), "trigger": previous.get("trigger"), "event": previous.get("event_id")})
                    or previous["status"] != "running" and previous.get("receipt_id") != package.digest({k: v for k, v in previous.items() if k != "receipt_id"})):
                raise ValueError("unknown prior backup receipt identity")
        return
    from bili_asr.services.retry_control import validate_control, validate_legacy_typed
    if kind == "retry-control/v2":
        validate_control(value)
    else:
        validate_legacy_typed(value)


def _observe(components: list[dict]) -> list[dict]:
    result = []
    for index, declaration in enumerate(components):
        path = Path(declaration["path"])
        document = package._json(path)
        _control(document, declaration["kind"])
        record = package._record(f"control-state/component-{index}.json", path)
        result.append({**declaration, **record, "source_path": declaration["path"]})
    return result


def _database(source: Path, target: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=min(timeout, 1))) as origin:
        origin.execute("PRAGMA query_only=ON")
        origin.execute("BEGIN")
        origin.execute("SELECT count(*) FROM sqlite_master").fetchone()
        with target.open("xb"):
            pass
        with closing(sqlite3.connect(target)) as destination:
            def progress(status, remaining, total):
                if time.monotonic() >= deadline:
                    raise TimeoutError("task-state SQLite backup exceeded its bounded observation interval")
            origin.backup(destination, pages=256, progress=progress, sleep=0.01)
        origin.rollback()


def _frozen_versions(database: Path) -> None:
    """Select bounded registered adapters while preserving original JSON bytes."""
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("editorial_input_versions", "publication_content_versions"):
            if table in tables and connection.execute(f"SELECT 1 FROM {table} WHERE version NOT IN (1,2) LIMIT 1").fetchone():
                raise ValueError("unknown frozen content contract version: " + table)
        for (raw,) in connection.execute("SELECT payload_json FROM workflow_jobs"):
            value = json.loads(raw)
            if not isinstance(value, dict) or type(value.get("schema_version", 1)) is not int or value.get("schema_version", 1) != 1:
                raise ValueError("unknown frozen workflow payload version")


def _copy_file(source: Path, target: Path, expected: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, target.open("xb") as writer:
        actual = stream_hash(reader, writer)
        package._sync_file(writer)
    if actual != (expected["size"], expected["sha256"]):
        raise ValueError("task-state component changed while copying: " + expected["path"])


def save_task_state(source_root: Path, output: Path, *, artifact_root=None, external_components=(),
                    no_external_control_state=False, backup_job_state=None, timeout_seconds=30,
                    media_policy="local-required", local_targets=None, remote_backends=None) -> dict:
    root, out = package.absolute(source_root), package.absolute(output)
    product = root if artifact_root is None else package.absolute(artifact_root)
    try:
        runtime_exclusions = inspect_runtime_roots(tuple(dict.fromkeys((product, root))))
    except RuntimeRootPolicyError as exc:
        raise ValueError(str(exc)) from exc
    components = validate_components(external_components)
    if media_policy not in {"local-required", "verified-references"}:
        raise ValueError("unknown task-state media policy")
    if media_policy == "local-required" and (local_targets or remote_backends):
        raise ValueError("cold target bindings require explicit verified-references media policy")
    if type(no_external_control_state) is not bool or (not components and not no_external_control_state) or (components and no_external_control_state):
        raise ValueError("declare typed external controls or explicitly declare no external control state")
    if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= 300:
        raise ValueError("task-state timeout must be positive and at most 300 seconds")
    if (out.exists() or not out.parent.is_dir() or out.is_relative_to(root) or out.is_relative_to(product)
            or any(out.is_relative_to(Path(item["path"]).parent) for item in components)):
        raise ValueError("task-state output must be a new file outside all source components")
    if backup_job_state is not None:
        _control(backup_job_state, "backup-job-state/v1")
    started = datetime.now(UTC).isoformat()
    deadline = time.monotonic() + timeout_seconds
    with ExitStack() as resources:
        for lock_path in sorted({item["lock_path"] for item in components}):
            resources.enter_context(maintenance_lock(Path(lock_path)))
        resources.enter_context(ArchiveSession(root, mode=ArchiveAccessMode.READ).access())
        for base in tuple(dict.fromkeys((product, root))):
            _root_entries(base, root, product)
        runtime_exclusions = inspect_runtime_roots(tuple(dict.fromkeys((product, root))))
        with tempfile.TemporaryDirectory(prefix=".task-state-", dir=out.parent) as temporary:
            stage = Path(temporary) / "state"
            stage.mkdir()
            before = _observe(components)
            _database(root / "archive.db", stage / "archive.db", timeout_seconds)
            contract = validate_snapshot_database(stage / "archive.db")
            with closing(sqlite3.connect((stage / "archive.db").as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
                runtime = runtime_contract(connection)
            physical = collect_artifacts(tuple(dict.fromkeys((product, root))))
            needed = required_artifacts(stage / "archive.db")
            needed.update(package._attempt_artifacts(stage / "archive.db"))
            selected = {key: value for key, value in physical.items() if not key.startswith("audio/")}
            missing = {key for key in needed if key not in physical
                       and (media_policy == "local-required" or not key.startswith("audio/"))}
            if missing:
                raise ValueError("task-state evidence dependency is missing: " + min(missing))
            media = []
            from bili_asr.services.maintenance_media import ReferenceCapture
            references = ReferenceCapture(ArtifactRoots.of(root, product), dict(local_targets or {}),
                                          dict(remote_backends or {}), Path(temporary), deadline)
            with closing(sqlite3.connect((stage / "archive.db").as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                for key in sorted(needed):
                    if key not in physical:
                        media.append(references.reference(connection, key, needed[key]))
                        continue
                    record = package._record(key, physical[key])
                    if needed[key] is not None and record["sha256"] != needed[key]:
                        raise ValueError("task-state dependency hash differs from frozen evidence: " + key)
                    if key.startswith("audio/"):
                        media.append({**record, "available": True, "kind": "audio-reference"})
            files = [package._record("archive.db", stage / "archive.db")]
            for key, source in sorted(selected.items()):
                if time.monotonic() >= deadline:
                    raise TimeoutError("task-state evidence capture exceeded its bounded observation interval")
                record = package._record(key, source)
                _copy_file(source, stage.joinpath(*portable_artifact_parts(key)), record)
                files.append(record)
            for component in before:
                record = {key: component[key] for key in ("path", "size", "sha256")}
                _copy_file(Path(component["source_path"]), stage / component["path"], record)
                files.append(record)
            if _observe(components) != before:
                raise ValueError("external control observation changed across database capture")
            for entry in files:
                if entry["path"] in selected and package._record(entry["path"], selected[entry["path"]]) != entry:
                    raise ValueError("task-state source evidence changed across capture")
            for entry in media:
                if entry["available"] and package._record(entry["path"], physical[entry["path"]]) != {k: entry[k] for k in ("path", "size", "sha256")}:
                    raise ValueError("task-state audio identity changed across capture")
            references.finish()
            for base in tuple(dict.fromkeys((product, root))):
                _root_entries(base, root, product)
            if inspect_runtime_roots(tuple(dict.fromkeys((product, root)))) != runtime_exclusions:
                raise ValueError("runtime exclusions changed across task-state capture")
            if backup_job_state is not None:
                evidence = stage / "control-state" / "backup-job.json"
                evidence.parent.mkdir(exist_ok=True)
                with evidence.open("xb") as writer:
                    writer.write(package.canonical(backup_job_state))
                    package._sync_file(writer)
                files.append(package._record("control-state/backup-job.json", evidence))
            value = {"format": FORMAT, "profile": "task-state", "container": {"compression": "stored", "encryption": None},
                     "database_contract": contract, "runtime_contract": runtime, "producer_version": __version__,
                     "runtime_policy": RUNTIME_POLICY, "runtime_exclusions": runtime_exclusions,
                     "source": {"root": str(root), "database_schema_sha256": contract}, "files": sorted(files, key=lambda item: item["path"]),
                     "tables": table_ledger(stage / "archive.db"), "media_dependencies": media,
                     "inactive_archive": True, "media_self_contained": False, "restore_policy": RESTORE_POLICY,
                     "no_external_control_state": no_external_control_state, "backup_job_state": backup_job_state,
                     "observation": {"protocol": PROTOCOL, "started_at": started, "finished_at": datetime.now(UTC).isoformat(),
                         "sqlite_snapshot_sha256": files[0]["sha256"], "component_versions": before, "atomic_cross_components": False}}
            if media_policy == "verified-references":
                from bili_asr.services.maintenance_media import POLICY
                value.update(format=REFERENCE_FORMAT, media_policy=POLICY)
            manifest = {**value, "created_at": started, "content_id": package.digest(value)}
            package._free_space(out, sum(item["size"] for item in files) * 2 + 32 * 1024**2)
            staged = Path(temporary) / "state.zip"
            with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
                for entry in manifest["files"]:
                    with (stage / entry["path"]).open("rb") as reader, bundle.open(entry["path"], "w", force_zip64=True) as writer:
                        stream_hash(reader, writer)
                bundle.writestr(NAME, package.canonical(manifest))
            verify_task_state(staged)
            with staged.open("r+b") as writer:
                package._sync_file(writer)
            if os.name == "nt":
                staged.rename(out)
            else:
                os.link(staged, out)
            package._sync_directory(out.parent)
    return {"operation": "task-state-backup", "valid": True, "snapshot": str(out), "content_id": manifest["content_id"],
            "inactive_archive": True, "media_self_contained": False, "manifest": manifest}


def extract_task_state(snapshot: Path, stage: Path) -> dict:
    path, destination = package.absolute(snapshot), package.absolute(stage)
    if not destination.is_dir() or any(destination.iterdir()):
        raise ValueError("task-state reader staging must be an existing empty directory")
    with zipfile.ZipFile(path) as bundle:
        if sum(item.filename == NAME for item in bundle.infolist()) != 1:
            raise ValueError("task-state requires exactly one manifest")
        info = bundle.getinfo(NAME)
        if info.file_size > package._LIMIT:
            raise ValueError("oversized task-state manifest")
        manifest = json.loads(bundle.read(info), object_pairs_hook=package._manifest_object)
        from bili_asr.contracts.json_schema import validate_json
        version = manifest.get("format") if isinstance(manifest, dict) else None
        if version not in {FORMAT, REFERENCE_FORMAT}:
            raise ValueError("unsupported task-state manifest version")
        if ("runtime_policy" in manifest) != ("runtime_exclusions" in manifest):
            raise ValueError("task-state runtime policy and exclusions must appear together")
        validate_json(version, manifest)
        if "runtime_exclusions" in manifest:
            validate_runtime_exclusions(manifest["runtime_exclusions"])
        if (manifest.get("profile") != "task-state"
                or manifest.get("runtime_contract") not in {"bilibili-v1", "universal-v2"}
                or ("runtime_policy" in manifest and manifest.get("runtime_policy") != RUNTIME_POLICY)
                or ("runtime_exclusions" in manifest and not isinstance(manifest.get("runtime_exclusions"), list))
                or manifest.get("container") != {"compression": "stored", "encryption": None}
                or manifest.get("inactive_archive") is not True or manifest.get("media_self_contained") is not False
                or manifest.get("restore_policy") != RESTORE_POLICY
                or manifest.get("content_id") != package.digest({k: v for k, v in manifest.items() if k not in {"content_id", "created_at"}})):
            raise ValueError("unsupported task-state manifest or identity")
        observation = manifest["observation"]
        if observation["protocol"] != PROTOCOL or observation["atomic_cross_components"] is not False:
            raise ValueError("unknown task-state observation protocol")
        try:
            start = datetime.fromisoformat(observation["started_at"])
            finish = datetime.fromisoformat(observation["finished_at"])
        except ValueError as exc:
            raise ValueError("invalid task-state observation timestamp") from exc
        if (start.utcoffset() != UTC.utcoffset(start) or finish.utcoffset() != UTC.utcoffset(finish)
                or finish < start or manifest["created_at"] != observation["started_at"]
                or not Path(manifest["source"]["root"]).is_absolute()
                or manifest["source"]["database_schema_sha256"] != manifest["database_contract"]):
            raise ValueError("task-state source or observation boundary differs from capture")
        files = manifest["files"]
        names = [item.filename for item in bundle.infolist()]
        expected = [entry["path"] for entry in files]
        if len(names) != len(set(names)) or len(expected) != len(set(expected)) or set(names) != {*expected, NAME} or "archive.db" not in expected:
            raise ValueError("task-state member inventory mismatch")
        package.check_artifact_collisions(["documents/" + key if key.startswith("control-state/") else key for key in expected])
        component_paths = []
        for component in observation["component_versions"]:
            _parts(component["path"])
            if (not component["path"].startswith("control-state/component-") or not component["path"].endswith(".json")
                    or component["path"] not in expected or component["kind"] not in {"retry-control/v1", "retry-control/v2", "backup-job-state/v1"}
                    or not Path(component["source_path"]).is_absolute() or not Path(component["lock_path"]).is_absolute()):
                raise ValueError("task-state component path or contract is unsupported")
            component_paths.append(component["path"])
        if len(component_paths) != len(set(component_paths)) or manifest["no_external_control_state"] != (not component_paths):
            raise ValueError("task-state external control declaration is inconsistent")
        control_paths = {key for key in expected if key.startswith("control-state/")}
        if control_paths != set(component_paths) | ({"control-state/backup-job.json"} if manifest["backup_job_state"] is not None else set()):
            raise ValueError("task-state contains undeclared external controls")
        for item in bundle.infolist():
            if (item.orig_filename != item.filename or item.is_dir() or item.flag_bits & 1
                    or item.compress_type != zipfile.ZIP_STORED or stat.S_IFMT(item.external_attr >> 16) not in {0, stat.S_IFREG}):
                raise ValueError("unsafe task-state member")
        for entry in files:
            key = entry["path"]
            if key.startswith("audio/") or key != "archive.db" and not key.startswith(("subtitles/", "transcripts/", "documents/", "publications/", "control-state/")):
                raise ValueError("task-state package contains unsupported media or private runtime files")
            target = destination.joinpath(*_parts(key))
            if (set(entry) != {"path", "size", "sha256"} or type(entry["size"]) is not int or entry["size"] < 0
                    or not isinstance(entry["sha256"], str) or not package._HASH.fullmatch(entry["sha256"])
                    or bundle.getinfo(key).file_size != entry["size"]):
                raise ValueError("invalid task-state file identity")
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(key) as reader, target.open("xb") as writer:
                actual = stream_hash(reader, writer)
            if actual != (entry["size"], entry["sha256"]):
                raise ValueError("task-state payload hash mismatch")
        database = destination / "archive.db"
        validate_snapshot_database(database, expected_contract=manifest["database_contract"])
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            if runtime_contract(connection) != manifest["runtime_contract"]:
                raise ValueError("task-state runtime contract differs from its database")
        if table_ledger(database) != manifest["tables"]:
            raise ValueError("task-state frozen table ledger mismatch")
        _frozen_versions(database)
        db_record = next(entry for entry in files if entry["path"] == "archive.db")
        if db_record["sha256"] != observation["sqlite_snapshot_sha256"]:
            raise ValueError("task-state SQLite observation identity mismatch")
        media = {entry["path"]: entry for entry in manifest["media_dependencies"]}
        if len(media) != len(manifest["media_dependencies"]):
            raise ValueError("task-state media dependencies contain duplicates")
        cold_entries = []
        for entry in media.values():
            portable_artifact_parts(entry["path"])
            cold = entry["available"] is False and version == REFERENCE_FORMAT
            fields = {"path", "size", "sha256", "kind", "available"} | ({"verification", "replicas"} if cold else set())
            if (set(entry) != fields or not entry["path"].startswith("audio/")
                    or type(entry["size"]) is not int or entry["size"] < 0 or not isinstance(entry["sha256"], str)
                    or not package._HASH.fullmatch(entry["sha256"]) or entry["kind"] != "audio-reference"
                    or not cold and entry["available"] is not True):
                raise ValueError("invalid task-state media dependency identity")
            if cold:
                cold_entries.append(entry)
        if cold_entries:
            from bili_asr.services.maintenance_media import validate_references
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                validate_references(connection, cold_entries)
        inventory = {entry["path"]: entry for entry in files} | media
        needed = required_artifacts(database) | package._attempt_artifacts(database)
        for key, fingerprint in needed.items():
            if key not in inventory or fingerprint is not None and inventory[key]["sha256"] != fingerprint:
                raise ValueError("task-state evidence dependency is missing or changed")
        for component in observation["component_versions"]:
            document = package._json(destination / component["path"])
            _control(document, component["kind"])
            if component["kind"] == "retry-control/v2":
                from bili_asr.services.retry_control import validate_control
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
                    validate_control(document, connection=connection)
            if package._record(component["path"], destination / component["path"]) != {k: component[k] for k in ("path", "size", "sha256")}:
                raise ValueError("task-state external observation identity mismatch")
        if manifest["backup_job_state"] is not None:
            _control(manifest["backup_job_state"], "backup-job-state/v1")
            if package._json(destination / "control-state/backup-job.json") != manifest["backup_job_state"]:
                raise ValueError("task-state backup job observation mismatch")
    return manifest


def verify_task_state(snapshot: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="task-state-verify-") as temporary:
        manifest = extract_task_state(snapshot, Path(temporary))
    from bili_asr.services.maintenance_media import media_availability
    return {"operation": "task-state-verify", "valid": True, "manifest": manifest, "content_id": manifest["content_id"],
            "inactive_archive": True, "media_self_contained": False, "restoration_verified": False,
            **media_availability(manifest),
            "verification_kind": "inactive-task-evidence-sqlite-fk-typed-rows-files-and-media-identities"}
