"""Small shared helpers for the current SQLite CLI."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import TYPE_CHECKING

from bili_asr.artifact_root import ARTIFACT_ROOT_ENV_VAR
from bili_asr.artifacts import REQUIRED_ARTIFACT_KEYS as _PRODUCT_PATH_KEYS  # noqa: F401 - compatibility export
from bili_asr.diagnostics import write_stderr

if TYPE_CHECKING:
    from bili_asr.storage import MetadataRepository

DEFAULT_ARCHIVE_ROOT = "archive"
_ARTIFACT_ROOT_HELP = f"Root for transcript and audio artifacts (or env {ARTIFACT_ROOT_ENV_VAR}); default: the archive root. Must already exist"


class _UsageErrorArgumentParser(argparse.ArgumentParser):
    """Map argparse usage errors to the CLI's configuration exit code."""

    def _print_message(self, message: str | None, file=None) -> None:
        if file is None or file is sys.stderr:
            if message:
                write_stderr(message.removesuffix("\n"))
            return
        super()._print_message(message, file)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        super().exit(1 if status == 2 else status, message)


def _metadata_database_path(archive_root: str) -> str:
    return os.path.join(archive_root, "archive.db")


def _open_read_repository(command: str, archive_root: str) -> "MetadataRepository | None":
    from bili_asr.storage import MetadataRepository
    from bili_asr.archive_session import ArchiveAccessMode, open_archive_connection

    path = _metadata_database_path(archive_root)
    if not os.path.isfile(path):
        write_stderr(f"{command}: no archive database at {archive_root}; run fetch-meta to create it")
        return None
    try:
        return MetadataRepository(open_archive_connection(archive_root, mode=ArchiveAccessMode.READ))
    except (OSError, sqlite3.Error) as exc:
        write_stderr(f"{command}: unreadable archive database at {archive_root} ({type(exc).__name__})")
        return None


def _format_run_line(stats: sqlite3.Row, error_code: str | None) -> str:
    finished = stats["finished_at"]
    fields = [
        f"run {stats['run_id']}", f"mid={stats['mid']}",
        f"started={stats['started_at']}", f"finished={finished if finished is not None else '-'}",
        f"outcome={stats['outcome']}", f"pages={stats['page_count']}",
        f"videos={stats['video_count']}",
    ]
    if error_code is not None:
        fields.append(f"error={error_code}")
    return " ".join(fields)


def _run_error_codes(repository: "MetadataRepository", run_ids: list[str]) -> dict[str, str]:
    codes: dict[str, str] = {}
    for run_id in run_ids:
        row = repository.connection.execute(
            "SELECT error_code FROM ingestion_pages WHERE run_id = ? "
            "AND error_code IS NOT NULL ORDER BY page_number LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is not None:
            codes[run_id] = str(row["error_code"])
    return codes
