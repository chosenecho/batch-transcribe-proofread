"""Read-only logical and physical storage composition of one archive database.

This report answers "what occupies the archive database" without rewriting,
compacting or archiving any evidence.  It separates UTF-8 payload bytes from
SQLite character counts, database pages and file bytes so that a growth or
cold-archive decision is based on measured facts rather than table names.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import zlib
from datetime import datetime, timezone

SCHEMA = "archive-storage-report/v1"
_TIME_COLUMNS = (
    "created_at", "finished_at", "published_at", "assessed_at",
    "observed_at", "started_at", "updated_at",
)
_MAX_SAMPLES = 64
_MAX_TOP = 100


def _quote(identifier: str) -> str:
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("storage report requires non-empty SQL identifiers")
    return '"' + identifier.replace('"', '""') + '"'


def _bounded(value: int, name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer from 1 to {maximum}")
    return value


def _column_kind(declared_type: str) -> str:
    declared = declared_type.upper()
    if "BLOB" in declared or not declared:
        return "blob"
    if any(token in declared for token in ("TEXT", "CHAR", "CLOB", "JSON")):
        return "text"
    return "other"


def _columns(connection: sqlite3.Connection, table: str) -> list[tuple[str, str, str]]:
    rows = connection.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    return [(str(row[1]), str(row[2] or ""), _column_kind(str(row[2] or ""))) for row in rows]


def _tables(connection: sqlite3.Connection) -> list[str]:
    return [str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _bytes_expression(name: str, kind: str) -> str:
    quoted = _quote(name)
    if kind == "blob":
        return f"SUM(length({quoted}))"
    if kind == "text":
        return f"SUM(length(CAST({quoted} AS BLOB)))"
    return "NULL"


def _characters_expression(name: str, kind: str) -> str:
    return f"SUM(length({_quote(name)}))" if kind == "text" else "NULL"


def _table_statistics(connection: sqlite3.Connection, table: str,
                      columns: list[tuple[str, str, str]]) -> dict:
    expressions = ["COUNT(*)"]
    for name, _declared, kind in columns:
        expressions.append(_bytes_expression(name, kind))
        expressions.append(_characters_expression(name, kind))
    row = connection.execute(
        f"SELECT {', '.join(expressions)} FROM {_quote(table)}").fetchone()
    values = list(row)
    column_reports = []
    logical_bytes = 0
    for index, (name, declared, _kind) in enumerate(columns):
        byte_count = values[1 + index * 2] or 0
        character_count = values[2 + index * 2]
        if byte_count:
            logical_bytes += int(byte_count)
        column_reports.append({
            "name": name,
            "declared_type": declared,
            "logical_bytes": int(byte_count) if byte_count else 0,
            "characters": int(character_count) if character_count is not None else None,
        })
    return {"name": table, "rows": int(values[0]), "logical_bytes": logical_bytes,
            "columns": column_reports}


def _page_objects(connection: sqlite3.Connection) -> list[dict] | None:
    try:
        rows = connection.execute(
            "SELECT name, SUM(pgsize) AS bytes, COUNT(*) AS pages "
            "FROM dbstat GROUP BY name ORDER BY bytes DESC, name").fetchall()
    except sqlite3.OperationalError:
        return None
    return [{"name": str(row[0]), "bytes": int(row[1]), "pages": int(row[2])} for row in rows]


def _contracts(connection: sqlite3.Connection) -> dict:
    result = {"archive_contract": None, "manuscript_contract": None}
    for table, column in (("archive_contract", "contract"), ("manuscript_contract", "version")):
        try:
            row = connection.execute(f"SELECT {column} FROM {_quote(table)} LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            continue
        if row is not None:
            result[table] = row[0]
    return result


def _time_column(columns: list[tuple[str, str, str]]) -> str | None:
    names = {name for name, _declared, _kind in columns}
    for candidate in _TIME_COLUMNS:
        if candidate in names:
            return candidate
    return None


def _growth(connection: sqlite3.Connection, table: str,
            columns: list[tuple[str, str, str]], time_column: str) -> dict | None:
    byte_expressions = [f"COALESCE({_bytes_expression(name, kind)}, 0)"
                        for name, _declared, kind in columns if kind != "other"]
    total_expression = " + ".join(byte_expressions) if byte_expressions else "0"
    month_expression = f"strftime('%Y-%m', {_quote(time_column)}, 'unixepoch')"
    rows = connection.execute(
        f"SELECT {month_expression} AS month, COUNT(*), COALESCE({total_expression}, 0) "
        f"FROM {_quote(table)} GROUP BY month ORDER BY month").fetchall()
    return {"table": table, "time_column": time_column, "months": [
        {"month": "unknown" if row[0] is None else str(row[0]), "rows": int(row[1]),
         "logical_bytes": int(row[2])} for row in rows]}


def _content_statistics(connection: sqlite3.Connection, table: str, column: str,
                        *, total_rows: int, sample_rows: int) -> dict:
    quoted = _quote(column)
    digest_bytes: dict[bytes, int] = {}
    total_bytes = 0
    rows = 0
    sampled = 0
    sample_raw = 0
    sample_compressed = 0
    interval = max(1, total_rows // sample_rows) if total_rows else 1
    for value in connection.execute(
            f"SELECT {quoted} FROM {_quote(table)} WHERE {quoted} IS NOT NULL"):
        raw = value[0]
        if raw is None:
            continue
        payload = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        size = len(payload)
        total_bytes += size
        rows += 1
        digest = hashlib.sha256(payload).digest()
        if sampled < sample_rows and (rows - 1) % interval == 0:
            sample_raw += size
            sample_compressed += len(zlib.compress(payload, 6))
            sampled += 1
        if digest not in digest_bytes:
            digest_bytes[digest] = size
    duplicate_bytes = total_bytes - sum(digest_bytes.values())
    ratio = (sample_compressed / sample_raw) if sample_raw else None
    return {
        "table": table,
        "column": column,
        "rows": rows,
        "logical_bytes": total_bytes,
        "distinct_contents": len(digest_bytes),
        "duplicate_bytes": duplicate_bytes,
        "duplicate_ratio": (duplicate_bytes / total_bytes) if total_bytes else 0.0,
        "compression_sample": {
            "rows": sampled,
            "raw_bytes": sample_raw,
            "compressed_bytes": sample_compressed,
            "ratio": ratio,
            "projected_compressed_bytes": (
                round(total_bytes * ratio) if ratio is not None else None),
        },
    }


def _sidecars(database_path: str | None) -> dict:
    if not database_path:
        return {}
    result = {}
    for suffix in ("-wal", "-shm", "-journal"):
        candidate = database_path + suffix
        try:
            result[suffix] = os.path.getsize(candidate)
        except OSError:
            continue
    return result


def storage_report(connection: sqlite3.Connection, *, sample_rows: int = 8,
                   top_tables: int = 20, top_columns: int = 8) -> dict:
    """Measure one open archive database without changing it.

    ``connection`` must already be a read-only connection.  The caller decides
    whether the database is a verified snapshot; this function never copies,
    checkpoints, vacuums or installs anything.
    """
    _bounded(sample_rows, "sample_rows", _MAX_SAMPLES)
    _bounded(top_tables, "top_tables", _MAX_TOP)
    _bounded(top_columns, "top_columns", _MAX_TOP)
    database_row = connection.execute("PRAGMA database_list").fetchone()
    database_path = database_row[2] if database_row is not None and database_row[2] else None
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    freelist = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
    journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
    tables = []
    for table in _tables(connection):
        columns = _columns(connection, table)
        if not columns:
            continue
        tables.append(_table_statistics(connection, table, columns))
    objects = _page_objects(connection)
    page_bytes = {item["name"]: item["bytes"] for item in objects} if objects else {}
    for table in tables:
        table["page_bytes"] = page_bytes.get(table["name"])
    tables.sort(key=lambda item: (-(item["page_bytes"] or item["logical_bytes"]), item["name"]))
    growth = []
    for table in tables[:top_tables]:
        columns = _columns(connection, table["name"])
        time_column = _time_column(columns)
        if time_column is None:
            continue
        measured = _growth(connection, table["name"], columns, time_column)
        if measured is not None:
            growth.append(measured)
    candidates = sorted(
        ((column["logical_bytes"], table["name"], column["name"], table["rows"])
         for table in tables for column in table["columns"] if column["logical_bytes"]),
        key=lambda item: (-item[0], item[1], item[2]))
    # ``table["rows"]`` is the table's full row count; content statistics count
    # non-null values in the selected column and report that separately.
    large_columns = [
        _content_statistics(connection, table, column, total_rows=rows, sample_rows=sample_rows)
        for _bytes, table, column, rows in candidates[:top_columns]]
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": {
            "path": database_path,
            "file_bytes": os.path.getsize(database_path) if database_path else None,
            "page_size": page_size,
            "page_count": page_count,
            "freelist_pages": freelist,
            "freelist_bytes": page_size * freelist,
            "journal_mode": journal_mode,
            "sqlite_version": sqlite3.sqlite_version,
            "sidecars": _sidecars(database_path),
        },
        "contracts": _contracts(connection),
        "page_objects": objects,
        "page_object_bytes_available": objects is not None,
        "tables": tables,
        "growth": growth,
        "large_columns": large_columns,
        "limitations": [
            "Logical bytes are SUM(length(CAST(column AS BLOB))) over text and blob columns; "
            "numeric columns and row overhead are not attributed.",
            "Characters use SQLite length() over TEXT columns and can differ from UTF-8 bytes.",
            "Page bytes come from dbstat and include B-tree pages, not free pages or file overhead; "
            "an index is reported as its own object, not attributed to its table.",
            "Duplicate bytes count exact repeated column contents beyond the first copy; "
            "semantic near-duplicates are not detected.",
            "Compression is a zlib level 6 sample of at most sample_rows values, not a measured "
            "archive-wide cold-archive ratio.",
            "Growth months use stored epoch seconds in UTC; missing or invalid times are 'unknown'.",
            "A live or incomplete copy can include pending WAL/journal sidecars; measure a verified "
            "consistent snapshot instead of an active archive.",
        ],
    }
