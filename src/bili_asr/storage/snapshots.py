"""Read-only archive contracts, database backups, and restored-job recovery."""

from __future__ import annotations

from contextlib import closing
from functools import lru_cache
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import time
from typing import Any

from bili_asr.artifacts import BUNDLE_MARKER_NAME, REQUIRED_ARTIFACT_KEYS, owns_bundle_paths
from bili_asr.artifact_inventory import ArtifactInventoryError, portable_artifact_parts
from bili_asr.storage.database import (
    _normalize_view_sql,
    _strip_sql_comments,
    initialize_schema,
    connect_database,
    require_manuscript_schema,
    SchemaContractError,
)


_BACKUP_TIMEOUT_SECONDS = 10.0
_PUBLICATION_KEYS = REQUIRED_ARTIFACT_KEYS
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class SnapshotDatabaseError(ValueError):
    """The archive database or one of its portable references is invalid."""


def _connect(database_path: Path, *, readonly: bool = True) -> sqlite3.Connection:
    path = Path(database_path).resolve()
    if not path.is_file():
        raise SnapshotDatabaseError(f"archive database is missing: {path}")
    connection = connect_database(path, readonly=readonly, must_exist=True, busy_timeout_ms=200)
    connection.row_factory = None
    return connection


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _checks(sql: str) -> list[str]:
    """Read CHECK bodies; PRAGMA exposes columns/FKs but not CHECK constraints."""
    text = _strip_sql_comments(sql)
    checks: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char in "'\"`[":
            closing = "]" if char == "[" else char
            index += 1
            while index < len(text):
                if text[index] == closing:
                    if closing != "]" and text[index:index + 2] == closing * 2:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        match = re.match(r"CHECK\s*\(", text[index:], flags=re.I)
        if match is None or (index and (text[index - 1].isalnum() or text[index - 1] == "_")):
            index += 1
            continue
        start = index + match.end()
        cursor, depth = start, 1
        quote: str | None = None
        while cursor < len(text) and depth:
            current = text[cursor]
            if quote is not None:
                if current == quote:
                    if quote != "]" and text[cursor:cursor + 2] == quote * 2:
                        cursor += 2
                        continue
                    quote = None
            elif current in "'\"`[":
                quote = "]" if current == "[" else current
            elif current == "(":
                depth += 1
            elif current == ")":
                depth -= 1
            cursor += 1
        if depth:
            raise SnapshotDatabaseError("malformed CHECK constraint in archive schema")
        checks.append(_normalize_view_sql(text[start:cursor - 1]))
        index = cursor
    return sorted(checks)


def _index_shape(connection: sqlite3.Connection, name: str) -> list[list[Any]]:
    return [
        [row[2], row[3], row[4], row[5]]
        for row in connection.execute(f"PRAGMA index_xinfo({_identifier(name)})")
    ]


def _table_shape(connection: sqlite3.Connection, name: str, sql: str) -> dict[str, Any]:
    columns = [list(row[1:]) for row in connection.execute(
        f"PRAGMA table_xinfo({_identifier(name)})"
    )]
    foreign_keys = [list(row[1:]) for row in connection.execute(
        f"PRAGMA foreign_key_list({_identifier(name)})"
    )]
    constraints = [
        [row[2], row[3], row[4], _index_shape(connection, row[1])]
        for row in connection.execute(f"PRAGMA index_list({_identifier(name)})")
        if row[3] in ("u", "pk")
    ]
    flags = [list(row[3:]) for row in connection.execute("PRAGMA table_list")
             if row[0] == "main" and row[1] == name]
    return {
        # Match the runtime's strict table contract so a restored archive also
        # passes open_database, including historical ALTER layouts it rejects.
        "sql": _normalize_view_sql(sql),
        "columns": columns,
        "foreign_keys": sorted(foreign_keys, key=repr),
        "constraints": sorted(constraints, key=repr),
        "checks": _checks(sql),
        "flags": flags,
    }


def _schema(connection: sqlite3.Connection) -> dict[str, Any]:
    from bili_asr.storage.editorial_replay import objects as replay_objects, require_indexes
    require_indexes(connection)
    derived = replay_objects()
    objects: dict[str, Any] = {}
    for kind, name, sql in connection.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        if name in derived:
            continue
        if kind == "table":
            body: Any = _table_shape(connection, name, sql)
        else:
            body = _normalize_view_sql(sql)
        objects[name] = {"kind": kind, "body": body}
    return objects


