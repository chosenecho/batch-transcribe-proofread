"""Materialize the frozen v1 baseline, including separate artifact roots.

All rows, JSON and file bytes come from the checked-in historical archive.
Current application writers and renderers are deliberately never invoked.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any

from tests.fixtures.frozen_migration_archive import frozen_archive


@dataclass(frozen=True)
class MigrationArchive:
    archive_root: Path
    artifact_roots: tuple[Path, ...]
    ids: dict[str, Any]
    # Each row starts with the original SQLite rowid, including TEXT-PK tables.
    table_rows: dict[str, tuple[tuple[Any, ...], ...]]
    frozen_json: dict[str, str]
    files: dict[str, bytes]
    not_covered: tuple[str, ...] = (
        "active leases and interrupted writes", "rejected reviews",
        "all caption languages and acquisition failures", "large archive scale",
    )


def capture_table_rows(connection: sqlite3.Connection) -> dict[str, tuple[tuple[Any, ...], ...]]:
    """Capture every shipped table in insertion order without JSON rewriting."""
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    result = {}
    for row in tables:
        name = row[0]
        quoted = '"' + name.replace('"', '""') + '"'
        result[name] = tuple(tuple(value) for value in connection.execute(
            f"SELECT rowid, * FROM {quoted} ORDER BY rowid"
        ))
    return result


def _frozen_json(connection: sqlite3.Connection) -> dict[str, str]:
    result = {}
    for table in ("editorial_inputs", "editorial_revisions", "editorial_model_calls",
                  "editorial_chunk_results", "publication_editions", "workflow_jobs",
                  "workflow_attempts", "workflow_publications", "transcript_asr_evidence"):
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
                   if row[1].endswith("_json")]
        for row in connection.execute(f'SELECT rowid AS original_rowid, * FROM "{table}" ORDER BY rowid'):
            for column in columns:
                if row[column] is not None:
                    result[f"{table}:{row['original_rowid']}:{column}"] = row[column]
    return result


def build_migration_archive(archive_root: Path, artifact_root: Path | None = None) -> MigrationArchive:
    """Copy fixed historical bytes into a new disposable archive."""
    archive_root = Path(archive_root)
    if archive_root.exists():
        raise ValueError("migration fixture requires a new archive")
    archive_root.parent.mkdir(parents=True, exist_ok=True)
    expected = frozen_archive(archive_root)
    write_root = archive_root if artifact_root is None else Path(artifact_root)
    roots = tuple(dict.fromkeys((write_root, archive_root)))
    files = {name: archive_root.joinpath(*name.split('/')).read_bytes() for name in expected['files']}
    if write_root != archive_root:
        if write_root.exists():
            raise ValueError("migration fixture requires a new artifact root")
        write_root.mkdir(parents=True)
        for name, body in files.items():
            # Keep the file-only raw sidecar in the database root so a
            # separate-root fixture still proves readers combine both roots.
            if name.startswith("subtitles/"):
                continue
            destination = write_root.joinpath(*name.split('/'))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            archive_root.joinpath(*name.split('/')).unlink()
        # Empty managed directories have no historical content to preserve.
        for path in sorted(archive_root.rglob('*'), key=lambda item: len(item.parts), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    connection = sqlite3.connect(archive_root / 'archive.db')
    connection.row_factory = sqlite3.Row
    try:
        return MigrationArchive(archive_root=archive_root, artifact_roots=roots,
            ids=expected['ids'], table_rows=capture_table_rows(connection),
            frozen_json=_frozen_json(connection), files=files)
    finally:
        connection.close()
