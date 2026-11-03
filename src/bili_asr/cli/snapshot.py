"""Portable archive snapshot commands."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from bili_asr.artifact_root import roots_for
from bili_asr.diagnostics import write_stderr


def add_snapshot_parser(subparsers: argparse._SubParsersAction, *, archive_root: str) -> None:
    parser = subparsers.add_parser("snapshot", help="Save, check, and restore portable archive snapshots")
    actions = parser.add_subparsers(dest="snapshot_action", required=True)
    save = actions.add_parser("save", help="Save a complete archive ZIP while writers are stopped")
    save.add_argument("--archive-root", default=archive_root)
    save.add_argument("--artifact-root", default=None, help="Existing artifact root (or BILI_ARTIFACT_ROOT)")
    save.add_argument("--out", required=True, help="New snapshot ZIP outside the archive roots")
    save.add_argument("--compression", choices=("stored", "deflate"), default="stored",
                      help="Optional lossless ZIP deflate to reduce archive size; default stored")
    save.add_argument("--storage-target", action="append", help="Explicit TARGET_ID=DIRECTORY binding for offloaded objects")
    save.add_argument("--baseline", type=Path, help="Save a differential against a verified full or incremental snapshot in the same repository")
    check = actions.add_parser("check", help="Verify snapshot hashes, database contract, and file references offline")
    check.set_defaults(database_policy=None)
    check.add_argument("--file", required=True, help="Snapshot ZIP to verify")
    inspect = actions.add_parser("inspect", help="Inspect verified data and derived recovery readiness")
    inspect.set_defaults(database_policy=None)
    inspect.add_argument("--file", required=True)
    inspect.add_argument("--runtime-bindings", default=None)
    plan = actions.add_parser("plan", help="Validate a restore target without creating or changing it")
    plan.set_defaults(database_policy=None)
    plan.add_argument("--file", required=True)
    plan.add_argument("--archive-root", required=True)
    plan.add_argument("--runtime-bindings", default=None)
    doctor = actions.add_parser("doctor", help="Read-only archive, artifact and runtime checks")
    doctor.set_defaults(database_policy=None)
    doctor.add_argument("--archive-root", default=archive_root)
    doctor.add_argument("--artifact-root", default=None)
    doctor.add_argument("--runtime-bindings", default=None)
    restore = actions.add_parser("restore", help="Restore a verified snapshot into a new or empty archive root")
    restore.add_argument("--file", required=True, help="Snapshot ZIP to restore")
    restore.add_argument("--archive-root", required=True, help="New or empty target directory")
    restore.add_argument("--report", default=None, help="New NDJSON recovery audit outside the archive roots")


def _cmd_snapshot(args: argparse.Namespace) -> int:
    from bili_asr.services.archive_recovery import (
        doctor_archive,
        inspect_snapshot,
        plan_restore,
    )
    from bili_asr.services.archive_snapshot import (
        check_snapshot,
        restore_snapshot,
        save_snapshot,
    )

    try:
        if args.snapshot_action == "save":
            from bili_asr.services.artifact_access import parse_target_bindings
            roots = roots_for(args.archive_root, flag_value=args.artifact_root, require_writable=False)
            compression_options = {"compression": args.compression} if args.compression != "stored" else {}
            if args.baseline is not None:
                from bili_asr.services.maintenance_chain import save_delta
                if args.storage_target:
                    raise ValueError("delta backup requires materialized local objects; use full snapshot storage targets first")
                report = save_delta(Path(args.archive_root), Path(args.out), args.baseline,
                                    artifact_root=roots.artifact_root if roots.configured else None,
                                    compression=args.compression)
            else:
                report = save_snapshot(
                    Path(args.archive_root), Path(args.out),
                    artifact_root=roots.artifact_root if roots.configured else None,
                    storage_targets=parse_target_bindings(args.storage_target),
                    **compression_options,
                )
        elif args.snapshot_action == "check":
            report = check_snapshot(Path(args.file))
        elif args.snapshot_action == "inspect":
            report = inspect_snapshot(Path(args.file), runtime_bindings=args.runtime_bindings)
        elif args.snapshot_action == "plan":
            report = plan_restore(Path(args.file), Path(args.archive_root), runtime_bindings=args.runtime_bindings)
        elif args.snapshot_action == "doctor":
            roots = roots_for(args.archive_root, flag_value=args.artifact_root, require_writable=False)
            report = doctor_archive(Path(args.archive_root), artifact_roots=roots, runtime_bindings=args.runtime_bindings)
        else:
            report = restore_snapshot(Path(args.file), Path(args.archive_root), report_path=args.report)
    except (ValueError, OSError, sqlite3.Error) as exc:
        write_stderr(f"snapshot {args.snapshot_action}: {exc}")
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if report.get("data_complete") is False or report.get("target_ready") is False:
        return 1
    return 0
