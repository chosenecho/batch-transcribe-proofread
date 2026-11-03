"""Durable single-file replacement handles incomplete writes and ownership failures."""
import os
import subprocess
import sys
import time

import pytest

from bili_asr import file_ops


def test_blocking_process_lock_waits_then_checks_marker(tmp_path):
    output = tmp_path / "output"
    ready = tmp_path / "ready"
    child = None
    try:
        # Start the contender only after the parent owns the actual OS lock.
        with file_ops.exclusive_lock(output):
            child = subprocess.Popen([sys.executable, "-c", """
import sys
from pathlib import Path
from bili_asr.file_ops import exclusive_lock
output, ready = map(Path, sys.argv[1:])
ready.write_text('ready')
with exclusive_lock(output, blocking=True):
    (output.parent / 'acquired').write_text('acquired')
""", str(output), str(ready)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            deadline = time.monotonic() + 5
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists(), "contender did not start"
            time.sleep(0.2)
            assert child.poll() is None, "blocking contender exited before release"
            assert not (tmp_path / 'acquired').exists()
        _, errors = child.communicate(timeout=15)
        assert child.returncode == 0, errors.decode(errors='replace')
        assert (tmp_path / 'acquired').read_text() == 'acquired'
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()


def test_first_use_concurrency_never_exposes_an_uninitialized_lock(tmp_path):
    output = tmp_path / 'output'
    ready = tmp_path / 'initializing'
    resume = tmp_path / 'resume'
    child = subprocess.Popen([sys.executable, '-c', """
import os, sys, time
from pathlib import Path
from bili_asr.file_ops import exclusive_lock
output, ready, resume = map(Path, sys.argv[1:])
original_write = os.write
def pause_first_write(descriptor, data):
    os.write = original_write
    ready.write_text('initializing')
    deadline = time.monotonic() + 10
    while not resume.exists():
        if time.monotonic() > deadline:
            raise TimeoutError('initializer was not resumed')
        time.sleep(0.01)
    return original_write(descriptor, data)
os.write = pause_first_write
with exclusive_lock(output, blocking=True):
    (output.parent / 'acquired').write_text('acquired')
""", str(output), str(ready), str(resume)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), 'initializer did not start'
        # The other creator is paused before writing its marker. A new owner
        # must still acquire a fully initialized public lock and finish safely.
        with file_ops.exclusive_lock(output):
            resume.write_text('resume')
            time.sleep(0.2)
            assert child.poll() is None
            assert not (tmp_path / 'acquired').exists()
        _, errors = child.communicate(timeout=15)
        assert child.returncode == 0, errors.decode(errors='replace')
        assert (tmp_path / 'acquired').exists()
        assert not list(tmp_path.glob('*.init-*'))
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_damaged_marker_never_admits_a_writer(tmp_path):
    (tmp_path / '.output.export.lock').write_bytes(b'not our lock')
    with pytest.raises(file_ops.FileOperationError, match='unmanaged or damaged'):
        with file_ops.exclusive_lock(tmp_path / 'output', blocking=True):
            pytest.fail('damaged lock admitted a writer')


class PartialWriter:
    def __init__(self, stream, *, zero_after_first=False):
        self.stream = stream
        self.zero_after_first = zero_after_first
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def write(self, content):
        self.calls += 1
        if self.zero_after_first and self.calls > 1:
            return 0
        return self.stream.write(content[:3])


def test_atomic_replace_completes_short_writes_before_publishing(tmp_path, monkeypatch):
    destination = tmp_path / "value.json"
    content = '{"original":"中文 synthetic data"}\n'.encode("utf-8")
    original = os.fdopen
    monkeypatch.setattr(file_ops.os, "fdopen", lambda *args: PartialWriter(original(*args)))
    file_ops.atomic_replace_file(destination, content)
    assert destination.read_bytes() == content
    assert list(tmp_path.iterdir()) == [destination]


def test_zero_write_preserves_destination_and_removes_private_partial_file(tmp_path, monkeypatch):
    destination = tmp_path / "value.json"
    destination.write_bytes(b"original bytes")
    original = os.fdopen
    monkeypatch.setattr(file_ops.os, "fdopen", lambda *args: PartialWriter(original(*args), zero_after_first=True))
    with pytest.raises(OSError, match="cannot complete file write"):
        file_ops.atomic_replace_file(destination, b"replacement bytes")
    assert destination.read_bytes() == b"original bytes"
    assert list(tmp_path.iterdir()) == [destination]


def test_stream_initialization_failure_closes_descriptor_and_preserves_destination(tmp_path, monkeypatch):
    destination = tmp_path / "value.json"
    destination.write_bytes(b"original bytes")
    opened = []

    def fail_open(descriptor, mode):
        opened.append(descriptor)
        raise OSError("synthetic stream initialization failure")

    monkeypatch.setattr(file_ops.os, "fdopen", fail_open)
    with pytest.raises(OSError, match="stream initialization failure"):
        file_ops.atomic_replace_file(destination, b"replacement bytes")
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert destination.read_bytes() == b"original bytes"
    assert list(tmp_path.iterdir()) == [destination]
