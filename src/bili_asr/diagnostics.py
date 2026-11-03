"""Best-effort diagnostics that cannot dirty a dead stderr buffer."""

import io
import os
import sys


def write_stderr(message: str) -> None:
    """Write one line, preserving the caller's stdout and process exit status.

    A real TextIOWrapper must bypass its buffer: swallowing a failed buffered
    write can still make CPython exit 120 when shutdown retries the flush.
    In-memory capture streams and host-provided writers use their write method.
    """
    stream = sys.stderr
    if stream is None:
        return
    try:
        line = message + "\n"
        if isinstance(stream, io.TextIOWrapper):
            try:
                descriptor = stream.fileno()
            except io.UnsupportedOperation:
                # Pytest and embedded hosts can wrap an in-memory buffer.
                stream.write(line)
                return
            remaining = line.encode(stream.encoding or "utf-8", errors="replace")
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    return
                remaining = remaining[written:]
        else:
            stream.write(line)
    except (OSError, ValueError, TypeError):
        return
