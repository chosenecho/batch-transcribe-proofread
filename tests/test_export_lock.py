"""Real descriptor cleanup and retry behavior when export lock creation fails."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bili_asr import export_snapshot


@pytest.mark.parametrize("failure", ["write", "fsync", "zero-write"])
def test_lock_initialization_failure_closes_descriptor_and_allows_retry(tmp_path, monkeypatch, failure):
    output = tmp_path / "public"
    lock = tmp_path / ".public.export.lock"
    real_open = os.open
    opened = []

    def track_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path).name.startswith(f"{lock.name}.init-"):
            opened.append(descriptor)
        return descriptor

    def fail(*_args):
        raise OSError("simulated lock initialization failure")

    with monkeypatch.context() as patch:
        patch.setattr(export_snapshot.os, "open", track_open)
        if failure == "zero-write":
            patch.setattr(export_snapshot.os, "write", lambda *_args: 0)
        else:
            patch.setattr(export_snapshot.os, failure, fail)
        with pytest.raises(OSError, match="export lock|lock initialization"):
            with export_snapshot._exclusive_lock(output):
                pytest.fail("failed lock initialization must not admit a writer")

    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert not lock.exists()
    assert not list(tmp_path.glob("*.init-*"))
    with export_snapshot._exclusive_lock(output):
        pass
    assert lock.read_bytes() == b"bili-asr-export-lock-v1\n"


def test_short_lock_writes_complete_the_marker_and_allow_reuse(tmp_path, monkeypatch):
    output = tmp_path / "public"
    real_write = os.write

    with monkeypatch.context() as patch:
        patch.setattr(export_snapshot.os, "write", lambda descriptor, data: real_write(descriptor, data[:3]))
        with export_snapshot._exclusive_lock(output):
            pass
    with export_snapshot._exclusive_lock(output):
        pass
    assert (tmp_path / ".public.export.lock").read_bytes() == b"bili-asr-export-lock-v1\n"
