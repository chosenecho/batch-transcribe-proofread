"""Snapshot coordination preserves worker concurrency and excludes maintenance."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from bili_asr.archive_maintenance import ArchiveAccessError, ArchiveBusyError, archive_access
from bili_asr.cli import main


def _attempt_in_thread(root: Path, exclusive: bool) -> list[Exception]:
    errors = []

    def acquire():
        try:
            with archive_access(root, exclusive=exclusive):
                pass
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=acquire)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    return errors


def test_workers_share_access_but_exclude_snapshots(tmp_path):
    archive = tmp_path / "archive"
    with archive_access(archive):
        assert _attempt_in_thread(archive, False) == []
        assert isinstance(_attempt_in_thread(archive, True)[0], ArchiveBusyError)
    assert _attempt_in_thread(archive, True) == []
    assert not archive.exists()


def test_snapshot_excludes_writers_and_other_snapshots(tmp_path):
    with archive_access(tmp_path, exclusive=True):
        assert isinstance(_attempt_in_thread(tmp_path, False)[0], ArchiveBusyError)
        assert isinstance(_attempt_in_thread(tmp_path, True)[0], ArchiveBusyError)


def test_nested_access_retains_outer_lock_and_refuses_upgrade(tmp_path):
    with archive_access(tmp_path):
        with archive_access(tmp_path):
            pass
        with pytest.raises(ArchiveBusyError):
            with archive_access(tmp_path, exclusive=True):
                pass
        assert isinstance(_attempt_in_thread(tmp_path, True)[0], ArchiveBusyError)
    with archive_access(tmp_path, exclusive=True):
        with archive_access(tmp_path):
            pass
        assert isinstance(_attempt_in_thread(tmp_path, False)[0], ArchiveBusyError)


def test_cross_process_lock_refuses_cli_writer_before_database_creation(tmp_path):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.fspath(Path(__file__).resolve().parents[1] / "src")
    archive = tmp_path / "archive"
    with archive_access(archive, exclusive=True):
        result = subprocess.run(
            [sys.executable, "-m", "bili_asr", "workflow", "status", "--archive-root", os.fspath(archive)],
            capture_output=True, text=True, env=environment, timeout=30,
        )
    assert result.returncode == 1
    assert "archive_busy" in result.stderr
    assert not archive.exists()


def test_cli_shared_access_does_not_block_another_worker(tmp_path, monkeypatch):
    from bili_asr.cli import workflow

    entered = []
    monkeypatch.setattr(workflow, "_cmd_workflow", lambda args: entered.append(args) or 0)
    with archive_access(tmp_path):
        assert main(["workflow", "status", "--archive-root", str(tmp_path)]) == 0
    assert len(entered) == 1


def test_missing_and_symlink_roots_refused_without_changing_target(tmp_path):
    with pytest.raises(ArchiveAccessError, match="does not exist"):
        with archive_access(tmp_path / "missing", create_root=False):
            pass
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("host cannot create symlinks")
    with pytest.raises(ArchiveAccessError, match="symlink"):
        with archive_access(alias):
            pass
