"""Single-host owned worker slots; no PID scans, inferred roles or second job queue."""

from __future__ import annotations

import math
import multiprocessing
import os
import signal
import stat
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_root import roots_for
from bili_asr.config import SESSDATA_ENV_VAR, resolve_sessdata
from bili_asr.diagnostics import write_stderr
from bili_asr.services.workflow_application import WorkflowApplication

# Reserve time for GPU session reaping and terminal persistence. Severe writer
# contention still falls back to hard termination and the existing lease fence.
_DRAIN_CLEANUP_SECONDS = 5.0


@contextmanager
def _slot_lock(root: Path):
    """A second supervisor for this archive must fail before creating any slot."""
    lock_path = root.parent / f".{root.name}.workflow-supervisor.lock"
    if lock_path.is_symlink():
        raise ValueError("supervisor lock must not be a symlink")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    handle = os.fdopen(descriptor, "r+b")
    try:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode) or os.fstat(handle.fileno()).st_nlink != 1:
            raise ValueError("supervisor lock must be a private regular file")
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("a workflow supervisor already owns this archive") from None
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("a workflow supervisor already owns this archive") from None
        yield
    finally:
        handle.close()


def _controller_parent_guard(stop: threading.Event, grace: float):
    parent = multiprocessing.parent_process()
    while True:
        time.sleep(0.1)
        if parent is not None and not parent.is_alive():
            stop.set()
            time.sleep(grace)
            if os.name == "posix":
                os.killpg(os.getpgrp(), signal.SIGKILL)
            os._exit(70)


def _controller(options: dict, role: str, worker_id: str):
    if os.name == "posix":
        os.setsid()
    stop = threading.Event()
    threading.Thread(target=_controller_parent_guard,
                     args=(stop, options["drain_timeout"] + _DRAIN_CLEANUP_SECONDS),
                     daemon=True, name="workflow-supervisor-parent").start()
    try:
        from bili_asr.runtime_bindings import load_runtime_bindings
        from bili_asr.services.artifact_access import parse_target_bindings
        bindings = load_runtime_bindings(options["runtime_bindings"])
        artifacts = roots_for(options["archive_root"], flag_value=options["artifact_root"], require_writable=True)
        with ArchiveSession(options["archive_root"], mode=ArchiveAccessMode.WRITE, artifact_roots=artifacts) as session:
            WorkflowApplication(session).run(worker_id=worker_id,
                sessdata=resolve_sessdata(None, os.environ.get(SESSDATA_ENV_VAR)), role=role,
                poll_interval_seconds=options["poll_interval"], shutdown_event=stop,
                drain_file=options["drain_file"], drain_timeout_seconds=options["drain_timeout"],
                gpu_session=options["gpu_session"], config_resolver=None if bindings is None else bindings.resolve,
                asr_prefetch=options["asr_prefetch"], asr_prefetch_bytes=options["asr_prefetch_bytes"],
                warmup_audio=options["warmup_audio"], warmup_timeout_seconds=options["warmup_timeout_seconds"],
                cache_root=options["cache_root"], cache_max_bytes=options["cache_max_bytes"],
                storage_targets=parse_target_bindings(options.get("storage_targets")))
    except Exception as exc:  # noqa: BLE001 - bounded supervisor diagnostics contain no exception text.
        write_stderr(f"workflow worker startup/exit: role={role} error={type(exc).__name__}")
        raise SystemExit(70) from None


@dataclass
class _Slot:
    role: str
    index: int
    process: object | None = None
    restarts: int = 0
    started_at: float = 0
    retry_at: float = 0


