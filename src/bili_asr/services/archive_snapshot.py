"""Portable, validated snapshots of the database and its external artifacts."""

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
from collections.abc import Iterator
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bili_asr import __version__
from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_inventory import (
    collect_artifacts as _collect_artifacts,
)
from bili_asr.artifact_inventory import (
    require_no_links as _no_links,
)
from bili_asr.artifact_inventory import (
    require_regular_file as _regular_file,
)
from bili_asr.artifact_inventory import (
    stream_hash as _stream_hash,
)
from bili_asr.services.migration_preflight import _root_entries
from bili_asr.services.runtime_root_policy import (
    RUNTIME_POLICY,
    RuntimeRootPolicyError,
    inspect_runtime_roots,
)
from bili_asr.services.snapshot_protocol import (
    SnapshotError,
    _absolute,
    _check_bundle_markers,
    _check_references,
    _empty_destination,
    _manifest_object,
    _read_manifest,
    _snapshot_errors,
    _sync_directory,
    _sync_file,
    _validate_into,
)
from bili_asr.storage.database import connect_database
from bili_asr.storage.snapshots import (
    create_database_snapshot,
    recover_interrupted_jobs,
    required_artifacts,
    validate_snapshot_database,
)

_FORMAT = "bili-asr-snapshot"
_FORMAT_VERSION = 1
_MANIFEST_NAME = "snapshot.json"
_MANIFEST_LIMIT = 16 * 1024 * 1024
_MARKER_LIMIT = 8192
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
















def _refuse_active_jobs(database: Path) -> None:
    # Source coordination belongs to save_snapshot; a private staging database
    # must not create a permanent maintenance lock beside its temporary root.
    connection = connect_database(database, readonly=True, must_exist=True)
    try:
        row = connection.execute(
            "SELECT job_id FROM workflow_jobs WHERE status = 'running' "
            "AND lease_expires_at > ? LIMIT 1", (int(time.time()),)
        ).fetchone()
        if row is not None:
            raise SnapshotError(f"workflow job is still running; stop its worker before saving: {row[0]}")
    finally:
        connection.close()


def _summary(manifest: dict[str, object]) -> dict[str, object]:
    files = manifest["files"]
    return {
        "snapshot_id": manifest["snapshot_id"],
        "created_at": manifest["created_at"],
        "database_contract": manifest["database_contract"],
        "file_count": len(files),
        "total_bytes": sum(file["size"] for file in files),
    }


