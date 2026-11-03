"""Shared helpers for the service layer.

Single home for the run/attempt Unix-second clock the ingest services
persist; injectable so tests can pin the persisted timeline.
"""

from __future__ import annotations

import time

__all__ = ["_now"]


def _now() -> int:
    """Return the current Unix second used for all persisted clocks."""
    return int(time.time())
