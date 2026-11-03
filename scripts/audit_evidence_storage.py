"""Measure UTF-8 evidence bytes and bounded compression samples without rewriting data."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import zlib

FIELDS = {
    "editorial_inputs": ("prepared_json",),
    "editorial_model_calls": ("request_json", "response_json"),
    "editorial_revisions": ("blocks_json",),
}


def audit(database: Path, *, sample_rows: int = 32, max_sample_bytes: int = 2 * 1024**2) -> dict:
    if type(sample_rows) is not int or not 1 <= sample_rows <= 1000:
        raise ValueError("sample_rows must be between 1 and 1000")
    if type(max_sample_bytes) is not int or not 1 <= max_sample_bytes <= 16 * 1024**2:
        raise ValueError("max_sample_bytes must be between 1 and 16777216")
    database = Path(database).resolve(strict=True)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")  # One consistent read snapshot, including committed WAL data.
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        free_pages = connection.execute("PRAGMA freelist_count").fetchone()[0]
        try:
            pages = {name: {"bytes": size, "pages": count} for name, size, count in connection.execute(
                "SELECT name,sum(pgsize),count(*) FROM dbstat GROUP BY name")}
        except sqlite3.OperationalError:
            pages = None
        fields = []
        for table, columns in FIELDS.items():
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not exists:
                continue
            for column in columns:  # Only fixed, project-owned identifiers reach SQL.
                count, characters, byte_size = connection.execute(
                    f"SELECT count({column}),coalesce(sum(length({column})),0),"
                    f"coalesce(sum(length(CAST({column} AS BLOB))),0) FROM {table}").fetchone()
                samples = []
                for raw, in connection.execute(
                    f"SELECT CAST({column} AS BLOB) FROM {table} WHERE {column} IS NOT NULL "
                    f"AND length(CAST({column} AS BLOB))<=? ORDER BY rowid LIMIT ?",
                    (max_sample_bytes, sample_rows)):
                    samples.append({"raw_bytes": len(raw), "zlib_bytes": len(zlib.compress(raw, 6)),
                                    "sha256": hashlib.sha256(raw).hexdigest()})
                frequency = Counter(item["sha256"] for item in samples)
                fields.append({"table": table, "column": column, "non_null_rows": count,
                               "text_characters": characters, "utf8_bytes": byte_size,
                               "sample": {"selection": "first-rowid-within-byte-limit",
                                          "rows": len(samples), "max_row_bytes": max_sample_bytes,
                                          "raw_bytes": sum(item["raw_bytes"] for item in samples),
                                          "zlib_bytes": sum(item["zlib_bytes"] for item in samples),
                                          "duplicate_rows": sum(n - 1 for n in frequency.values()),
                                          "codec": "zlib/6", "extrapolated": False}})
        report = {"format": "evidence-storage-audit/v1",
                  "captured_at": datetime.now(timezone.utc).isoformat(),
                  "sqlite_version": sqlite3.sqlite_version,
                  "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
                  "database": {"page_bytes": page_size * page_count, "page_size": page_size,
                               "page_count": page_count, "freelist_bytes": free_pages * page_size,
                               "main_file_bytes": database.stat().st_size,
                               "wal_file_bytes": database.with_name(database.name + "-wal").stat().st_size
                               if database.with_name(database.name + "-wal").exists() else 0},
                  "table_page_usage": pages, "fields": fields, "external_storage_bytes": None,
                  "notes": ["Page usage includes indexes and overflow; it is not per-column attribution.",
                            "File sizes are filesystem observations; SQL counts share a read transaction.",
                            "External storage was not inspected. No evidence was removed or re-encoded."]}
        connection.rollback()
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--sample-rows", type=int, default=32)
    parser.add_argument("--max-sample-bytes", type=int, default=2 * 1024**2)
    args = parser.parse_args()
    try:
        report = audit(args.database, sample_rows=args.sample_rows, max_sample_bytes=args.max_sample_bytes)
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"storage audit failed: {error}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