@lru_cache(maxsize=128)
def _current_contract(kind: str = "bilibili-v1", imports: bool = False, supplements: bool = False,
                      artifacts: bool = False, online: bool = False,
                      rights: bool = False, observations: bool = False) -> tuple[dict[str, Any], str]:
    from bili_asr.contracts.fingerprints import database_fingerprint
    registered = database_fingerprint(kind, imports, supplements, artifacts, online, rights, observations)
    from bili_asr.storage.archive_contracts import BILIBILI_V1, bootstrap_contract, _resource
    with closing(sqlite3.connect(":memory:")) as connection:
        if kind == BILIBILI_V1:
            initialize_schema(connection)
        else:
            bootstrap_contract(connection, kind)
        if imports:
            from bili_asr.storage.archive_contracts import _resource
            connection.executescript(_resource("schema-preserved-body-import.sql"))
        if supplements:
            connection.executescript(_resource("schema-source-supplements.sql"))
        if artifacts:
            from bili_asr.storage.archive_contracts import _resource
            connection.executescript(_resource("schema-artifact-storage.sql"))
        if online:
            connection.executescript(_resource("schema-artifact-online.sql"))
        if rights:
            connection.executescript(_resource("schema-rights.sql"))
        if observations:
            connection.executescript(_resource("schema-source-observations.sql"))
        required = _schema(connection)
    canonical = json.dumps(required, sort_keys=True, separators=(",", ":"))
    computed = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if computed != registered:
        raise SnapshotDatabaseError(f"registered snapshot schema has drifted: {kind}; define a new contract")
    return required, registered


def validate_snapshot_database(database_path: Path, expected_contract: str | None = None) -> str:
    """Validate a registered database contract without changing its source.

    Snapshot package version and database schema contract are independent.
    Unmarked Bilibili snapshots keep their original contract digest.
    """
    from bili_asr.storage.archive_contracts import runtime_contract
    try:
        with closing(_connect(database_path)) as connection:
            from bili_asr.storage.import_origins import require_import_extension
            from bili_asr.storage.source_supplements import require_supplement_extension
            from bili_asr.storage.artifact_catalog import require_artifact_catalog
            from bili_asr.storage.artifact_online import require_artifact_online
            rights_objects = {
                "content_rights_events", "idx_content_rights_edition", "idx_content_rights_part",
                "immutable_content_rights_update", "immutable_content_rights_delete",
            }
            existing_rights_objects = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE sql IS NOT NULL"
                ).fetchall() if row[0] in rights_objects
            }
            if existing_rights_objects and existing_rights_objects != rights_objects:
                missing = sorted(rights_objects - existing_rights_objects)
                raise SnapshotDatabaseError(
                    "content-rights-v1 extension is incomplete: missing " + ", ".join(missing)
                )
            rights = bool(existing_rights_objects)
            from bili_asr.storage.source_observations import require_observations, ObservationRepository, verify_frozen_source_bindings
            observations = require_observations(connection)
            required, contract = _current_contract(runtime_contract(connection), require_import_extension(connection),
                                                   require_supplement_extension(connection), require_artifact_catalog(connection),
                                                   require_artifact_online(connection), rights, observations)
            ObservationRepository(connection).verify()
            verify_frozen_source_bindings(connection)
            if runtime_contract(connection) == "universal-v2":
                from bili_asr.storage.source_projection import read_youtube_fields_many
                cursor = connection.execute("SELECT DISTINCT external_id FROM source_metadata_observations WHERE platform='youtube'")
                while batch := cursor.fetchmany(256):
                    read_youtube_fields_many(connection, tuple(row[0] for row in batch))
                from bili_asr.storage.metadata_acceptance import read_bindings_many
                cursor = connection.execute("SELECT DISTINCT external_id FROM source_metadata_observations WHERE platform='bilibili'")
                while batch := cursor.fetchmany(256):
                    read_bindings_many(connection, tuple(row[0] for row in batch))
            if expected_contract is not None and expected_contract != contract:
                raise SnapshotDatabaseError("snapshot database contract is unsupported by this build")
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            if integrity != [("ok",)]:
                details = "; ".join(str(row[0]) for row in integrity[:5])
                raise SnapshotDatabaseError(f"archive database integrity check failed: {details}")
            actual = _schema(connection)
            for name, shape in actual.items():
                if shape["kind"] == "trigger" and name not in required:
                    raise SnapshotDatabaseError(f"archive database has unsupported trigger {name}")
            for name, shape in required.items():
                if name not in actual:
                    raise SnapshotDatabaseError(f"archive database contract is missing {name}")
                if actual[name] != shape:
                    raise SnapshotDatabaseError(f"archive database contract is incompatible at {name}")
            require_manuscript_schema(connection)
            violation = connection.execute("PRAGMA foreign_key_check").fetchone()
            if violation is not None:
                raise SnapshotDatabaseError(f"archive database foreign-key check failed: {violation}")
    except (sqlite3.Error, SchemaContractError) as exc:
        raise SnapshotDatabaseError(f"cannot validate archive database: {exc}") from exc
    return contract


