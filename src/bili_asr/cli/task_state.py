"""Read task-state backups without an archive session, worker or model."""
from __future__ import annotations

import argparse
import json
import sqlite3
import zipfile
from pathlib import Path

from bili_asr.diagnostics import write_stderr


def add_task_state_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("task-state", help="Verify or read inactive task-state evidence offline")
    actions = parser.add_subparsers(dest="task_state_action", required=True)
    for name in ("verify", "read", "records", "evidence"):
        action = actions.add_parser(name)
        action.add_argument("--snapshot", required=True, type=Path)
        if name in {"read", "records"}:
            for flag in ("job", "video", "stage", "status", "model"):
                action.add_argument("--" + flag, action="append", default=[])
            action.add_argument("--part", type=int, action="append", default=[])
            action.add_argument("--updated-from", type=int)
            action.add_argument("--updated-to", type=int)
            action.add_argument("--offset", type=int, default=0)
            action.add_argument("--limit", type=int, default=20)
            action.add_argument("--history-limit", type=int, default=1000)
        if name == "records":
            action.add_argument("--table", required=True)
        if name == "evidence":
            action.add_argument("--path", required=True)
        if name != "verify":
            action.add_argument("--work-directory", type=Path)


def _cmd_task_state(args) -> int:
    from bili_asr.services.maintenance_state_reader import (
        StateQuery,
        read_state_evidence,
        read_task_records,
        read_task_state,
    )
    from bili_asr.services.maintenance_task_state import verify_task_state

    try:
        if args.task_state_action == "verify":
            result = verify_task_state(args.snapshot)
        elif args.task_state_action == "evidence":
            result = read_state_evidence(args.snapshot, args.path, work_directory=args.work_directory)
        else:
            query = StateQuery(job_ids=tuple(args.job), videos=tuple(args.video), part_ids=tuple(args.part),
                               stages=tuple(args.stage), statuses=tuple(args.status), models=tuple(args.model),
                               updated_from=args.updated_from, updated_to=args.updated_to,
                               offset=args.offset, limit=args.limit, history_limit=args.history_limit)
            if args.task_state_action == "records":
                result = read_task_records(args.snapshot, args.table, query=query, work_directory=args.work_directory)
            else:
                result = read_task_state(args.snapshot, query=query, work_directory=args.work_directory)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, zipfile.BadZipFile) as exc:
        write_stderr("task-state: " + str(exc))
        return 1
