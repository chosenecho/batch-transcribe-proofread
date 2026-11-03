"""SQLite status, coverage, and ingestion-run handlers."""

from __future__ import annotations

import argparse
import sys

from bili_asr.diagnostics import write_stderr
from bili_asr.cli._shared import (
    _format_run_line,
    _open_read_repository,
    _run_error_codes,
)


def _cmd_status(args: argparse.Namespace) -> int:
    repository = _open_read_repository("status", args.archive_root)
    if repository is None:
        return 1
    try:
        connection = repository.connection
        row = connection.execute(
            "SELECT (SELECT COUNT(*) FROM bilibili_users) AS users, "
            "(SELECT COUNT(*) FROM videos) AS videos, "
            "(SELECT COUNT(*) FROM video_parts) AS parts"
        ).fetchone()
        print(f"users: {row['users']}")
        print(f"videos: {row['videos']}")
        print(f"parts: {row['parts']}")
        processing = connection.execute(
            "SELECT processing_status, COUNT(*) AS count FROM video_parts "
            "GROUP BY processing_status ORDER BY processing_status"
        ).fetchall()
        if processing:
            summary = ", ".join(
                f"{item['processing_status']}={item['count']}" for item in processing
            )
            print(f"processing: {summary}")
        for item in connection.execute(
            "SELECT kind, status, COUNT(*) AS count FROM workflow_jobs "
            "GROUP BY kind, status ORDER BY kind, status"
        ):
            print(f"workflow: {item['kind']} {item['status']}={item['count']}")
        pending = repository.list_pending_parts()
        print(f"pending: {len(pending)}")
        for part in pending[:20]:
            print(f"  {part['work_id']}")
        if len(pending) > 20:
            print(f"  + {len(pending) - 20} more pending part(s)")
        for user in connection.execute("SELECT mid FROM bilibili_users ORDER BY mid"):
            cursor = repository.read_cursor(int(user["mid"]))
            if cursor is not None:
                print(f"cursor: mid={cursor.mid} next_page={cursor.next_page} state={cursor.state}")
        return 0
    finally:
        repository.connection.close()


def _cmd_coverage(args: argparse.Namespace) -> int:
    from bili_asr.coverage_report import CoverageReport

    try:
        report = CoverageReport.build(
            args.archive_root, scope=args.scope, artifact_roots=args.artifact_roots
        )
        sys.stdout.write(report.to_json() if args.format == "json" else report.to_csv())
        if args.format == "json":
            sys.stdout.write("\n")
        diagnostics = report.data["diagnostics"]
        return 1 if args.strict and diagnostics else 0
    except Exception:
        write_stderr("coverage: workflow projection unavailable")
        return 1


def _cmd_runs(args: argparse.Namespace) -> int:
    repository = _open_read_repository("runs", args.archive_root)
    if repository is None:
        return 1
    try:
        if args.limit is not None and args.limit < 1:
            write_stderr("runs: --limit must be a positive integer")
            return 1
        stats_rows = repository.run_stats()
        if not stats_rows:
            print("runs: empty")
            return 0
        ordered = sorted(stats_rows, key=lambda row: (row["started_at"], row["run_id"]), reverse=True)
        selected = ordered if args.limit is None else ordered[: args.limit]
        codes = _run_error_codes(repository, [str(row["run_id"]) for row in selected])
        for row in selected:
            print(_format_run_line(row, codes.get(str(row["run_id"]))))
        return 0
    finally:
        repository.connection.close()
