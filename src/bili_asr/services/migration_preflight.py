"""Read-only P0 inventory for stopped, checkpointed legacy archives."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from bili_asr.archive_maintenance import ArchiveAccessError
from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_inventory import (
    ARTIFACT_DIRECTORIES,
    ArtifactInventoryError,
    check_artifact_collisions,
    collect_artifacts,
    is_temporary_artifact,
    portable_artifact_parts,
    require_no_links,
    require_regular_file,
    stream_hash,
)
from bili_asr.services.runtime_root_policy import (
    ARTIFACT_RUNTIME,
    ASR_INPUT_RESERVATIONS,
    PRESERVED_IMPORT_LOCK,
    RuntimeRootPolicyError,
    inspect_runtime_root,
)
from bili_asr.storage.migration_artifacts import (
    LEGACY_BUNDLE_NAMES,
    LEGACY_BUNDLE_SCHEMA,
    LEGACY_MARKER,
    MigrationArtifactError,
    legacy_artifact_references,
    legacy_object,
)
from bili_asr.storage.migration_source import (
    MigrationSourceError,
    inspect_migration_source,
)


class MigrationPreflightError(ValueError):
    """The source cannot form a trustworthy offline migration inventory."""


# Names only: these are not read recursively or incorporated into the fingerprint.
_EXCLUDED_FILES = {
    ".env": "credentials", "credentials.json": "credentials",
    "manifest.json": "legacy-operational-manifest",
    "archive.db-wal": "sqlite-sidecar", "archive.db-shm": "sqlite-sidecar",
    "archive.db-journal": "sqlite-sidecar",
}
_EXCLUDED_DIRS = {"models": "model-weights", "logs": "operational-logs", "cache": "cache"}


def _identity(path: Path) -> tuple:
    info = require_regular_file(path)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _hash_file(path: Path) -> tuple[int, str, tuple]:
    before = _identity(path)
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != before[:2]:
            raise MigrationPreflightError("source file changed before reading")
        size, sha256 = stream_hash(stream)
    if _identity(path) != before or size != before[2]:
        raise MigrationPreflightError("source file changed while reading")
    return size, sha256, before


def _database_state(path: Path) -> tuple:
    for suffix in ("-wal", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            require_regular_file(sidecar)
            if sidecar.stat().st_size:
                raise MigrationPreflightError("pending WAL or journal: stop writers and checkpoint first")
    return _identity(path)


def _root_entries(base: Path, archive: Path, product: Path) -> list[dict]:
    excluded = []
    runtime = inspect_runtime_root(base)
    for entry in sorted(base.iterdir()):
        require_no_links(entry)
        if entry.name == "archive.db" and base == archive:
            require_regular_file(entry)
        elif entry == product and product != archive:
            continue  # The configured root is inspected separately.
        elif entry in product.parents and product.is_relative_to(archive) and product != archive:
            excluded.extend(_root_entries(entry, archive, product))
        elif entry.name == PRESERVED_IMPORT_LOCK or (entry.name in {ARTIFACT_RUNTIME, ASR_INPUT_RESERVATIONS} and entry.is_dir()):
            excluded.extend({**item, "base": str(base)} for item in runtime
                             if item["path"] == entry.name or item["path"].startswith(entry.name + "/"))
        elif entry.name in ARTIFACT_DIRECTORIES and entry.is_dir():
            continue
        elif entry.name in _EXCLUDED_FILES and entry.is_file():
            excluded.append({"path": entry.name, "base": str(base), "category": _EXCLUDED_FILES[entry.name]})
        elif entry.name in _EXCLUDED_DIRS and entry.is_dir():
            excluded.append({"path": entry.name, "base": str(base), "category": _EXCLUDED_DIRS[entry.name]})
        elif is_temporary_artifact(entry.name):
            raise MigrationPreflightError("unfinished temporary artifact at source root")
        else:
            raise MigrationPreflightError("unsupported source root entry; define an explicit preservation rule")
    return excluded


def _inventory_paths(bases: tuple[Path, ...]) -> list[tuple[str, Path, Path]]:
    paths = [(key, path, base) for base in bases for key, path in collect_artifacts((base,)).items()]
    check_artifact_collisions(sorted({key for key, _, _ in paths}))
    return sorted(paths, key=lambda item: (item[0], bases.index(item[2])))


def _verify_markers(selected: dict[str, dict], physical: dict[str, Path]) -> None:
    for marker, item in selected.items():
        if marker.split("/")[-1] != LEGACY_MARKER:
            continue
        parts = portable_artifact_parts(marker)
        if len(parts) != 3 or parts[0] != "transcripts" or item["size"] > 8192:
            raise MigrationPreflightError("invalid legacy bundle marker path or size")
        with physical[marker].open("rb") as stream:
            document = legacy_object(stream.read(8193))
        if (document.get("schema") != LEGACY_BUNDLE_SCHEMA
                or not isinstance(document.get("artifacts"), dict)
                or set(document["artifacts"]) != set(LEGACY_BUNDLE_NAMES)):
            raise MigrationPreflightError("invalid complete legacy bundle marker")
        directory = "/".join(parts[:-1])
        for key, basename in LEGACY_BUNDLE_NAMES.items():
            path = f"{directory}/{basename}"
            record = document["artifacts"][key]
            if (not isinstance(record, dict) or set(record) != {"path", "sha256"}
                    or record["path"] != path or path not in selected
                    or record["sha256"] != selected[path]["sha256"]):
                raise MigrationPreflightError("legacy bundle marker hash or path mismatch")


def migration_preflight(source_root: Path, *, artifact_root: Path | None = None) -> dict:
    """Inspect a fixed legacy source; never create a target or recover source jobs.

    Writers must already be stopped, including old processes that do not honor
    maintenance locks. Nonempty WAL/journals are refused, not checkpointed here.
    The only coordination write is the stable maintenance lock beside the root.
    """
    try:
        for requested in (source_root, artifact_root):
            if requested is not None:
                lexical = Path(requested)
                require_no_links(lexical if lexical.is_absolute() else Path.cwd() / lexical)
        root = Path(os.path.abspath(source_root))
        product = root if artifact_root is None else Path(os.path.abspath(artifact_root))
        for base in (root, product):
            require_no_links(base)
            if not base.is_dir():
                raise MigrationPreflightError("source archive or artifact root is not a directory")
        database = root / "archive.db"
        require_regular_file(database)  # Guard lexical path before any resolve/open.
        if product != root:
            if root.is_relative_to(product):
                raise MigrationPreflightError("artifact root cannot contain the source archive")
            if product.is_relative_to(root):
                first = product.relative_to(root).parts[0]
                if first in ARTIFACT_DIRECTORIES or first in _EXCLUDED_DIRS:
                    raise MigrationPreflightError("nested artifact root cannot overlap managed artifact or excluded directories")
        bases = tuple(dict.fromkeys((product, root)))
        with ArchiveSession(root, mode=ArchiveAccessMode.MAINTENANCE).access():
            database_before = _database_state(database)
            source = inspect_migration_source(database)
            _, database_sha256, _ = _hash_file(database)
            excluded = [entry for base in bases for entry in _root_entries(base, root, product)]
            paths = _inventory_paths(bases)
            selected, physical, identities, shadowed = {}, {}, {}, []
            for key, path, base in paths:
                size, sha256, identity = _hash_file(path)
                identities[path] = identity
                item = {"path": key, "size": size, "sha256": sha256, "base": str(base)}
                if key in selected:
                    shadowed.append({**item, "selected_base": selected[key]["base"]})
                else:
                    selected[key], physical[key] = item, path
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("PRAGMA trusted_schema=OFF")
                for path, expected in legacy_artifact_references(connection).items():
                    try:
                        portable_artifact_parts(path)
                    except ArtifactInventoryError as exc:
                        raise MigrationPreflightError("unsafe legacy artifact reference path") from exc
                    if path not in selected:
                        raise MigrationPreflightError(f"missing legacy artifact: {path}")
                    if any(expected[key] is not None and selected[path][key] != expected[key]
                           for key in ("sha256", "size")):
                        raise MigrationPreflightError(f"legacy artifact hash or size mismatch: {path}")
                recovery = {"expired_running_jobs": source.expired_running_jobs}
                for table in ("ingestion_runs", "acquisition_runs"):
                    recovery[f"unfinished_{table}"] = connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE outcome='running'"
                    ).fetchone()[0]
                recovery["unfinished_editorial_model_calls"] = connection.execute(
                    "SELECT COUNT(*) FROM editorial_model_calls WHERE finished_at IS NULL"
                ).fetchone()[0]
            _verify_markers(selected, physical)
            if (_inventory_paths(bases) != paths
                    or any(_identity(path) != identity for path, identity in identities.items())
                    or _database_state(database) != database_before
                    or [entry for base in bases for entry in _root_entries(base, root, product)] != excluded):
                raise MigrationPreflightError("source changed during preflight; stop all writers first")
            files = list(selected.values())
            fingerprint = hashlib.sha256(json.dumps({
                "contract": source.contract_id, "database": database_sha256,
                "files": [{key: value for key, value in item.items() if key != "base"} for item in files],
            }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            return {
                "operation": "migration-preflight", "report_version": 1, "valid": True,
                "mode": "stopped-checkpointed", "conversion_performed": False, "target_created": False,
                "source": {"archive_root": str(root), "artifact_root": str(product),
                           "contract_id": source.contract_id, "source_revision": source.source_revision,
                           "database_sha256": database_sha256, "fingerprint": fingerprint},
                "tables": [asdict(table) for table in source.table_fingerprints],
                "derived_objects": list(source.derived_objects), "files": files,
                "totals": {"table_rows": sum(table.row_count for table in source.table_fingerprints),
                           "file_count": len(files), "file_bytes": sum(item["size"] for item in files)},
                "excluded": excluded, "shadowed": shadowed, "recovery_candidates": recovery,
            }
    except (MigrationSourceError, MigrationArtifactError, ArtifactInventoryError,
            ArchiveAccessError, RuntimeRootPolicyError) as exc:
        raise MigrationPreflightError(str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        raise MigrationPreflightError("cannot inspect legacy source safely") from exc
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        if isinstance(exc, MigrationPreflightError):
            raise
        raise MigrationPreflightError("legacy frozen content is malformed") from exc