def create_database_snapshot(source_db: Path, target_db: Path) -> str:
    """Back up a read-only source, refusing overwrite and indefinitely busy locks."""
    source_db, target_db = Path(source_db), Path(target_db)
    if source_db.resolve() == target_db.resolve():
        raise SnapshotDatabaseError("database snapshot target must differ from its source")
    created = False
    try:
        with closing(_connect(source_db)) as source:
            with target_db.open("xb"):
                created = True
            with closing(_connect(target_db, readonly=False)) as target:
                deadline = time.monotonic() + _BACKUP_TIMEOUT_SECONDS

                def progress(status: int, remaining: int, total: int) -> None:
                    if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and time.monotonic() >= deadline:
                        raise SnapshotDatabaseError("database snapshot timed out waiting for a database lock")

                source.backup(target, pages=256, progress=progress, sleep=0.05)
        return validate_snapshot_database(target_db)
    except (OSError, sqlite3.Error, SnapshotDatabaseError) as exc:
        if created:
            target_db.unlink(missing_ok=True)
        if isinstance(exc, SnapshotDatabaseError):
            raise
        raise SnapshotDatabaseError(f"cannot create database snapshot: {exc}") from exc


def _portable_key(value: object) -> str:
    try:
        portable_artifact_parts(value)
    except ArtifactInventoryError as exc:
        raise SnapshotDatabaseError(f"invalid portable artifact path: {value!r}") from exc
    return value


