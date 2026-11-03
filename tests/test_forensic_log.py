"""Real-file regressions for verification-surface-truth residual R3."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import forensic_log
import tests.conftest as _conftest


REPO = Path(__file__).parents[1]


def _symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


def _nested_project(base: Path, log_path: Path, *, teardown: bool = False) -> Path:
    project = base / "pytest-run"
    (project / "tests").mkdir(parents=True)
    (project / "scripts").mkdir()
    shutil.copy2(REPO / "scripts" / "forensic_log.py", project / "scripts")
    source = (REPO / "tests" / "conftest.py").read_text(encoding="utf-8")
    (project / "tests" / "conftest.py").write_text(
        source + f"\n_TRACEBACK_PATH = {str(log_path)!r}\n", encoding="utf-8"
    )
    tests = '''import pytest

@pytest.fixture
def broken_setup():
    raise RuntimeError("forensic setup sentinel")

def test_setup_error(broken_setup):
    pass
'''
    if teardown:
        tests += '''
@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("forensic teardown sentinel")

def test_teardown_error(broken_teardown):
    pass
'''
    (project / "tests" / "test_broken.py").write_text(tests, encoding="utf-8")
    return project


def _run_nested(project: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    # Keep this a real pytest process with the actual hook, while avoiding
    # unrelated installed plugins and the outer run's options.
    env.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=project, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "forensic setup sentinel" in result.stdout
    return result


def test_actual_pytest_hook_retains_setup_teardown_and_append_history(tmp_path: Path):
    log = tmp_path / ".tb" / "errors.log"
    project = _nested_project(tmp_path, log, teardown=True)
    for _ in range(2):
        result = _run_nested(project)
        assert "1 passed, 2 errors" in result.stdout
        assert "not persisted" not in result.stderr
    text = log.read_text(encoding="utf-8")
    assert text.count("SETUP ERROR") == 2
    assert text.count("TEARDOWN ERROR") == 2
    assert "forensic setup sentinel" in text
    assert "forensic teardown sentinel" in text
    for fact in ("nodeid:", "fixture info:", "location:", "cwd=", "sys.executable=", "tmpdir=", "bili_asr=", "disk["):
        assert fact in text
    if os.name == "posix":
        assert stat.S_IMODE(log.stat().st_mode) == 0o600


@pytest.mark.parametrize("linked_component", ["log_dir", "ancestor"])
@pytest.mark.parametrize("existing_log", [False, True])
def test_actual_pytest_hook_refuses_directory_links_without_touching_outside(
    tmp_path: Path, linked_component: str, existing_log: bool,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    if linked_component == "log_dir":
        target = outside
        link = tmp_path / ".tb"
        log = link / "errors.log"
    else:
        target = outside
        (outside / "work" / ".tb").mkdir(parents=True)
        link = tmp_path / "linked-parent"
        log = link / "work" / ".tb" / "errors.log"
    escaped = outside / ("errors.log" if linked_component == "log_dir" else "work/.tb/errors.log")
    if existing_log:
        escaped.write_text("outside sentinel\n", encoding="utf-8")
    _symlink(link, target, directory=True)
    project = _nested_project(tmp_path, log)

    result = _run_nested(project)

    assert "1 error" in result.stdout
    assert "setup error not persisted" in result.stderr
    if existing_log:
        assert escaped.read_text(encoding="utf-8") == "outside sentinel\n"
    else:
        assert not escaped.exists()
    assert link.is_symlink()


def test_actual_pytest_hook_refuses_linked_log_file(tmp_path: Path):
    directory = tmp_path / ".tb"
    directory.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("outside sentinel\n", encoding="utf-8")
    log = directory / "errors.log"
    _symlink(log, outside)
    project = _nested_project(tmp_path, log)

    result = _run_nested(project)

    assert "setup error not persisted" in result.stderr
    assert outside.read_text(encoding="utf-8") == "outside sentinel\n"
    assert log.is_symlink()


@pytest.mark.parametrize("linked_component", ["log_dir", "ancestor", "file"])
def test_portable_fallback_refuses_all_visible_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked_component: str,
):
    monkeypatch.setattr(forensic_log, "_USE_DIR_FD", False)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "errors.log"
    victim.write_text("outside sentinel\n", encoding="utf-8")
    if linked_component == "file":
        log_dir = tmp_path / ".tb"
        log_dir.mkdir()
        log = log_dir / "errors.log"
        _symlink(log, victim)
    else:
        link = tmp_path / (".tb" if linked_component == "log_dir" else "linked-parent")
        _symlink(link, outside, directory=True)
        log = link / ("errors.log" if linked_component == "log_dir" else ".tb/errors.log")
    with pytest.raises(OSError):
        with forensic_log.open_forensic_log(log) as handle:
            handle.write("must stay inside\n")
    assert victim.read_text(encoding="utf-8") == "outside sentinel\n"
    assert not (outside / ".tb").exists()


def test_portable_fallback_appends_to_real_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(forensic_log, "_USE_DIR_FD", False)
    log = tmp_path / ".tb" / "errors.log"
    for text in ("first\n", "second\n"):
        with forensic_log.open_forensic_log(log) as handle:
            handle.write(text)
    assert log.read_text(encoding="utf-8") == "first\nsecond\n"


def test_opened_log_directory_stays_confined_when_replaced_with_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    if not forensic_log._USE_DIR_FD:
        pytest.skip("requires directory descriptor support")
    log_dir = tmp_path / ".tb"
    log_dir.mkdir()
    held = tmp_path / "held-directory"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_open = os.open
    swapped = False

    def swap_before_file_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "errors.log" and dir_fd is not None:
            log_dir.rename(held)
            _symlink(log_dir, outside, directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(forensic_log.os, "open", swap_before_file_open)
    with forensic_log.open_forensic_log(log_dir / "errors.log") as handle:
        handle.write("held-directory evidence\n")
    assert swapped
    assert (held / "errors.log").read_text(encoding="utf-8") == "held-directory evidence\n"
    assert not (outside / "errors.log").exists()


def test_fifo_is_refused_without_waiting_for_a_reader(tmp_path: Path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("requires FIFO support")
    directory = tmp_path / ".tb"
    directory.mkdir()
    log = directory / "errors.log"
    os.mkfifo(log)
    with pytest.raises(OSError):
        with forensic_log.open_forensic_log(log) as handle:
            handle.write("must not block\n")
    assert stat.S_ISFIFO(log.lstat().st_mode)


def test_logging_and_stderr_failure_cannot_mask_the_original_test_error(monkeypatch: pytest.MonkeyPatch):
    def unavailable_log(_path):
        raise OSError("log directory unavailable")

    def unavailable_stderr(*_args, **_kwargs):
        raise OSError("stderr unavailable")

    monkeypatch.setattr(_conftest, "open_forensic_log", unavailable_log)
    monkeypatch.setattr(_conftest, "print", unavailable_stderr, raising=False)
    hook = _conftest.pytest_runtest_makereport(
        SimpleNamespace(nodeid="original setup error"), SimpleNamespace(when="setup")
    )
    next(hook)
    outcome = SimpleNamespace(get_result=lambda: SimpleNamespace(failed=True))
    with pytest.raises(StopIteration):
        hook.send(outcome)
