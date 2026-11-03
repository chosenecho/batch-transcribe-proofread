"""One drain deadline shared by signal, file and cooperative task checks."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import threading
import time

from bili_asr.workflow import WorkerDrainTimeout


class WorkerShutdown:
    def __init__(self, stop: threading.Event, *, drain_file: str | None, timeout: float | None):
        self.stop, self.drain_file, self.timeout = stop, drain_file, timeout
        self.started: float | None = None
        self.active = False
        self._closed = threading.Event()
        self._interrupt_sent = False

    def requested(self) -> bool:
        requested = self.stop.is_set() or (self.drain_file is not None and Path(self.drain_file).exists())
        if requested and self.started is None:
            self.started = time.monotonic()
        return requested

    def expired(self) -> bool:
        return (self.requested() and self.timeout is not None
                and time.monotonic() - self.started >= self.timeout)

    def checkpoint(self) -> None:
        if self.expired():
            self.active = False
            raise WorkerDrainTimeout("graceful worker shutdown deadline elapsed")

    @contextmanager
    def attempt(self):
        self.active = True
        try:
            self.checkpoint()
            yield
            self.checkpoint()
        finally:
            self.active = False

    def _signal(self, _signum, _frame):
        repeated = self.requested()
        self.stop.set()
        self.requested()
        if repeated and self.active:
            self.active = False
            raise WorkerDrainTimeout("repeated shutdown signal interrupted the current task")

    def _alarm(self, _signum, _frame):
        # A queued watchdog signal can arrive during terminal persistence. Only
        # interrupt the handler scope; its executor records the exact attempt.
        if self.active and self.expired():
            self.active = False
            raise WorkerDrainTimeout("graceful worker shutdown deadline elapsed")

    def _watch(self, interrupt: bool):
        while not self._closed.wait(0.05):
            self.requested()
            if interrupt and self.active and self.expired() and not self._interrupt_sent:
                self._interrupt_sent = True
                os.kill(os.getpid(), signal.SIGALRM)

    @contextmanager
    def install(self):
        previous = {}
        main = threading.current_thread() is threading.main_thread()
        # SIGALRM interrupts blocking Python/HTTP handlers on POSIX. Preserve
        # an application's existing timer rather than stealing its alarm port.
        interrupt = (main and os.name == "posix"
                     and signal.getsignal(signal.SIGALRM) == signal.SIG_DFL
                     and signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0))
        if main:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, self._signal)
            if interrupt:
                previous[signal.SIGALRM] = signal.signal(signal.SIGALRM, self._alarm)
        watcher = threading.Thread(target=self._watch, args=(interrupt,), name="workflow-drain", daemon=True)
        watcher.start()
        try:
            yield self
        finally:
            self.active = False
            blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM}) if interrupt else None
            self._closed.set()
            watcher.join(timeout=1)
            if interrupt and signal.SIGALRM in signal.sigpending():
                signal.sigwait({signal.SIGALRM})
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            if blocked is not None:
                signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