def _digest(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise SnapshotDatabaseError(f"invalid artifact SHA-256: {value!r}")
    return value


def _object(value: str, description: str) -> dict[str, Any]:
    try:
        result = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise SnapshotDatabaseError(f"invalid {description} JSON") from exc
    if not isinstance(result, dict):
        raise SnapshotDatabaseError(f"{description} must be a JSON object")
    return result


def required_artifacts(database_path: Path) -> dict[str, str | None]:
    """Read all required file references, including audio attempt dedup paths."""
    validate_snapshot_database(database_path)
    result: dict[str, str | None] = {}

    def add(path: object, digest: object = None) -> None:
        key, expected = _portable_key(path), _digest(digest)
        previous = result.get(key)
        if previous is not None and expected is not None and previous != expected:
            raise SnapshotDatabaseError(f"conflicting artifact hashes for {key}")
        result[key] = previous if previous is not None else expected

    try:
        with closing(_connect(database_path)) as connection:
            for key, digest in connection.execute("SELECT storage_key, sha256 FROM audio_objects"):
                add(key, digest)
            for (raw,) in connection.execute(
                "SELECT a.result_json FROM workflow_attempts AS a "
                "JOIN workflow_jobs AS j ON j.job_id = a.job_id "
                "WHERE a.outcome = 'succeeded' AND j.kind = 'audio'"
            ):
                payload = _object(raw, "successful audio attempt result")
                add(payload.get("storage_key"), payload.get("sha256"))
            for (raw,) in connection.execute("SELECT artifact_json FROM workflow_publications"):
                payload = _object(raw, "workflow publication artifacts")
                for key in _PUBLICATION_KEYS:
                    add(payload.get(key))
                if set(payload) != set(_PUBLICATION_KEYS) or not owns_bundle_paths(payload):
                    raise SnapshotDatabaseError("workflow publication artifacts do not describe one complete bundle")
                add(str(PurePosixPath(payload["srt_path"]).parent / BUNDLE_MARKER_NAME))
            for key, digest in connection.execute(
                "SELECT relative_path, content_sha256 FROM document_artifacts"
            ):
                add(key, digest)
            from bili_asr.storage.archive_contracts import UNIVERSAL_V2, runtime_contract
            from bili_asr.storage.artifact_catalog import require_artifact_catalog

            if require_artifact_catalog(connection):
                for object_id, in connection.execute("SELECT DISTINCT object_id FROM artifact_group_members"):
                    add(f"documents/artifact-objects/{object_id}", object_id)
                for key, sha256 in connection.execute("SELECT p.relative_key,m.object_id FROM artifact_publication_groups b JOIN workflow_publications w USING(publication_id) JOIN artifact_group_paths p USING(group_id) JOIN artifact_group_members m ON m.group_id=p.group_id AND m.role=p.role WHERE NOT EXISTS (SELECT 1 FROM workflow_publications newer WHERE newer.video_part_id=w.video_part_id AND (newer.published_at>w.published_at OR (newer.published_at=w.published_at AND newer.publication_id>w.publication_id)))"):
                    add(key, sha256)
                for key, digest in connection.execute("SELECT report_key,report_sha256 FROM artifact_catalog_upgrades"):
                    add(key, digest)
                for key, digest in connection.execute("SELECT relative_key,sha256 FROM artifact_catalog_upgrade_files"):
                    add(key, digest)

            if runtime_contract(connection) == UNIVERSAL_V2:
                for migration_id, raw in connection.execute(
                    "SELECT migration_id, report_json FROM migration_records"
                ):
                    report = _object(raw, "migration report")
                    if report.get("migration_id") != migration_id:
                        raise SnapshotDatabaseError("migration report identity differs from its record")
                    for name in ("id_mapping", "imported_rows"):
                        member = report.get(name)
                        if not isinstance(member, dict):
                            raise SnapshotDatabaseError("migration report omits its audit inventory")
                        add(member.get("path"), member.get("sha256"))
                    report_key = f"documents/migrations/{migration_id}/migration-report.json"
                    add(report_key, hashlib.sha256((raw + "\n").encode("utf-8")).hexdigest())
            # All immutable release files travel with the archive, including
            # superseded and withdrawn history. Verify their approved identity
            # without materializing artifacts during a streamed snapshot check.
            from bili_asr.storage.publication import PublicationRepository, verify_release_identity

            connection.row_factory = sqlite3.Row
            repository = PublicationRepository(connection)
            from bili_asr.storage.import_origins import require_import_extension, read_baseline
            if require_import_extension(connection):
                with closing(connection.execute("SELECT import_id FROM manuscript_import_baselines")) as baselines:
                    for row in baselines:
                        baseline = read_baseline(connection, row["import_id"])
                        add(baseline["body_path"], baseline["body_sha256"])
                        add(baseline["review_path"], baseline["review_sha256"])
                from bili_asr.storage.publication import read_edition
                with closing(connection.execute("SELECT edition_id FROM publication_import_origins")) as origins:
                    for row in origins:
                        read_edition(connection, row["edition_id"])
            # Close the active statement even when integrity verification
            # raises; otherwise Windows cannot remove the staged database and
            # its cleanup error masks the real release-integrity diagnostic.
            with closing(connection.execute("SELECT release_id FROM publication_releases")) as releases:
                for row in releases:
                    release = repository.release(row["release_id"])
                    verify_release_identity(connection, release)
                    add(release["relative_path"], release["artifact_sha256"])
            invalid_head = connection.execute(
                "SELECT h.video_part_id FROM publication_heads h "
                "LEFT JOIN publication_releases r ON r.release_id = h.current_release_id "
                "WHERE h.current_release_id IS NOT NULL AND "
                "(r.release_id IS NULL OR r.video_part_id != h.video_part_id OR r.status != 'published') LIMIT 1"
            ).fetchone()
            if invalid_head is not None:
                raise SnapshotDatabaseError("publication-integrity: effective release head is invalid")
    except sqlite3.Error as exc:
        raise SnapshotDatabaseError(f"cannot read archive artifact references: {exc}") from exc
    except ValueError as exc:
        if isinstance(exc, SnapshotDatabaseError):
            raise
        raise SnapshotDatabaseError(f"cannot validate manuscript artifact references: {exc}") from exc
    return result


_RECOVERY_TABLES = (
    ("workflow_attempts", "attempt_id", "outcome", "outcome = 'running'", "failed"),
    ("workflow_jobs", "job_id", "status", "status = 'running'", "queued"),
    ("ingestion_runs", "run_id", "outcome", "outcome = 'running'", "failed"),
    ("acquisition_runs", "run_id", "outcome", "outcome = 'running'", "failed"),
    ("editorial_model_calls", "call_id", "finished_at", "finished_at IS NULL", "finished"),
)


def preview_interrupted_jobs(connection: sqlite3.Connection, snapshot_id: str):
    """Yield row-level recovery decisions without changing scheduling state."""
    for table, key, column, predicate, target in _RECOVERY_TABLES:
        cursor = connection.execute(f"SELECT {key}, {column} FROM {table} WHERE {predicate} ORDER BY {key}")
        while rows := cursor.fetchmany(256):
            for identity, previous in rows:
                yield {"entity": table, "identity": identity,
                       "before": previous, "after": target,
                       "snapshot_id": snapshot_id, "reason": "interrupted_by_restore"}


def recover_interrupted_jobs(database_path: Path, snapshot_id: str, *, audit_sink=None) -> dict[str, int]:
    """Recover only a restored staging DB, leaving all completed history intact."""
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise SnapshotDatabaseError("snapshot_id must be non-empty")
    validate_snapshot_database(database_path)
    now = int(time.time())
    counts: dict[str, int] = {}
    try:
        with closing(_connect(database_path, readonly=False)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                # The sink belongs to private staging; publish its report only
                # after recovery commits and the restored archive is installed.
                if audit_sink is not None:
                    for change in preview_interrupted_jobs(connection, snapshot_id):
                        audit_sink(change)
                attempts = connection.execute(
                    "SELECT attempt_id, result_json FROM workflow_attempts WHERE outcome = 'running'"
                ).fetchall()
                for attempt_id, raw in attempts:
                    evidence = {} if raw is None else _object(raw, "running attempt result")
                    evidence["snapshot_recovery"] = {
                        "snapshot_id": snapshot_id, "reason": "interrupted_by_restore"
                    }
                    connection.execute(
                        "UPDATE workflow_attempts SET outcome = 'failed', "
                        "finished_at = max(started_at, ?), error_code = 'snapshot_restored', result_json = ? "
                        "WHERE attempt_id = ?",
                        (now, json.dumps(evidence, sort_keys=True, separators=(",", ":")), attempt_id),
                    )
                counts["workflow_attempts"] = len(attempts)
                counts["workflow_jobs"] = connection.execute(
                    "UPDATE workflow_jobs SET status = 'queued', lease_owner = NULL, "
                    "lease_expires_at = NULL, available_at = ?, updated_at = ?, "
                    "last_error_code = 'snapshot_restored' WHERE status = 'running'", (now, now)
                ).rowcount
                for table in ("ingestion_runs", "acquisition_runs"):
                    counts[table] = connection.execute(
                        f"UPDATE {table} SET outcome = 'failed', finished_at = max(started_at, ?) "
                        "WHERE outcome = 'running'", (now,)
                    ).rowcount
                counts["editorial_model_calls"] = connection.execute(
                    "UPDATE editorial_model_calls SET finished_at = max(started_at, ?), "
                    "error_code = 'snapshot_restored' WHERE finished_at IS NULL", (now,)
                ).rowcount
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    except sqlite3.Error as exc:
        raise SnapshotDatabaseError(f"cannot recover restored archive database: {exc}") from exc
    return counts


__all__ = [
    "SnapshotDatabaseError",
    "create_database_snapshot",
    "recover_interrupted_jobs",
    "preview_interrupted_jobs",
    "required_artifacts",
    "validate_snapshot_database",
]