def _kill_owned_group(process) -> None:
    if os.name == "posix" and process.pid is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class WorkerSupervisor:
    """Own exactly the configured processes and reap a slot before replacing it."""

    def __init__(self, *, archive_root, slots: Mapping[str, int], artifact_root=None,
                 poll_interval: float = 5, drain_file=None, drain_timeout: float = 60,
                 max_restarts: int = 8, gpu_session: str = "persistent", asr_prefetch: bool = False,
                 asr_prefetch_bytes: int = 64 * 1024 * 1024, runtime_bindings=None,
                 warmup_audio: str | None = None, warmup_timeout_seconds: float = 300,
                 cache_root: str | None = None, cache_max_bytes: int = 10 * 1024**3,
                 storage_targets: list[str] | None = None,
                 context=None, controller: Callable = _controller):
        root = Path(archive_root).resolve(strict=True)
        if any(role not in {"asr", "cpu", "editorial", "acquisition"} for role in slots):
            raise ValueError("unsupported worker role")
        if any(type(count) is not int or count < 0 or count > 32 for count in slots.values()) or not sum(slots.values()):
            raise ValueError("worker slot counts must be 0..32 with at least one active slot")
        if type(max_restarts) is not int or max_restarts < 0:
            raise ValueError("max_restarts must be a nonnegative integer")
        if not math.isfinite(poll_interval) or poll_interval <= 0 or not math.isfinite(drain_timeout) or drain_timeout <= 0:
            raise ValueError("poll_interval and drain_timeout must be finite and positive")
        if gpu_session not in {"persistent", "oneshot"}:
            raise ValueError("unknown GPU session mode")
        if type(asr_prefetch_bytes) is not int or asr_prefetch_bytes < 1:
            raise ValueError("asr_prefetch_bytes must be positive")
        if not math.isfinite(warmup_timeout_seconds) or warmup_timeout_seconds <= 0:
            raise ValueError("warmup timeout must be finite and positive")
        if warmup_audio is not None and (not Path(warmup_audio).is_absolute() or not Path(warmup_audio).is_file()):
            raise ValueError("warmup audio must be an existing absolute file")
        if cache_root is not None and not Path(cache_root).is_absolute():
            raise ValueError("cache root must be absolute")
        if type(cache_max_bytes) is not int or cache_max_bytes < 1:
            raise ValueError("cache startup byte limit must be positive")
        self.options = {"archive_root": str(root), "artifact_root": artifact_root,
            "poll_interval": poll_interval, "drain_file": drain_file, "drain_timeout": drain_timeout,
            "gpu_session": gpu_session, "asr_prefetch": asr_prefetch,
            "asr_prefetch_bytes": asr_prefetch_bytes, "runtime_bindings": runtime_bindings,
            "warmup_audio": warmup_audio, "warmup_timeout_seconds": warmup_timeout_seconds,
            "cache_root": cache_root, "cache_max_bytes": cache_max_bytes, "storage_targets": storage_targets}
        self.root, self.max_restarts = root, max_restarts
        self.context = context or multiprocessing.get_context("spawn")
        self.controller = controller
        self.slots = [_Slot(role, index) for role, count in slots.items() for index in range(count)]
        self.stop = threading.Event()
        self.identity = uuid4().hex[:12]

    def _draining(self):
        return self.stop.is_set() or (self.options["drain_file"] is not None and Path(self.options["drain_file"]).exists())

    def tick(self, *, now: float | None = None):
        """Inspect only owned process handles; never infer an unrelated process's role."""
        now = time.monotonic() if now is None else now
        if self._draining():
            return
        for slot in self.slots:
            if slot.process is not None:
                if slot.process.is_alive():
                    continue
                _kill_owned_group(slot.process)
                slot.process.join(timeout=0)
                slot.process.close()
                slot.process = None
                if now - slot.started_at >= 60:
                    slot.restarts = 0
                slot.restarts += 1
                if slot.restarts > self.max_restarts:
                    raise ValueError(f"worker slot restart budget exhausted: role={slot.role} slot={slot.index}")
                slot.retry_at = now + min(30, 0.5 * 2 ** min(slot.restarts - 1, 6))
            if now < slot.retry_at:
                continue
            worker_id = f"supervisor-{self.identity}-{slot.role}-{slot.index}"
            process = self.context.Process(target=self.controller,
                args=(self.options, slot.role, worker_id), name=worker_id)
            try:
                process.start()
            except BaseException:
                process.close()
                raise
            slot.process, slot.started_at = process, now

    def close(self):
        self.stop.set()
        for slot in self.slots:
            if slot.process is not None and slot.process.is_alive():
                if os.name == "posix":
                    try:
                        os.kill(slot.process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                else:
                    # Windows has no equivalent to SIGTERM delivery to a Python handler.
                    # The normal drain-file path is graceful; terminate is the bounded fallback.
                    slot.process.terminate()
        deadline = time.monotonic() + self.options["drain_timeout"] + _DRAIN_CLEANUP_SECONDS
        while any(slot.process is not None and slot.process.is_alive() for slot in self.slots):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        for slot in self.slots:
            process, slot.process = slot.process, None
            if process is None:
                continue
            if process.is_alive():
                try:
                    if os.name == "posix" and os.getpgid(process.pid) == process.pid:
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
            process.join(timeout=2)
            _kill_owned_group(process)
            process.close()

    def run(self):
        old_signals = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                old_signals[signum] = signal.signal(signum, lambda *_args: self.stop.set())
        try:
            with _slot_lock(self.root):
                # A read-only contract check precedes controller construction.
                with ArchiveSession(self.root, mode=ArchiveAccessMode.READ):
                    pass
                try:
                    while not self._draining():
                        self.tick()
                        self.stop.wait(0.1)
                finally:
                    self.close()
        finally:
            for signum, previous in old_signals.items():
                signal.signal(signum, previous)
