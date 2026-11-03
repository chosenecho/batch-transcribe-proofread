"""Database-free kernel fences shared by artifact services and decoder children."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager

from bili_asr.archive_maintenance import archive_access
from bili_asr.storage_targets import require_safe_path


@contextmanager
def resource_fence(roots, key: str, *, exclusive: bool):
    directory = roots.archive_root / ".artifact-runtime"
    require_safe_path(directory)
    directory.mkdir(exist_ok=True)
    identity = hashlib.sha256(key.encode()).hexdigest()
    with archive_access(directory / identity, exclusive=exclusive):
        yield


@contextmanager
def object_fence(roots, object_id: str, *, exclusive: bool):
    with resource_fence(roots, "object:" + object_id, exclusive=exclusive):
        yield
