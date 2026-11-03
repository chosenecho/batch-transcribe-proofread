"""Smoke checks that run from the staged, installed package tree."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from bili_asr.storage import open_database


def test_installed_package_bootstraps_schema_and_status(tmp_path: Path) -> None:
    connection = open_database(tmp_path)
    connection.close()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "bili_asr",
            "status",
            "--archive-root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "users: 0" in proc.stdout
    assert "videos: 0" in proc.stdout
    assert "parts: 0" in proc.stdout


def test_installed_package_contains_check_asr_env_module() -> None:
    import bili_asr.check_asr_env as checker

    assert Path(checker.__file__).name == "check_asr_env.py"