def save_snapshot(archive_root: Path, out: Path, *, artifact_root: Path | None = None,
                  storage_targets: dict[str, Path] | None = None, compression: str = "stored") -> dict:
    """Save one consistent portable ZIP without changing its source archive."""
    with _snapshot_errors():
        if compression not in {"stored", "deflate"}:
            raise SnapshotError("snapshot compression must be stored or deflate")
        root, output = _absolute(archive_root), _absolute(out)
        bases = (root,) if artifact_root is None else tuple(dict.fromkeys((_absolute(artifact_root), root)))
        for base in bases:
            _no_links(base)
            if output.resolve().is_relative_to(base.resolve()):
                raise SnapshotError(f"snapshot output must be outside archive and artifact roots: {output}")
        _no_links(output)
        if output.exists():
            raise SnapshotError(f"snapshot output already exists: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        with ArchiveSession(root, mode=ArchiveAccessMode.MAINTENANCE).access():
            product = bases[0]
            for base in bases:
                _root_entries(base, root, product)
            source_database = root / "archive.db"
            _regular_file(source_database)
            validate_snapshot_database(source_database)
            _refuse_active_jobs(source_database)
            try:
                runtime_exclusions = inspect_runtime_roots(bases)
            except RuntimeRootPolicyError as exc:
                raise SnapshotError(str(exc)) from exc
            artifacts = _collect_artifacts(bases)
            with tempfile.TemporaryDirectory(prefix=f".{output.name}.snapshot-stage-", dir=output.parent) as temporary:
                stage = Path(temporary)
                database = stage / "archive.db"
                contract = create_database_snapshot(source_database, database)
                _refuse_active_jobs(database)
                external = {}
                if storage_targets:
                    from bili_asr.artifact_root import ArtifactRoots
                    from bili_asr.services.artifact_access import ArtifactAccess
                    with closing(connect_database(database, readonly=True)) as connection:
                        access = ArtifactAccess(connection, ArtifactRoots.of(root, artifact_root), storage_targets)
                        for key, expected in required_artifacts(database).items():
                            if key not in artifacts and expected is not None:
                                external[key] = access.locate_external(expected)
                if external:
                    needed = database.stat().st_size + sum(path.stat().st_size for path in artifacts.values()) + sum(item.size for item in external.values())
                    if shutil.disk_usage(stage).free < needed + 64 * 1024**2:
                        raise SnapshotError("snapshot target lacks space for the complete archive and container overhead")
                archive = stage / "snapshot.zip"
                files: list[dict[str, object]] = []
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED if compression == "deflate" else zipfile.ZIP_STORED,
                                     compresslevel=1 if compression == "deflate" else None, allowZip64=True) as bundle:
                    for key, source_path in sorted({"archive.db": database, **artifacts}.items()):
                        before = _regular_file(source_path)
                        with source_path.open("rb") as source, bundle.open(key, "w", force_zip64=True) as destination:
                            opened = os.fstat(source.fileno())
                            if (not stat.S_ISREG(opened.st_mode) or opened.st_ino != before.st_ino
                                    or opened.st_dev != before.st_dev):
                                raise SnapshotError(f"artifact changed before reading: {source_path}")
                            size, digest = _stream_hash(source, destination)
                        after = _regular_file(source_path)
                        if (before.st_size != size or before.st_size != after.st_size
                                or before.st_mtime_ns != after.st_mtime_ns or before.st_ino != after.st_ino):
                            raise SnapshotError(f"artifact changed while saving: {source_path}")
                        files.append({"path": key, "size": size, "sha256": digest})
                    for key, retained in sorted(external.items()):
                        with bundle.open(key, "w", force_zip64=True) as destination:
                            verified = retained.copy_to(destination)
                        files.append({"path": key, **verified})
                    _check_references(database, {file["path"]: file for file in files})
                    manifest: dict[str, object] = {
                        "format": _FORMAT,
                        "format_version": _FORMAT_VERSION,
                        "snapshot_id": str(uuid.uuid4()),
                        "created_at": datetime.now(UTC).isoformat(),
                        "producer_version": __version__,
                        "database_contract": contract,
                        "files": files,
                        "runtime_policy": RUNTIME_POLICY,
                        "excluded": runtime_exclusions,
                    }
                    encoded = json.dumps(manifest, ensure_ascii=True, indent=2).encode("utf-8")
                    if len(encoded) > _MANIFEST_LIMIT:
                        raise SnapshotError("snapshot file manifest exceeds 16 MiB")
                    bundle.writestr(_MANIFEST_NAME, encoded)
                with zipfile.ZipFile(archive, "r") as bundle:
                    _check_bundle_markers(bundle, {file["path"]: file for file in files})
                for base in bases:
                    _root_entries(base, root, product)
                with archive.open("r+b") as source:
                    os.fsync(source.fileno())
                # Both primitives atomically refuse a concurrently created output.
                if os.name == "nt":
                    os.rename(archive, output)
                else:
                    os.link(archive, output)
                _sync_directory(output.parent)
                return {"operation": "save", "snapshot": str(output), **_summary(manifest)}








@dataclass(frozen=True)
class VerifiedSnapshot:
    database_path: Path
    manifest: dict[str, object]


@contextmanager
def verified_snapshot_reader(snapshot: Path) -> Iterator[VerifiedSnapshot]:
    """Own a temporary database after validating every byte in the bundle."""
    with _snapshot_errors():
        path = _absolute(snapshot)
        with tempfile.TemporaryDirectory(prefix="bili-asr-snapshot-read-") as temporary:
            stage = Path(temporary)
            manifest = _validate_into(path, stage, extract_artifacts=False)
            yield VerifiedSnapshot(stage / "archive.db", manifest)


