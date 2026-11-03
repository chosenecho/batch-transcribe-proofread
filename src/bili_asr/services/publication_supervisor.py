"""Bound publication waits while the worker alone owns archive writer locks.

Only explicit phase transitions reset the deadline. Output is inherited, so a
blocked output stream or a descendant retaining stdout cannot stall supervision.
Kernel-blocked workers can retain their own locks until the kernel recovers;
the supervisor never transfers or releases those locks.
"""
from __future__ import annotations

from contextlib import contextmanager
import math
import os
import secrets
import signal
import socket
import subprocess
import sys
import time

from bili_asr.diagnostics import write_stderr
from bili_asr.process_environment import worker_environment

DEFAULT_IO_TIMEOUT_SECONDS = 60.0
_UNREAPED: list[subprocess.Popen] = []
_PROGRESS: socket.socket | None = None


def publication_phase(phase: str) -> None:
    """Signal candidate start or final persistence from the isolated worker."""
    if _PROGRESS is not None:
        _PROGRESS.sendall({"candidate": b"C", "final": b"F"}[phase])


@contextmanager
def publication_progress(progress: socket.socket):
    """Bind the isolated worker's phase channel for one invocation."""
    global _PROGRESS
    if _PROGRESS is not None:
        raise RuntimeError("publication progress channel is already active")
    _PROGRESS = progress
    try:
        yield
    finally:
        _PROGRESS = None


def _command(port: int, token: str, argv: list[str]) -> list[str]:
    return [sys.executable, "-m", "bili_asr.publication_worker", str(port), token, *argv]


def _terminate(worker: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            os.killpg(worker.pid, signal.SIGKILL)
        except OSError:
            pass
    else:
        try:
            # A venv executable may be a launcher with the actual interpreter
            # beneath it. Terminate the tree, not only the launcher handle.
            killer = subprocess.Popen(
                ["taskkill", "/PID", str(worker.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                killer.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                killer.kill()
                try:
                    killer.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    _UNREAPED.append(killer)
        except OSError:
            pass
        try:
            worker.kill()
        except OSError:
            pass
    try:
        worker.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        _UNREAPED.append(worker)


def supervise_publication(argv: list[str], *, timeout_seconds: float) -> int:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        write_stderr("publish-transcripts: --io-timeout-seconds must be finite and positive")
        return 1
    _UNREAPED[:] = [worker for worker in _UNREAPED if worker.poll() is None]
    if len(_UNREAPED) >= 4:
        write_stderr("publish-transcripts: publication worker capacity exhausted")
        return 1
    token = secrets.token_hex(16)
    progress = None
    worker = None
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.setblocking(False)
        deadline = time.monotonic() + timeout_seconds
        try:
            worker = subprocess.Popen(
                _command(listener.getsockname()[1], token, argv),
                stdout=sys.stdout, stderr=sys.stderr,
                env=worker_environment(), close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name == "posix",
            )
            authenticated = False
            final_seen = False
            greeting = b""
            while True:
                status = worker.poll()
                if status is not None:
                    return status if status >= 0 else 1
                if time.monotonic() >= deadline:
                    _terminate(worker)
                    write_stderr("publish-transcripts: publication I/O deadline exceeded; invocation stopped; worker-owned locks may remain until I/O recovers")
                    return 1
                if progress is None:
                    try:
                        progress, _ = listener.accept()
                        progress.setblocking(False)
                    except BlockingIOError:
                        pass
                if progress is not None:
                    try:
                        data = progress.recv(4096)
                    except BlockingIOError:
                        data = None
                    if data:
                        if not authenticated:
                            greeting += data
                            if len(greeting) >= 33:
                                if greeting[:33] != token.encode("ascii") + b"\n":
                                    raise OSError("invalid publication progress channel")
                                authenticated = True
                                data = greeting[33:]
                        if authenticated and data:
                            for value in data:
                                if final_seen or value not in (ord("C"), ord("F")):
                                    raise OSError("invalid publication phase")
                                final_seen = value == ord("F")
                            deadline = time.monotonic() + timeout_seconds
                time.sleep(min(0.02, max(0.001, deadline - time.monotonic())))
        except OSError:
            if worker is not None:
                _terminate(worker)
            write_stderr("publish-transcripts: publication supervisor failed")
            return 1
        except BaseException:
            if worker is not None:
                _terminate(worker)
            raise
        finally:
            if progress is not None:
                progress.close()


if __name__ == "__main__":
    from bili_asr.publication_worker import main
    raise SystemExit(main())
