"""Allowlisted failure facts; never serialize an exception message or traceback."""

from __future__ import annotations

import errno
import sqlite3
from collections.abc import Mapping

from bili_asr.workflow_errors import safe_job_details

_PHASES = frozenset({"workflow_execution", "model_load", "audio_prepare", "decode", "align",
                     "inference", "startup", "ipc", "result_admission", "result_commit", "failure_persistence"})
# Fixed historical class codes preserve retry/hold policy without accepting
# arbitrary class names, messages or error_code attributes.
_TYPES = frozenset({"Exception", "BaseException", "ValueError", "TypeError", "RuntimeError",
    "AssertionError", "KeyError", "IndexError", "OverflowError", "MemoryError", "OSError",
    "PermissionError", "FileNotFoundError", "FileExistsError", "NotADirectoryError",
    "IsADirectoryError", "BrokenPipeError", "ConnectionError", "TimeoutError",
    "TimeoutException", "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
    "PoolTimeout", "NetworkError", "RemoteProtocolError", "DeepSeekTransportError", "RuntimeCacheError",
    "OperationalError", "IntegrityError", "DatabaseError", "ProgrammingError", "DataError",
    "InterfaceError", "NotSupportedError", "SystemExit", "KeyboardInterrupt",
    "ASRDependencyError", "ASRRuntimeCompatibilityError", "ASRModelError", "AudioDecodeError",
    "InferenceSessionError", "ASRInferenceTimeoutError"})
_COUNTS = frozenset({"segment_ordinal", "chunk_index", "exit_code", "attempt_count"})
_REASONS = frozenset({"child_error", "child_exited", "response_identity_mismatch",
                     "total_deadline", "startup_deadline", "no_progress", "capture_unavailable"})


def filter_failure_details(value: object) -> dict:
    """Revalidate child-supplied data against a closed schema, with a tiny size bound."""
    if not isinstance(value, Mapping):
        return {"phase": "ipc", "reason": "capture_unavailable"}
    result = {}
    for name, allowed in (("phase", _PHASES), ("exception_type", _TYPES), ("reason", _REASONS)):
        item = value.get(name)
        if type(item) is str and item in allowed:
            result[name] = item
    for name in _COUNTS:
        number = value.get(name)
        if type(number) is int and -2**31 <= number <= 2**31 - 1:
            result[name] = number
    number = value.get("errno")
    if type(number) is int and number in errno.errorcode:
        result["errno"] = number
        result["errno_name"] = errno.errorcode[number]
    code, name = value.get("sqlite_errorcode"), value.get("sqlite_errorname")
    if type(code) is int and 0 <= code <= 2**31 - 1:
        result["sqlite_errorcode"] = code
        if (isinstance(name, str) and name.startswith("SQLITE_") and len(name) <= 64
                and getattr(sqlite3, name, None) == code):
            result["sqlite_errorname"] = name
    return safe_job_details(result or {"phase": "ipc", "reason": "capture_unavailable"})


def capture_failure(exc: BaseException, *, phase: str = "workflow_execution", **location) -> dict:
    """Best-effort capture cannot turn a failure into a second diagnostic exception."""
    try:
        kind = type(exc).__name__
        result = {"phase": phase, "exception_type": kind if kind in _TYPES else "Exception"}
        if isinstance(exc, DiagnosedFailure):
            result.update(exc.safe_details)
        if isinstance(exc, OSError):
            result["errno"] = exc.errno
        if isinstance(exc, sqlite3.Error):
            result.update(sqlite_errorcode=getattr(exc, "sqlite_errorcode", None),
                          sqlite_errorname=getattr(exc, "sqlite_errorname", None))
        result.update(location)
        return filter_failure_details(result)
    except Exception:  # noqa: BLE001 - even hostile exception attributes are not serialized.
        return {"phase": "workflow_execution", "reason": "capture_unavailable"}


class DiagnosedFailure:
    """Marker for reviewed, message-free diagnostic data from our process boundary."""

    safe_details: dict

    def set_failure_details(self, value: object) -> None:
        self.safe_details = filter_failure_details(value)
