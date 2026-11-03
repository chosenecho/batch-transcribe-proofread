"""Real bundle verification keeps memory bounded and observes late corruption."""

import hashlib
import json
import os
import tempfile
import tracemalloc
from pathlib import Path

import pytest

from bili_asr import archive


def _large_bundle(root):
    row = {"bvid": "BVstream", "work_id": "BVstream:p0", "page_index": 0, "cid": 1}
    paths = archive.write_archive(
        root, row, [{"start": 0, "end": 1, "text": "hello"}], source="asr"
    )
    target = Path(root) / paths["txt_path"]
    target.write_bytes(b"a" * (4 * 1024 * 1024 + 17))
    marker = archive.bundle_marker_path(Path(root) / paths["srt_path"])
    document = json.loads(marker.read_text(encoding="ascii"))
    document["artifacts"]["txt_path"]["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    marker.write_text(json.dumps(document), encoding="ascii")
    return paths, target


def test_bundle_verification_memory_does_not_scale_with_artifact_size(tmp_root):
    paths, _target = _large_bundle(tmp_root)
    tracemalloc.start()
    try:
        assert archive.archive_bundle_complete(tmp_root, paths)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024


def test_bundle_verification_checks_bytes_past_first_chunk(tmp_root):
    paths, target = _large_bundle(tmp_root)
    assert archive.archive_bundle_complete(tmp_root, paths)
    with target.open("r+b") as handle:
        handle.seek(-1, os.SEEK_END)
        handle.write(b"b")
    assert not archive.archive_bundle_complete(tmp_root, paths)


def test_bundle_verification_rejects_edit_to_a_chunk_already_read(tmp_root, monkeypatch):
    paths, target = _large_bundle(tmp_root)
    target_stat = target.stat()
    real_read = os.read
    mutated = False

    def read_then_edit(fd, size):
        nonlocal mutated
        chunk = real_read(fd, size)
        info = os.fstat(fd)
        if not mutated and chunk and (info.st_dev, info.st_ino) == (target_stat.st_dev, target_stat.st_ino):
            mutated = True
            with target.open("r+b") as handle:
                handle.write(b"b")
            # Make the change observable even on coarse timestamp filesystems.
            os.utime(target, ns=(target_stat.st_atime_ns, target_stat.st_mtime_ns + 1_000_000_000))
        return chunk

    monkeypatch.setattr(archive.os, "read", read_then_edit)
    assert not archive.archive_bundle_complete(tmp_root, paths)
    assert mutated


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX named pipes")
def test_bundle_verification_refuses_fifo_without_waiting_for_a_writer():
    # WSL's mounted Windows workspace cannot create FIFOs; use its native /tmp.
    with tempfile.TemporaryDirectory(prefix="bili-asr-fifo-", dir="/tmp") as root:
        paths, target = _large_bundle(root)
        target.unlink()
        os.mkfifo(target)
        assert not archive.archive_bundle_complete(root, paths)
