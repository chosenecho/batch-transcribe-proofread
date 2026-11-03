"""Pin for conftest's ``tmp_root`` PID-reuse hardening (plan 007).

Before the UUID suffix, the dir name was ``manifest-test-<pid>-<counter>``; a
leftover dir from a killed run could collide on PID reuse and raise
``FileExistsError`` in fixture setup (an intermittent full-suite red). The fix
adds a UUID component, so a stale dir is tolerated instead of colliding.
"""

from __future__ import annotations

import os
import re

import tests.conftest as _conftest


def test_tmp_root_reuse_leftover_dir(tmp_root, monkeypatch):
    """A stale leftover dir must not make ``tmp_root`` raise FileExistsError."""
    # Model the actual old name after a killed process and PID/counter reuse.
    # Keeping both new fixtures alive also proves uniqueness within one process.
    monkeypatch.setattr(_conftest, "_TEST_TMP_BASE", tmp_root)
    monkeypatch.setattr(_conftest.os, "getpid", lambda: 23456)
    monkeypatch.setattr(_conftest, "_counter", iter([0, 0]))
    stale = os.path.join(tmp_root, "manifest-test-23456-0")
    os.makedirs(stale)
    sentinel = os.path.join(stale, "untouched.txt")
    with open(sentinel, "w", encoding="utf-8") as stream:
        stream.write("leftover from a killed test")

    fixture = _conftest.tmp_root.__wrapped__
    generators = [fixture(), fixture()]
    yielded = []
    try:
        for gen in generators:
            path = next(gen)
            yielded.append(path)
            assert path != stale
            assert os.path.isdir(path)
            assert os.path.commonpath([path, tmp_root]) == tmp_root
            assert re.fullmatch(r"manifest-test-23456-0-[0-9a-f]{32}", os.path.basename(path))
        assert len(set(yielded)) == 2
        with open(sentinel, encoding="utf-8") as stream:
            assert stream.read() == "leftover from a killed test"
    finally:
        for gen in generators:
            gen.close()
    assert all(not os.path.exists(path) for path in yielded)
    assert os.path.isfile(sentinel)  # teardown never takes ownership of the stale dir
