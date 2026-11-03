"""Explicit preservation upgrade into a separately installed archive copy.

The source stays read-only under maintenance coordination. SQLite's backup
copies every original table and row verbatim; neither execution recovery nor
legacy-to-universal conversion runs. Existing artifact bytes are streamed to
private staging. Only matching audio bytes become verified local replicas.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import tempfile
import time
from contextlib import closing
from pathlib import Path

from bili_asr.archive_maintenance import archive_access
from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_inventory import (
    collect_artifacts,
    portable_artifact_parts,
    require_no_links,
    require_regular_file,
    stream_hash,
)
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.canonical_json import canonical, digest
from bili_asr.storage.archive_contracts import _resource, runtime_contract
from bili_asr.storage.artifact_catalog import (
    EXTENSION,
    SCHEMA_RESOURCE,
    ArtifactCatalog,
    require_artifact_catalog,
)
from bili_asr.storage.database import connect_database
from bili_asr.storage.snapshots import required_artifacts, validate_snapshot_database


class ArtifactCatalogUpgradeError(ValueError):
    """An isolated upgrade cannot be verified or safely installed."""


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def authority_fingerprints(connection: sqlite3.Connection) -> list[dict]:
    """Stream all existing tables with row IDs, storage types and raw cell bytes.

    This extends the legacy preservation principle to universal/import tables
    without reparsing old JSON or routing through the legacy-only row reader.
    """
    previous_factory = connection.text_factory
    result = []
    def text(value):
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)
    tables = [(text(row[0]), text(row[1])) for row in connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    try:
        connection.text_factory = bytes
        for name, sql in tables:
            columns = [text(row[1]) for row in connection.execute(f"PRAGMA table_xinfo({_identifier(name)})") if row[6] == 0]
            # The shipped tables all have rowid. The fallback retains primary
            # key ordering for a future registered WITHOUT ROWID contract.
            without_rowid = "WITHOUT ROWID" in sql.upper()
            expressions = [] if without_rowid else ["rowid"]
            for column in columns:
                expressions.extend((f"typeof({_identifier(column)})", _identifier(column)))
            order = ",".join(_identifier(column) for column in columns) if without_rowid else "rowid"
            digest = hashlib.sha256(canonical({"table": name, "sql": sql, "columns": columns}).encode("utf-8"))
            count = 0
            with closing(connection.execute(f"SELECT {','.join(expressions)} FROM {_identifier(name)} ORDER BY {order}")) as cursor:
                for row in cursor:
                    count += 1
                    start = 0 if without_rowid else 1
                    if not without_rowid:
                        digest.update(struct.pack(">q", row[0]))
                    for index in range(start, len(row), 2):
                        kind, value = row[index], row[index + 1]
                        payload = (b"" if kind == b"null" else struct.pack(">q", value) if kind == b"integer"
                                   else struct.pack(">d", value) if kind == b"real" else value)
                        digest.update(kind + b"\0" + struct.pack(">Q", len(payload)) + payload)
            result.append({"table": name, "row_count": count, "sha256": digest.hexdigest()})
    finally:
        connection.text_factory = previous_factory
    return result


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _empty_target(target: Path) -> None:
    require_no_links(target)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ArtifactCatalogUpgradeError("artifact catalog upgrade target must be nonexistent or empty")


def _disjoint(target: Path, bases: tuple[Path, ...]) -> None:
    for base in bases:
        if target == base or target.is_relative_to(base) or base.is_relative_to(target):
            raise ArtifactCatalogUpgradeError("artifact catalog upgrade source, artifact roots and target must be disjoint")


def install_catalog_in_staged_copy(connection: sqlite3.Connection, files: dict[str, dict], *, now: int) -> list[dict]:
    """Internal converter step for a caller-owned, independently verified stage.

    The caller must own an unpublished preservation copy, supply verified file
    facts, and compare original authority fingerprints before publishing it.
    This is intentionally not exposed as an in-place archive command.
    """
    if require_artifact_catalog(connection):
        raise ArtifactCatalogUpgradeError("staged copy already contains the artifact extension")
    connection.executescript(_resource(SCHEMA_RESOURCE))
    with connection:
        return _backfill_audio(connection, files, now=now)


def _backfill_audio(connection: sqlite3.Connection, files: dict[str, dict], *, now: int) -> list[dict]:
    catalog = ArtifactCatalog(connection)
    catalog.register_target("local", kind="local", created_at=now)
    findings = []
    matching_audio = {}
    for key, item in files.items():
        if key.startswith("audio/") and len(key.split("/")) == 2:
            matching_audio.setdefault((item["sha256"], item["size"]), []).append(key)
    for row in connection.execute("SELECT audio_id,sha256,byte_size,storage_key FROM audio_objects ORDER BY audio_id"):
        identity = catalog.register_object(row["sha256"], row["byte_size"], created_at=now)
        catalog.bind_audio(row["audio_id"], identity)
        relative_key = row["storage_key"]
        portable_artifact_parts(relative_key)
        actual = files.get(relative_key)
        if actual is None:
            findings.append({"audio_id": row["audio_id"], "object_id": identity, "relative_key": relative_key, "state": "missing"})
        elif (actual["size"], actual["sha256"]) != (row["byte_size"], identity):
            findings.append({"audio_id": row["audio_id"], "object_id": identity, "relative_key": relative_key, "state": "identity_mismatch",
                             "observed_sha256": actual["sha256"], "observed_size": actual["size"]})
        else:
            catalog.record_verified_replica(identity, "local", relative_key, sha256=identity,
                                            byte_size=row["byte_size"], verified_at=now)
            findings.append({"audio_id": row["audio_id"], "object_id": identity, "relative_key": relative_key, "state": "verified"})
        for alternate_key in matching_audio.get((identity, row["byte_size"]), ()):
            if alternate_key != relative_key:
                catalog.record_verified_replica(identity, "local", alternate_key, sha256=identity,
                                                byte_size=row["byte_size"], verified_at=now)
                findings.append({"audio_id": row["audio_id"], "object_id": identity, "relative_key": alternate_key,
                                 "state": "verified", "reason": "observed_alternate_copy"})
    # Audio attempt JSON remains untouched. Retain historical claims even when
    # they refer to a previous path, an overwritten generation or missing bytes.
    for attempt_id, raw in connection.execute(
        "SELECT a.attempt_id,a.result_json FROM workflow_attempts a JOIN workflow_jobs j USING(job_id) "
        "WHERE a.outcome='succeeded' AND j.kind='audio' ORDER BY a.rowid"
    ):
        try:
            claim = json.loads(raw)
            key, identity = claim.get("storage_key"), claim.get("sha256")
            portable_artifact_parts(key)
            candidates = connection.execute("SELECT byte_size FROM artifact_objects WHERE object_id=?", (identity,)).fetchall()
            actual = files.get(key)
            if not candidates:
                state = "unbound_historical_claim"
            elif actual is None:
                state = "missing"
            elif (actual["size"], actual["sha256"]) != (candidates[0][0], identity):
                state = "identity_mismatch"
            else:
                catalog.record_verified_replica(identity, "local", key, sha256=identity,
                                                byte_size=actual["size"], verified_at=now)
                state = "verified"
            findings.append({"attempt_id": attempt_id, "object_id": identity, "relative_key": key, "state": state})
        except (TypeError, ValueError, AttributeError):
            findings.append({"attempt_id": attempt_id, "state": "invalid_historical_claim"})
    return findings


def _verify_installed(target: Path, report: dict) -> None:
    require_regular_file(target / "archive.db")
    validate_snapshot_database(target / "archive.db")
    with closing(connect_database(target / "archive.db", readonly=True)) as connection:
        require_artifact_catalog(connection, required=True)
        originals = {item["table"] for item in report["authority_tables"]}
        actual = [item for item in authority_fingerprints(connection) if item["table"] in originals]
        if actual != report["authority_tables"]:
            raise ArtifactCatalogUpgradeError("installed upgrade has diverged from its original authority baseline")
    for item in report["files"]:
        path = target.joinpath(*portable_artifact_parts(item["path"]))
        require_regular_file(path)
        with path.open("rb") as stream:
            if stream_hash(stream) != (item["size"], item["sha256"]):
                raise ArtifactCatalogUpgradeError("installed upgraded archive bytes changed")


def _source_inventory(bases: tuple[Path, ...]) -> list[dict]:
    """Read every base separately so a preferred copy cannot hide old bytes."""
    inventory = []
    for base_index, base in enumerate(bases):
        for key, path in sorted(collect_artifacts((base,)).items()):
            with path.open("rb") as reader:
                size, sha256 = stream_hash(reader)
            inventory.append({"base_index": base_index, "path": key, "size": size, "sha256": sha256})
    return inventory


def _audio_path_expectations(connection: sqlite3.Connection) -> dict[str, str]:
    """Choose one retained digest per canonical path, never a storage-root guess."""
    declarations = {}
    for key, sha256 in connection.execute("SELECT storage_key,sha256 FROM audio_objects"):
        declarations.setdefault(key, set()).add(sha256)
    for raw, in connection.execute("SELECT a.result_json FROM workflow_attempts a JOIN workflow_jobs j USING(job_id) WHERE a.outcome='succeeded' AND j.kind='audio'"):
        try:
            claim = json.loads(raw)
            key, sha256 = claim.get("storage_key"), claim.get("sha256")
            if isinstance(key, str) and isinstance(sha256, str):
                declarations.setdefault(key, set()).add(sha256)
        except (TypeError, ValueError, AttributeError):
            continue  # Retain invalid claims in source and the backfill report.
    if any(len(values) > 1 for values in declarations.values()):
        raise ArtifactCatalogUpgradeError("one historical audio path claims multiple digests; preserve source and resolve versioned input evidence first")
    return {key: next(iter(values)) for key, values in declarations.items()}


def _snapshot_readiness(database: Path, inventory: list[dict]) -> dict:
    scope = {"scope": "declared-reference-bytes", "bundle_integrity_verified": False}
    try:
        required = required_artifacts(database)
    except ValueError:
        return {**scope, "reference_bytes_available": False, "reason": "declared_reference_conflict_or_invalidity"}
    found = {item["path"]: item for item in inventory}
    missing, mismatched = [], []
    for key, expected in required.items():
        item = found.get(key)
        if item is None:
            missing.append(key)
        elif expected is not None and item["sha256"] != expected:
            mismatched.append(key)
    return {**scope, "reference_bytes_available": not missing and not mismatched,
            "missing_paths": sorted(missing), "mismatched_paths": sorted(mismatched)}


def upgrade_artifact_catalog(source_root: Path, target_root: Path, *,
                             artifact_roots: ArtifactRoots | None = None, dry_run: bool = False) -> dict:
    """Copy, install, backfill and verify the optional catalog; never alter source.

    Cut over explicitly after validating the new target. A rollback keeps the
    original archive; new writes in the target form a separate branch of history
    and are not automatically merged back into the old source.
    """
    source = Path(os.path.abspath(source_root))
    target = Path(os.path.abspath(target_root))
    roots = artifact_roots or ArtifactRoots.of(source)
    if roots.archive_root != source:
        raise ArtifactCatalogUpgradeError("artifact roots belong to a different upgrade source")
    bases = roots.read_bases()
    for base in (*bases, target):
        require_no_links(base)
    _disjoint(target, bases)
    with ArchiveSession(source, mode=ArchiveAccessMode.MAINTENANCE, artifact_roots=roots) as session:
        validate_snapshot_database(session.database_path)
        if require_artifact_catalog(session.connection):
            raise ArtifactCatalogUpgradeError("source already has artifact-storage-v1; preserve or snapshot it instead of upgrading again")
        authority = authority_fingerprints(session.connection)
        fingerprint = hashlib.sha256(canonical(authority).encode("utf-8")).hexdigest()
        kind = runtime_contract(session.connection)
        audio_expectations = _audio_path_expectations(session.connection)
        if dry_run:
            return {"operation": "artifact-catalog-upgrade", "extension": EXTENSION, "source_contract": kind,
                    "source_fingerprint": fingerprint, "authority_tables": authority, "installed": False, "dry_run": True}
        with archive_access(target, exclusive=True):
            if (target / "archive.db").is_file():
                with ArchiveSession(target, mode=ArchiveAccessMode.READ) as existing:
                    require_artifact_catalog(existing.connection, required=True)
                    row = existing.connection.execute("SELECT * FROM artifact_catalog_upgrades").fetchone()
                    if row is None or row["source_fingerprint"] != fingerprint:
                        raise ArtifactCatalogUpgradeError("existing upgrade target belongs to another source baseline")
                    report_path = target.joinpath(*portable_artifact_parts(row["report_key"]))
                    require_regular_file(report_path)
                    raw = report_path.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != row["report_sha256"]:
                        raise ArtifactCatalogUpgradeError("installed upgrade report changed")
                    report = json.loads(raw)
                    _verify_installed(target, report)
                    if _source_inventory(bases) != report["source_files"]:
                        raise ArtifactCatalogUpgradeError("source artifact bytes differ from the installed upgrade baseline")
                    return {**report, "installed": True, "reused": True}
            _empty_target(target)
            source_files = _source_inventory(bases)
            # Matching authority bytes win the canonical slot; alternate bytes
            # remain preserved extras. Base order resolves only equal evidence.
            copy_order = sorted(source_files, key=lambda item: (item["path"], item["sha256"] != audio_expectations.get(item["path"], item["sha256"]), item["base_index"]))
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=f".{target.name}.artifact-upgrade-", dir=target.parent) as temporary:
                stage = Path(temporary) / "archive"
                stage.mkdir()
                database_path = stage / "archive.db"
                with closing(connect_database(database_path)) as converted:
                    deadline = time.monotonic() + 30
                    def progress(status, remaining, total):
                        if time.monotonic() > deadline:
                            raise ArtifactCatalogUpgradeError("database backup exceeded upgrade contention deadline")
                    session.connection.backup(converted, pages=256, progress=progress, sleep=0.05)
                    if authority_fingerprints(converted) != authority:
                        raise ArtifactCatalogUpgradeError("database backup did not preserve all typed authority rows")
                    inventory = []
                    inventory_by_key = {}
                    preserved_extras = []
                    for item in copy_order:
                        original_key = item["path"]
                        path = bases[item["base_index"]].joinpath(*portable_artifact_parts(original_key))
                        key = original_key
                        selected = inventory_by_key.get(original_key)
                        if selected is not None:
                            if (selected["size"], selected["sha256"]) == (item["size"], item["sha256"]):
                                continue
                            if original_key.startswith("audio/") and len(original_key.split("/")) == 2:
                                key = f"audio/catalog-copy-{item['sha256']}{Path(original_key).suffix}"
                            else:
                                key = f"documents/catalog-copies/base-{item['base_index']}/{original_key}.preserved"
                        output_path = stage.joinpath(*portable_artifact_parts(key))
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        require_regular_file(path)
                        if output_path.exists():
                            with output_path.open("rb") as reader:
                                if stream_hash(reader) == (item["size"], item["sha256"]):
                                    continue
                            raise ArtifactCatalogUpgradeError("preserved alternate copy key conflicts with existing bytes")
                        with path.open("rb") as reader, output_path.open("xb") as writer:
                            size, sha256 = stream_hash(reader, writer)
                            writer.flush()
                            os.fsync(writer.fileno())
                        if (size, sha256) != (item["size"], item["sha256"]):
                            raise ArtifactCatalogUpgradeError("source artifact changed after upgrade inventory")
                        with path.open("rb") as reader:
                            if stream_hash(reader) != (size, sha256):
                                raise ArtifactCatalogUpgradeError("source artifact changed during preservation copy")
                        with output_path.open("rb") as reader:
                            if stream_hash(reader) != (size, sha256):
                                raise ArtifactCatalogUpgradeError("copied artifact failed independent verification")
                        inventory.append({"path": key, "size": size, "sha256": sha256})
                        inventory_by_key[key] = inventory[-1]
                        if key != original_key:
                            preserved_extras.append(inventory[-1])
                    inventory.sort(key=lambda item: item["path"])
                    preserved_extras.sort(key=lambda item: item["path"])
                    now = int(time.time())
                    with converted:
                        findings = install_catalog_in_staged_copy(converted, {item["path"]: item for item in inventory}, now=now)
                    before = {item["table"] for item in authority}
                    if [item for item in authority_fingerprints(converted) if item["table"] in before] != authority:
                        raise ArtifactCatalogUpgradeError("catalog backfill changed original authority facts")
                    report = {"schema_version": 1, "operation": "artifact-catalog-upgrade", "extension": EXTENSION,
                              "source_contract": kind, "source_fingerprint": fingerprint, "authority_tables": authority,
                              "files": inventory, "source_files": source_files, "audio_findings": findings,
                              "preserved_extras": preserved_extras,
                              "snapshot_readiness": _snapshot_readiness(database_path, inventory),
                              "recovery_changes": [], "created_at": now}
                    upgrade_id = hashlib.sha256(canonical(report).encode("utf-8")).hexdigest()
                    report["upgrade_id"] = upgrade_id
                    report_key = f"documents/artifact-upgrades/{upgrade_id}/report.json"
                    report_path = stage.joinpath(*portable_artifact_parts(report_key))
                    report_path.parent.mkdir(parents=True)
                    raw = (canonical(report) + "\n").encode("utf-8")
                    with report_path.open("xb") as writer:
                        writer.write(raw)
                        writer.flush()
                        os.fsync(writer.fileno())
                    with converted:
                        converted.execute("INSERT INTO artifact_catalog_upgrades VALUES (?,?,?,?,?,?)",
                                          (upgrade_id, fingerprint, report_key, hashlib.sha256(raw).hexdigest(), digest(preserved_extras), now))
                        converted.executemany("INSERT INTO artifact_catalog_upgrade_files VALUES (?,?,?,?)",
                                              [(upgrade_id, item["path"], item["sha256"], item["size"]) for item in preserved_extras])
                if authority_fingerprints(session.connection) != authority:
                    raise ArtifactCatalogUpgradeError("source authority changed before upgrade installation")
                if _source_inventory(bases) != source_files:
                    raise ArtifactCatalogUpgradeError("source artifact inventory changed before upgrade installation")
                _verify_installed(stage, report)
                # The writable descriptor is required on Windows too; a read-only
                # descriptor can make _commit/fsync fail after otherwise good copy.
                with database_path.open("r+b") as database:
                    os.fsync(database.fileno())
                for directory in sorted((path for path in stage.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
                    _sync_directory(directory)
                _sync_directory(stage)
                _empty_target(target)
                if target.exists():
                    target.rmdir()
                stage.rename(target)
                warnings = []
                try:
                    _sync_directory(target.parent)
                except OSError:
                    warnings.append("target_parent_directory_sync_failed")
                return {**report, "installed": True, "reused": False, "warnings": warnings}
