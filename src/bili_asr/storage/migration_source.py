"""Fixed, read-only legacy archive inspection for an offline migration preflight.

The signatures are frozen from 9b28957, independent of the schema shipped by a
future build. This module deliberately does not use the runtime database opener.
Callers must stop writers and hold the archive access lock for the entire read.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import struct
import time
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from bili_asr.sql_identity import schema_sql_sha256


class MigrationSourceError(ValueError):
    """A legacy source cannot be inspected safely or is unsupported."""


@dataclass(frozen=True)
class TableFingerprint:
    name: str
    row_count: int
    sha256: str
    includes_rowid: bool = True


@dataclass(frozen=True)
class MigrationSourceReport:
    contract_id: str
    source_revision: str
    table_fingerprints: tuple[TableFingerprint, ...]
    derived_objects: tuple[str, ...]
    expired_running_jobs: int = 0


@lru_cache(maxsize=1)
def _contract() -> dict[str, Any]:
    return json.loads(resources.files("bili_asr.storage").joinpath(
        "migration-source-bilibili-v1.json"
    ).read_text(encoding="utf-8"))


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _reject_links(path: Path) -> None:
    """Inspect lexical ancestors before following any part of the input path."""
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise MigrationSourceError("legacy source path cannot contain symlinks or junctions")


def _source_state(path: Path) -> tuple[int, int, int, int, int]:
    _reject_links(path)
    if not path.is_file():
        raise MigrationSourceError("legacy archive database is missing")
    # immutable=1 never creates WAL/SHM files, but cannot incorporate pending
    # transactions. Require an offline checkpoint rather than omit their data.
    for suffix in ("-wal", "-journal"):
        companion = path.with_name(path.name + suffix)
        _reject_links(companion)
        if companion.exists() and companion.stat().st_size:
            raise MigrationSourceError(
                "legacy archive has a pending WAL or journal; stop writers and checkpoint first"
            )
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _validate_objects(connection: sqlite3.Connection, contract: dict[str, Any]) -> tuple[str, ...]:
    actual = {
        name: {"kind": kind, "sql_sha256": schema_sql_sha256(sql)}
        for kind, name, sql in connection.execute(
            "SELECT type, name, sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY name"
        )
    }
    required = contract["objects"]
    for name, shape in required.items():
        if name not in actual:
            raise MigrationSourceError(f"legacy source contract is missing {name}")
        if actual[name] != {key: shape[key] for key in ("kind", "sql_sha256")}:
            raise MigrationSourceError(f"legacy source contract is incompatible at {name}")
    extras = set(actual) - set(required)
    derived: set[str] = set()
    from bili_asr.storage.editorial_replay import derived_group
    for group in (*contract["derived_groups"], derived_group()):
        names = set(group["objects"])
        present = names & extras
        if not present:
            continue
        if present != names:
            raise MigrationSourceError("legacy source has an incomplete derived search index")
        for name, variants in group["objects"].items():
            if actual[name] not in variants:
                raise MigrationSourceError(f"legacy source has an unsupported derived object {name}")
        derived.update(names)
    unknown = sorted(extras - derived)
    if unknown:
        raise MigrationSourceError(f"legacy source has an unsupported object {unknown[0]}")
    if connection.execute("SELECT version FROM manuscript_contract").fetchall() != [(1,)]:
        raise MigrationSourceError("legacy source manuscript contract identity is invalid")
    return tuple(sorted(derived))


def _fingerprint(connection: sqlite3.Connection, name: str, columns: list[str]) -> TableFingerprint:
    expressions = ["rowid"]
    for column in columns:
        quoted = _identifier(column)
        expressions.extend((f"typeof({quoted})", quoted))
    digest = hashlib.sha256(b"bili-asr-legacy-table-fingerprint-v1\x00")
    # Include a schema-level prefix so rows from two tables cannot be confused.
    digest.update(json.dumps([name, columns], ensure_ascii=False, separators=(",", ":")).encode())
    count = 0
    for row in connection.execute(
        f"SELECT {', '.join(expressions)} FROM {_identifier(name)} ORDER BY rowid"
    ):
        digest.update(b"R" + struct.pack(">q", row[0]))
        for index in range(1, len(row), 2):
            kind, value = row[index:index + 2]
            kind = kind.decode("ascii")
            if kind == "null":
                payload = b""
            elif kind == "integer":
                payload = struct.pack(">q", value)
            elif kind == "real":
                payload = struct.pack(">d", value)
            else:
                payload = value
            digest.update(kind.encode("ascii") + b"\x00" + struct.pack(">Q", len(payload)))
            digest.update(payload)
        count += 1
    return TableFingerprint(name, count, digest.hexdigest())


def inspect_migration_source(database_path: Path) -> MigrationSourceReport:
    """Validate and fingerprint one stopped, checkpointed baseline database.

    All authoritative tables are streamed in effective rowid order, retaining
    SQLite storage types and exact text/blob bytes. Hashes expose no row values.
    The known FTS search cache is explicitly classified as rebuildable data.
    No schema initialization, write connection or source-side files are used.
    Database paths and their ancestors must contain no links or junctions.
    """
    # Keep lexical ancestors (including '..') for both link checks. Resolving
    # first would silently accept a database alias or a linked parent directory.
    path = Path(database_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        before = _source_state(path)
        contract = _contract()
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise MigrationSourceError("legacy source failed SQLite integrity validation")
            derived = _validate_objects(connection, contract)
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise MigrationSourceError("legacy source failed foreign-key validation")
            now = int(time.time())
            if connection.execute(
                "SELECT 1 FROM workflow_jobs WHERE status = 'running' "
                "AND (lease_expires_at IS NULL OR lease_expires_at > ?) LIMIT 1", (now,),
            ).fetchone() is not None:
                raise MigrationSourceError(
                    "legacy source has running workflow jobs with live or missing leases; stop workers first"
                )
            expired_running = connection.execute(
                "SELECT COUNT(*) FROM workflow_jobs WHERE status = 'running'",
            ).fetchone()[0]
            # SQLite supplies text as UTF-8 bytes for this factory, independent
            # of the database encoding. Keep bytes (even undecodable text) and
            # its storage type instead of parsing/normalizing frozen JSON.
            connection.text_factory = bytes
            fingerprints = tuple(
                _fingerprint(connection, name, shape["columns"])
                for name, shape in sorted(contract["objects"].items())
                if shape["kind"] == "table"
            )
        if _source_state(path) != before:
            raise MigrationSourceError("legacy source changed during inspection; stop writers first")
    except (OSError, sqlite3.Error) as exc:
        # SQLite's diagnostic can contain private CHECK values; do not expose it.
        raise MigrationSourceError("cannot read legacy archive database safely") from exc
    return MigrationSourceReport(
        contract["contract_id"], contract["source_revision"], fingerprints, derived, expired_running,
    )


def legacy_source_contract() -> dict:
    """Load the frozen source resource, never the active runtime initializer."""
    return _contract()


def table_fingerprint(connection: sqlite3.Connection, name: str, columns: list[str]) -> TableFingerprint:
    """Fingerprint original columns of a source or explicitly converted target.

    A target may add mapping columns, but caller-supplied identifiers must name
    columns of a registered authority table. SQLite storage types remain part
    of the digest; TEXT is read as UTF-8 bytes rather than normalized strings.
    """
    contract = _contract()
    shape = contract["objects"].get(name)
    if shape is None or shape["kind"] != "table" or columns != shape["columns"]:
        raise MigrationSourceError("unregistered source table fingerprint")
    return typed_table_fingerprint(connection, name, columns)


def typed_table_fingerprint(connection: sqlite3.Connection, name: str, columns: list[str]) -> TableFingerprint:
    """Stream exact rowid, storage types and bytes for validated source columns.

    The caller validates the archive contract before choosing its authority tables.
    Kept identical to the frozen migration digest so older receipts remain valid.
    """
    actual = [row[1] for row in connection.execute(f"PRAGMA table_info({_identifier(name)})")]
    if not columns or len(set(columns)) != len(columns) or not set(columns) <= set(actual):
        raise MigrationSourceError("invalid authority fingerprint columns")
    previous = connection.text_factory
    try:
        connection.text_factory = bytes
        return _fingerprint(connection, name, columns)
    finally:
        connection.text_factory = previous


def iter_legacy_rows(connection: sqlite3.Connection, name: str):
    """Stream (rowid, ((storage_type, raw_value), ...)) in source order.

    This is the materializer contract. A converter must bind TEXT through a
    CAST to retain its SQLite storage class, including undecodable UTF-8 bytes.
    """
    shape = _contract()["objects"].get(name)
    if shape is None or shape["kind"] != "table":
        raise MigrationSourceError("unregistered source table reader")
    expressions = ["rowid"]
    for column in shape["columns"]:
        expressions.extend((f"typeof({_identifier(column)})", _identifier(column)))
    previous = connection.text_factory
    try:
        connection.text_factory = bytes
        for row in connection.execute(f"SELECT {','.join(expressions)} FROM {_identifier(name)} ORDER BY rowid"):
            yield row[0], tuple((row[index].decode("ascii"), row[index + 1]) for index in range(1, len(row), 2))
    finally:
        connection.text_factory = previous


def typed_row_digest(name: str, rowid: int, cells, *, columns: list[str] | None = None) -> str:
    """Hash effective rowid, names, storage classes and exact raw cell bytes."""
    shape = _contract()["objects"].get(name)
    if shape is None or shape["kind"] != "table" or len(cells) != len(shape["columns"]):
        raise MigrationSourceError("unregistered typed source row")
    selected = shape["columns"] if columns is None else columns
    if any(column not in shape["columns"] for column in selected):
        raise MigrationSourceError("unregistered typed source column")
    digest = hashlib.sha256(b"bili-asr-imported-row-v1\x00" + struct.pack(">q",rowid))
    digest.update(json.dumps([name, selected],separators=(",",":")).encode("utf-8"))
    for column,(kind,value) in zip(shape["columns"],cells):
        if column not in selected:
            continue
        payload = (b"" if kind=="null" else struct.pack(">q",value) if kind=="integer"
                   else struct.pack(">d",value) if kind=="real" else value)
        digest.update(kind.encode("ascii") + b"\x00" + struct.pack(">Q",len(payload)) + payload)
    return digest.hexdigest()