def check_snapshot(snapshot: Path) -> dict:
    """Stream and check every file, database contract and database reference."""
    with _snapshot_errors():
        path = _absolute(snapshot)
        with zipfile.ZipFile(path) as bundle:
            differential = "differential.json" in bundle.namelist()
        if differential:
            from bili_asr.services.maintenance_chain import verify_chain
            return verify_chain(path)
        with tempfile.TemporaryDirectory(prefix="bili-asr-snapshot-check-") as temporary:
            manifest = _validate_into(path, Path(temporary), extract_artifacts=False)
        return {"operation": "check", "snapshot": str(path), "valid": True, **_summary(manifest)}




def restore_snapshot(snapshot: Path, archive_root: Path, *, report_path: Path | str | None = None) -> dict:
    """Validate a complete staged archive, recover interrupted work and publish it."""
    with _snapshot_errors():
        path, root = _absolute(snapshot), _absolute(archive_root)
        _no_links(root)
        if path.resolve().is_relative_to(root.resolve()):
            raise SnapshotError("restore target cannot contain the source snapshot")
        report = None if report_path is None else _absolute(Path(report_path))
        if report is not None:
            _no_links(report)
            if report == path or report.is_relative_to(root) or report.exists():
                raise SnapshotError("recovery report must be a new file outside the archive and source snapshot")
            if not report.parent.is_dir():
                raise SnapshotError("recovery report parent must already exist")
        root.parent.mkdir(parents=True, exist_ok=True)
        with ArchiveSession(root, mode=ArchiveAccessMode.MAINTENANCE).access(allow_missing=True):
            _empty_destination(root)
            with tempfile.TemporaryDirectory(prefix=f".{root.name}.restore-stage-", dir=root.parent) as temporary:
                temporary_root = Path(temporary)
                stage = temporary_root / "archive"
                stage.mkdir()
                manifest = _validate_into(path, stage)
                audit = temporary_root / "recovery.ndjson"
                with audit.open("x", encoding="utf-8", newline="\n") as stream:
                    def record(change):
                        stream.write(json.dumps({"type": "recovery_change", **change}, sort_keys=True) + "\n")
                    recovered = recover_interrupted_jobs(stage / "archive.db", manifest["snapshot_id"],
                                                         audit_sink=record if report is not None else None)
                    stream.write(json.dumps({"type": "recovery_committed", "snapshot_id": manifest["snapshot_id"],
                                             "recovered": recovered}, sort_keys=True) + "\n")
                    _sync_file(stream)
                validate_snapshot_database(stage / "archive.db", expected_contract=manifest["database_contract"])
                with (stage / "archive.db").open("r+b") as database:
                    os.fsync(database.fileno())
                for directory, _children, _files in os.walk(stage, topdown=False):
                    _sync_directory(Path(directory))
                # Reserve and persist the audit before installation. A report
                # collision or write failure still leaves the target untouched.
                with (report.open("xb", buffering=0) if report is not None else nullcontext(None)) as destination:
                    if destination is not None:
                        with audit.open("rb") as source:
                            for block in iter(lambda: source.read(1024 * 1024), b""):
                                destination.write(block)
                        _sync_file(destination)
                    if _empty_destination(root):
                        root.rmdir()
                    stage.rename(root)
                    result = {"operation": "restore", "archive_root": str(root), "installed": True,
                              "recovered": recovered, **_summary(manifest)}
                    warnings = []
                    try:
                        _sync_directory(root.parent)
                    except OSError:
                        warnings.append("installed_directory_sync_failed")
                    if destination is not None:
                        try:
                            destination.write((json.dumps({"type": "archive_installed", "snapshot_id": manifest["snapshot_id"]},
                                                          sort_keys=True) + "\n").encode())
                            _sync_file(destination)
                            _sync_directory(report.parent)
                            result["recovery_report_written"] = True
                        except OSError:
                            result["recovery_report_written"] = False
                            warnings.append("installed_recovery_report_failed")
                    if warnings:
                        result["warnings"] = warnings
                    return result




__all__ = [
    'SnapshotError',
    'VerifiedSnapshot',
    '_absolute',
    '_check_bundle_markers',
    '_check_references',
    '_empty_destination',
    '_manifest_object',
    '_read_manifest',
    '_snapshot_errors',
    '_sync_directory',
    '_sync_file',
    '_validate_into',
    'check_snapshot',
    'restore_snapshot',
    'save_snapshot',
    'verified_snapshot_reader',
]
