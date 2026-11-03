"""Bounded, data-only diagnostics crossing the workflow execution boundary."""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import re
from typing import Any

_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_VALUE = re.compile(r"[A-Za-z0-9_.:+/-]{0,128}\Z")
_PRIVATE = re.compile(r"cookie|sessdata|secret|password|authorization|url|path|raw|content", re.I)


def safe_job_details(details: Mapping[str, Any]) -> dict[str, Any]:
    """Copy bounded identifiers/counts; reject free text and capability values."""
    def copy(value: Any, depth: int = 0) -> Any:
        if depth > 6:
            raise ValueError("job diagnostic nesting exceeds limit")
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            if abs(value) > 2**63 - 1:
                raise ValueError("job diagnostic integer exceeds limit")
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("job diagnostic number must be finite")
            return value
        if isinstance(value, str):
            if not _VALUE.fullmatch(value) or "://" in value:
                raise ValueError("job diagnostics accept identifiers, not free text")
            return value
        if isinstance(value, Mapping):
            if len(value) > 32:
                raise ValueError("too many job diagnostic fields")
            result = {}
            for key, item in value.items():
                if not isinstance(key, str) or not _CODE.fullmatch(key) or _PRIVATE.search(key):
                    raise ValueError("private or invalid job diagnostic field")
                result[key] = copy(item, depth + 1)
            return result
        if isinstance(value, (list, tuple)) and len(value) <= 32:
            return [copy(item, depth + 1) for item in value]
        raise ValueError("unsupported job diagnostic value")

    result = copy(details)
    if not isinstance(result, dict):
        raise ValueError("job diagnostics must be a mapping")
    if len(json.dumps(result, ensure_ascii=True).encode("ascii")) > 16384:
        raise ValueError("job diagnostics exceed 16 KiB")
    return result


class JobExecutionError(RuntimeError):
    """An actionable error with reviewed diagnostic data, never exception text."""

    def __init__(self, error_code: str, safe_details: Mapping[str, Any] | None = None):
        if not isinstance(error_code, str) or not _CODE.fullmatch(error_code):
            raise ValueError("invalid workflow error code")
        self.error_code = error_code
        self.safe_details = safe_job_details(safe_details or {})
        super().__init__(error_code)
