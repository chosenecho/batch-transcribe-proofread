from __future__ import annotations
import bili_asr.cli.main as _module_cli_main
import bili_asr.cli.status_cmd as _module_cli_status_cmd
import csv
import json
from pathlib import Path
import pytest
from bili_asr.archive import write_archive
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.coverage_report import CoverageReport
from bili_asr.sidecar_projection import is_plain_cli_archive


NOW = "2026-08-28T12:00:00Z"


def cursor(state: str = "complete") -> dict:
    return {"mid": 23191782, "next_page": 3, "total": 2,
            "state": state, "last_api_error_code": None, "updated_at": NOW}


def scheduler(state: str = "complete", ids: list[str] | None = None) -> dict:
    return {"scope": "all", "limit": 20, "state": state,
            "processed_work_ids": ids or ["BVone:p1", "BVtwo:p1"],
            "last_api_error_code": None, "allow_long_live": False, "updated_at": NOW}


def ledger(ids: list[str] | None = None, *, exit_code: int = 0) -> dict:
    return {"run_id": "run-1", "command": "schedule", "started_at": NOW,
            "finished_at": NOW, "exit_code": exit_code, "mid": 23191782,
            "work_ids": ids or ["BVone:p1", "BVtwo:p1"], "pages_fetched": 1,
            "records_fetched": 2, "records_existing": 0, "last_api_error_code": None,
            "coverage_summary": {"archived": 2}, "cursor_snapshot": cursor()}
