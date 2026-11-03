"""Read-only exact-duplicate inventory command."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import sqlite3

from bili_asr.dedup import build_report, format_text
from bili_asr.diagnostics import write_stderr
from bili_asr.archive_session import ArchiveAccessMode, open_archive_connection


def _readonly_connection(archive_root: str) -> sqlite3.Connection:
    return open_archive_connection(archive_root, mode=ArchiveAccessMode.READ)


def _cmd_dedup(args: argparse.Namespace) -> int:
    if args.dedup_action != "report":
        write_stderr("dedup: unsupported action")
        return 1
    if args.limit < 1:
        write_stderr("dedup report: --limit must be positive")
        return 1
    try:
        with closing(_readonly_connection(args.archive_root)) as connection:
            report = build_report(connection, limit=args.limit)
    except (OSError, sqlite3.Error, ValueError) as exc:
        write_stderr(f"dedup report: {exc}")
        return 1
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_text(report))
    return 0
