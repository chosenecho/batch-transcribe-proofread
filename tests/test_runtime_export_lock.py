"""Stopped runtime probes follow the export locking protocol without creating state."""
from pathlib import Path

import pytest

from bili_asr import file_ops


def test_existing_only_lock_probe_never_creates_a_missing_lock(tmp_path):
    with pytest.raises(FileNotFoundError), file_ops.exclusive_lock(tmp_path / "output", existing_only=True):
        pytest.fail("a missing lock cannot prove stopped state")
    assert not list(tmp_path.iterdir())


def test_export_lock_refuses_path_replacement_between_open_and_admission(tmp_path, monkeypatch):
    output = tmp_path / "output"
    with file_ops.exclusive_lock(output):
        pass
    lock = tmp_path / ".output.export.lock"
    original_lstat = Path.lstat
    replacement = tmp_path / "replacement"
    replacement.write_bytes(file_ops.EXPORT_LOCK_MARKER)
    def replaced_identity(path, *args, **kwargs):
        if path == lock:
            return original_lstat(replacement)
        return original_lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", replaced_identity)
    with pytest.raises(file_ops.FileOperationError, match="identity changed"), file_ops.exclusive_lock(output, existing_only=True):
        pytest.fail("a replaced lock must not admit a lease")
