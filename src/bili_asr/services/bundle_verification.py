"""Strict bundle hashing in a disposable read-only process.

The timeout bounds waiting for the worker, not kernel cancellation: a process
in uninterruptible I/O may survive SIGKILL until the kernel call returns. It
inherits no archive-writer descriptor and performs no publication writes.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Mapping
from bili_asr.process_environment import worker_environment as _worker_environment

DEFAULT_VERIFY_TIMEOUT_SECONDS = 30.0
DEFAULT_VERIFY_READ_BUDGET_BYTES = 256 * 1024 * 1024


class VerificationBudgetExceeded(OSError):
    """The shared invocation budget cannot establish complete verification."""


@dataclass
class VerificationReadBudget:
    """Finite byte allowance; unresolved workers retain their full reservation."""
    remaining: int = DEFAULT_VERIFY_READ_BUDGET_BYTES
    read_bytes: int = 0

    def __post_init__(self):
        if isinstance(self.remaining, bool) or not isinstance(self.remaining, int):
            raise TypeError("verification budget must be an integer")
        if self.remaining < 1:
            raise ValueError("verification budget must be positive")

_KILL_GRACE_SECONDS = 0.2
_MAX_UNREAPED = 4
# Keep references to unreaped workers; never block the publisher on wait().
_UNREAPED: list[subprocess.Popen] = []


def _command() -> list[str]:
    return [sys.executable, "-m", "bili_asr.services.bundle_verification"]


def _stop_worker(worker: subprocess.Popen) -> None:
    try:
        worker.kill()
    except ProcessLookupError:
        pass
    try:
        worker.communicate(timeout=_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _UNREAPED.append(worker)
        # A kernel-blocked worker may survive kill. Do not join or close its
        # buffered pipes (Windows communicate can own a blocked reader).


def verify_bundle(
    root: str | os.PathLike[str], paths: Mapping[str, str], *,
    timeout_seconds: float = DEFAULT_VERIFY_TIMEOUT_SECONDS,
    budget: VerificationReadBudget | None = None,
) -> bool:
    """Hash every artifact, or refuse an inconclusive read within the deadline.

    No stat/freshness cache replaces hashes. Timeout raises TimeoutError so
    the publisher preserves the unverified bundle and continues other parts.
    Process creation itself and other publisher I/O are outside this boundary.
    """
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise TypeError("timeout_seconds must be numeric")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    if budget is not None and not isinstance(budget, VerificationReadBudget):
        raise TypeError("budget must be a VerificationReadBudget")
    allowance = budget.remaining if budget is not None else None
    if allowance == 0:
        raise VerificationBudgetExceeded("verification read budget exhausted")
    payload = json.dumps({
        "root": os.fspath(root), "paths": dict(paths), "read_limit": allowance,
    }).encode()
    if len(payload) > 65536:
        raise ValueError("bundle declaration exceeds verification request limit")
    _UNREAPED[:] = [worker for worker in _UNREAPED if worker.poll() is None]
    if len(_UNREAPED) >= _MAX_UNREAPED:
        raise OSError("verification worker capacity exhausted")
    started = time.monotonic()
    worker = subprocess.Popen(
        _command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, close_fds=True,
        env=_worker_environment(),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    # Reserve before sending the request. A stalled or invalid-response
    # worker has unknown actual reads; never reuse its allowance elsewhere.
    if budget is not None:
        budget.remaining = 0
    try:
        output, _ = worker.communicate(
            payload, timeout=max(0.001, timeout_seconds - (time.monotonic() - started)),
        )
    except subprocess.TimeoutExpired:
        _stop_worker(worker)
        raise TimeoutError("bundle verification deadline exceeded") from None
    except BaseException:
        _stop_worker(worker)
        raise
    if worker.returncode != 0:
        raise OSError("bundle verification worker failed")
    try:
        result = json.loads(output)
    except (ValueError, UnicodeError):
        raise OSError("invalid bundle verification response") from None
    response = {key: value for key, value in result.items() if key != "read_bytes"} if isinstance(result, dict) else None
    valid_response = (
        (isinstance(response, dict) and set(response) == {"complete"}
         and isinstance(response["complete"], bool))
        or response == {"error": "budget_exhausted"}
        or (isinstance(response, dict) and set(response) == {"error", "errno"}
            and response["error"] == "unreadable"
            and (response["errno"] is None or
                 (isinstance(response["errno"], int) and not isinstance(response["errno"], bool))))
    )
    if not valid_response:
        raise OSError("invalid bundle verification response")
    if isinstance(result, dict) and "read_bytes" in result:
        read_bytes = result.pop("read_bytes")
        if isinstance(read_bytes, bool) or not isinstance(read_bytes, int) or read_bytes < 0:
            raise OSError("invalid verification byte count")
        if allowance is not None and read_bytes > allowance:
            raise OSError("verification worker exceeded byte allowance")
        if budget is not None:
            budget.read_bytes += read_bytes
            budget.remaining = allowance - read_bytes
    elif budget is not None:
        raise OSError("verification worker omitted byte count")
    if result == {"error": "budget_exhausted"}:
        raise VerificationBudgetExceeded("verification read budget exhausted")
    if result == {"complete": True}:
        return True
    if result == {"complete": False}:
        return False
    if isinstance(result, dict) and set(result) == {"error", "errno"} and result["error"] == "unreadable":
        number = result["errno"]
        if number is None or (isinstance(number, int) and not isinstance(number, bool)):
            raise OSError(number, "bundle verification could not read artifacts")
    raise OSError("bundle verification could not read artifacts")


def _worker() -> None:
    from bili_asr.archive import archive_bundle_complete
    request = json.loads(sys.stdin.buffer.read(65537))
    read_bytes = 0
    limit = request.get("read_limit")
    real_read = os.read

    def counted_read(fd: int, size: int) -> bytes:
        nonlocal read_bytes
        if limit is not None:
            remaining = limit - read_bytes
            if remaining <= 0:
                raise VerificationBudgetExceeded("verification read budget exhausted")
            size = min(size, remaining)
        chunk = real_read(fd, size)
        read_bytes += len(chunk)
        return chunk

    # All marker/artifact reads in the canonical archive reader use os.read.
    # Count returned bytes, never file stat estimates; cap the syscall itself.
    os.read = counted_read
    try:
        complete = archive_bundle_complete(
            request["root"], request["paths"], require_readable=True,
        )
        result = {"complete": complete}
    except VerificationBudgetExceeded:
        result = {"error": "budget_exhausted"}
    except OSError as exc:
        result = {"error": "unreadable", "errno": exc.errno}
    finally:
        os.read = real_read
    result["read_bytes"] = read_bytes
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    _worker()
