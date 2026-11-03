"""Write events supplement coarse Linux stat clocks without changing file bytes."""
import ctypes
import errno
import os
import sys
from types import SimpleNamespace

import pytest

from bili_asr import file_generation as generation


def test_non_linux_watch_uses_native_generation_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "sys", SimpleNamespace(platform="darwin"))
    with generation.change_watch(tmp_path / "unused") as changed:
        assert not changed()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux inotify interface")
def test_linux_watch_catches_restored_bytes_and_mtime_and_keeps_failure_sticky(tmp_path):
    path = tmp_path / "private-audio"
    content = b"unchanged final bytes"
    path.write_bytes(content)
    before = path.stat()
    with generation.change_watch(path) as changed:
        assert not changed()
        path.write_bytes(b"x" * len(content))
        path.write_bytes(content)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert path.read_bytes() == content and path.stat().st_mtime_ns == before.st_mtime_ns
        assert changed() and changed()
    assert path.read_bytes() == content


@pytest.mark.skipif(sys.platform != "linux", reason="Linux inotify interface")
@pytest.mark.parametrize("stage", ["initialize", "watch"])
def test_linux_watch_setup_failure_is_explicit_and_closes_descriptor(tmp_path, monkeypatch, stage):
    descriptor = os.open(os.devnull, os.O_RDONLY) if stage == "watch" else -1
    def initialize(flags):
        ctypes.set_errno(errno.EMFILE)
        return descriptor
    def add(fd, name, mask):
        assert fd == descriptor
        ctypes.set_errno(errno.EACCES)
        return -1
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(
        inotify_init1=initialize, inotify_add_watch=add))
    with pytest.raises(OSError):
        with generation.change_watch(tmp_path / "private-audio"):
            pytest.fail("unobserved input must not reach inference")
    if descriptor >= 0:
        with pytest.raises(OSError):
            os.fstat(descriptor)
