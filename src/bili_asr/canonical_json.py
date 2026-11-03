"""Frozen canonical JSON bytes and digests shared by durable identity readers."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


__all__ = ["canonical", "digest"]
