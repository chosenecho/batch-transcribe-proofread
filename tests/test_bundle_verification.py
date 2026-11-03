"""Real isolated hashing and disposable-worker deadline boundaries."""
import os
import sys
import time
from pathlib import Path

import pytest

from bili_asr import archive
from bili_asr.services import bundle_verification as service


def _bundle(root):
    return archive.write_archive(root, {
        "bvid": "BV1DEADLINE", "work_id": "BV1DEADLINE:p0", "page_index": 0,
        "cid": 1, "title": "test", "duration_s": 2, "pubdate_str": "2026-10-06",
    }, [{"start": 0, "end": 1, "text": "strict bytes"}], source="subtitle-ai")


def test_isolated_hash_detects_same_size_same_mtime_edit(tmp_root):
    paths = _bundle(tmp_root)
    assert service.verify_bundle(tmp_root, paths)
    path = Path(tmp_root) / paths["txt_path"]
    # Normalize to whole seconds: drvfs rounds utime timestamps.
    os.utime(path, (1_700_000_000, 1_700_000_000))
    before = path.stat()
    payload = path.read_bytes()
    path.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert not service.verify_bundle(tmp_root, paths)


@pytest.mark.parametrize("shadow_environment", [False, True])
def test_real_child_imports_active_package_from_unrelated_cwd(tmp_root, monkeypatch, shadow_environment):
    directory = Path(tmp_root) / "unrelated"
    directory.mkdir()
    monkeypatch.chdir(directory)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    if shadow_environment:
        shadow = directory / "shadow" / "bili_asr" / "services"
        shadow.mkdir(parents=True)
        (shadow.parent / "__init__.py").write_text("")
        (shadow / "__init__.py").write_text("")
        (shadow / "bundle_verification.py").write_text("# incompatible installed version\n")
        monkeypatch.setenv("PYTHONPATH", str(shadow.parent.parent))
    before = dict(os.environ)
    code = (
        "import json; from pathlib import Path; "
        "from bili_asr.services import bundle_verification as module; "
        f"print(json.dumps({{'complete':Path(module.__file__).resolve()==Path({service.__file__!r}).resolve()}}))"
    )
    real_command = service._command
    monkeypatch.setattr(service, "_command", lambda: [sys.executable, "-c", code])
    assert service.verify_bundle(tmp_root, {})
    monkeypatch.setattr(service, "_command", real_command)
    assert service.verify_bundle(tmp_root, _bundle(tmp_root))
    assert dict(os.environ) == before


def test_stalled_worker_deadline_returns_and_next_verification_succeeds(tmp_root, monkeypatch):
    paths = _bundle(tmp_root)
    real_command = service._command
    monkeypatch.setattr(service, "_command", lambda: [sys.executable, "-c", "import time; time.sleep(60)"])
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        service.verify_bundle(tmp_root, paths, timeout_seconds=0.1)
    assert time.monotonic() - started < 2
    monkeypatch.setattr(service, "_command", real_command)
    assert service.verify_bundle(tmp_root, paths)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor inspection")
def test_verification_worker_does_not_inherit_writer_descriptor(tmp_root, monkeypatch):
    lock = Path(tmp_root) / "writer.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    os.set_inheritable(descriptor, True)
    code = (
        "import os,json; "
        f"target={str(lock)!r}; "
        "names=[os.readlink('/proc/self/fd/'+fd) for fd in os.listdir('/proc/self/fd') "
        "if os.path.exists('/proc/self/fd/'+fd)]; "
        "print(json.dumps({'complete':target not in names}))"
    )
    monkeypatch.setattr(service, "_command", lambda: [sys.executable, "-c", code])
    try:
        assert service.verify_bundle(tmp_root, {})
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_verification_deadline_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        service.verify_bundle("unused", {}, timeout_seconds=value)


def test_worker_permission_error_keeps_bounded_error_class(tmp_root, monkeypatch):
    monkeypatch.setattr(service, "_command", lambda: [
        sys.executable, "-c", "print('{\"error\":\"unreadable\",\"errno\":13}')",
    ])
    with pytest.raises(PermissionError):
        service.verify_bundle(tmp_root, {})


def test_unreapable_worker_cleanup_does_not_wait_forever(monkeypatch):
    import subprocess
    class Stuck:
        returncode = None
        def communicate(self, *args, **kwargs):
            assert kwargs["timeout"] <= 0.2
            raise subprocess.TimeoutExpired("stuck", kwargs["timeout"])
        def kill(self):
            pass
        def poll(self):
            return None
    worker = Stuck()
    monkeypatch.setattr(service, "_UNREAPED", [])
    monkeypatch.setattr(service.subprocess, "Popen", lambda *args, **kwargs: worker)
    with pytest.raises(TimeoutError):
        service.verify_bundle("unused", {}, timeout_seconds=0.1)
    assert service._UNREAPED == [worker]
    service._UNREAPED[:] = [worker] * 4
    with pytest.raises(OSError, match="capacity exhausted"):
        service.verify_bundle("unused", {})


def test_shared_budget_counts_actual_reads_and_refuses_next_bundle(tmp_root):
    paths = _bundle(tmp_root)
    marker = archive.bundle_marker_path(Path(tmp_root) / paths["srt_path"])
    total = marker.stat().st_size + sum((Path(tmp_root) / path).stat().st_size for path in paths.values())
    budget = service.VerificationReadBudget(total + 1)
    assert service.verify_bundle(tmp_root, paths, budget=budget)
    assert budget.read_bytes == total
    assert budget.remaining == 1
    with pytest.raises(service.VerificationBudgetExceeded):
        service.verify_bundle(tmp_root, paths, budget=budget)
    assert budget.read_bytes == total + 1
    assert budget.remaining == 0


def test_single_bundle_never_reads_more_than_allowance(tmp_root):
    paths = _bundle(tmp_root)
    budget = service.VerificationReadBudget(17)
    with pytest.raises(service.VerificationBudgetExceeded):
        service.verify_bundle(tmp_root, paths, budget=budget)
    assert budget.read_bytes == 17
    assert budget.remaining == 0


def test_timeout_keeps_full_reserved_allowance(tmp_root, monkeypatch):
    monkeypatch.setattr(service, "_command", lambda: [sys.executable, "-c", "import time; time.sleep(60)"])
    budget = service.VerificationReadBudget(100)
    with pytest.raises(TimeoutError):
        service.verify_bundle(tmp_root, {}, budget=budget, timeout_seconds=0.1)
    assert budget.remaining == 0
    with pytest.raises(service.VerificationBudgetExceeded):
        service.verify_bundle(tmp_root, {}, budget=budget)


def test_invalid_response_cannot_refund_reserved_allowance(tmp_root, monkeypatch):
    monkeypatch.setattr(service, "_command", lambda: [
        sys.executable, "-c", "print('{\"unexpected\":true,\"read_bytes\":0}')",
    ])
    budget = service.VerificationReadBudget(100)
    with pytest.raises(OSError, match="invalid bundle verification response"):
        service.verify_bundle(tmp_root, {}, budget=budget)
    assert budget.remaining == 0
