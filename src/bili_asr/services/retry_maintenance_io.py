"""Single-host retry state coordination; compatible with production flock."""

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


class MaintenanceBusyError(RuntimeError):
    pass


@contextmanager
def maintenance_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise MaintenanceBusyError("maintenance lock is held") from exc
            unlock = lambda: (handle.seek(0), msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1))
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MaintenanceBusyError("maintenance lock is held") from exc
            unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        try:
            yield
        finally:
            unlock()


def read_json(path: Path) -> dict:
    def unique_fields(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate maintenance JSON field")
            value[key] = item
        return value
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)
    if not isinstance(value, dict):
        raise ValueError("maintenance JSON must be an object")  # noqa: TRY004 - external JSON contract
    return value


def write_json(path: Path, value: dict) -> None:
    """Replace durably; callers hold the stable lock, never the replaced inode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Windows scanners may briefly hold a closed destination without delete
        # sharing. Retry only the rename and keep the durable original on failure.
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32} or attempt == 4:
                    raise
                time.sleep(0.05)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def update_health(path: Path, **changes) -> dict:
    with maintenance_lock(path.with_suffix(path.suffix + ".lock")):
        value = read_json(path) if path.exists() else {"schema_version": 1}
        if "last_lock_skipped_at" in changes:
            value["lock_skip_count"] = value.get("lock_skip_count", 0) + 1
        value.update(changes)
        write_json(path, value)
        return value
