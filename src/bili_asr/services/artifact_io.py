"""Per-invocation byte accounting and bounded payload I/O rate."""
from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

_METER = ContextVar("artifact_io_meter", default=None)


@dataclass
class ArtifactIOMeter:
    bytes_per_second: int
    bytes_read: int = 0
    started: float = field(default_factory=time.monotonic)
    checkpoint: Callable[[], None] | None = None

    def observe(self, count: int):
        if self.checkpoint is not None:
            self.checkpoint()
        self.bytes_read += count
        if self.bytes_per_second:
            delay = self.bytes_read / self.bytes_per_second - (time.monotonic() - self.started)
            while delay > 0:
                time.sleep(min(delay, 0.25))
                if self.checkpoint is not None:
                    self.checkpoint()
                delay = self.bytes_read / self.bytes_per_second - (time.monotonic() - self.started)

    def report(self):
        return {"bytes_read": self.bytes_read, "elapsed_seconds": time.monotonic() - self.started,
                "bytes_per_second_limit": self.bytes_per_second}


def observe_io(count: int):
    meter = _METER.get()
    if meter is not None:
        meter.observe(count)


@contextmanager
def artifact_io_budget(bytes_per_second: int, *, checkpoint: Callable[[], None] | None = None):
    if type(bytes_per_second) is not int or bytes_per_second < 0:
        raise ValueError("I/O rate must be a nonnegative integer")
    meter = ArtifactIOMeter(bytes_per_second, checkpoint=checkpoint)
    token = _METER.set(meter)
    try:
        yield meter
    finally:
        _METER.reset(token)
