"""Kernel-owned object fences and shared local-space reservations.

Locks outlive an execution lease. They are released by the owning process or
the kernel on process exit, never by guessing whether a PID or timer is stale.
Persistent observations can be retired only while holding the corresponding
exclusive kernel fence. Manual pins are never retired here.
"""
from __future__ import annotations

import shutil
import time
import uuid
from contextlib import contextmanager

from bili_asr.archive_maintenance import ArchiveBusyError
from bili_asr.artifact_fences import object_fence, resource_fence
from bili_asr.services.local_space import reserved_local_bytes
from bili_asr.storage.artifact_catalog import ArtifactCatalog


@contextmanager
def consumer_pin(connection, roots, object_id: str, *, owner: str):
    """Hold through decoding, inference and every remaining read of this input."""
    with object_fence(roots, object_id, exclusive=False):
        catalog = ArtifactCatalog(connection)
        with connection:
            identity = catalog.pin_object(object_id, "runtime-consumer:" + owner[:900], pin_id="runtime:" + uuid.uuid4().hex)
        try:
            yield identity
        finally:
            with connection:
                catalog.release_pin(identity)


def reconcile_consumer_pins(connection, roots) -> int:
    count = 0
    for object_id, in connection.execute("SELECT DISTINCT object_id FROM artifact_pins WHERE pin_id LIKE 'runtime:%' AND reason LIKE 'runtime-consumer:%' AND released_at IS NULL").fetchall():
        try:
            with object_fence(roots, object_id, exclusive=True), connection:
                count += connection.execute("UPDATE artifact_pins SET released_at=? WHERE object_id=? AND pin_id LIKE 'runtime:%' AND reason LIKE 'runtime-consumer:%' AND released_at IS NULL",
                                            (int(time.time()), object_id)).rowcount
        except ArchiveBusyError:
            continue
    return count


@contextmanager
def reserve_local_space(connection, roots, byte_count: int, *, owner: str, minimum_free_bytes: int = 0):
    """Reserve peak added bytes across restores/downloads, without lease expiry."""
    if type(byte_count) is not int or byte_count < 0 or type(minimum_free_bytes) is not int or minimum_free_bytes < 0:
        raise ValueError("space reservation must be a nonnegative integer")
    identity = uuid.uuid4().hex
    scope = "local-artifacts" if roots.configured else "local"
    with resource_fence(roots, "reservation:" + identity, exclusive=False):
        with resource_fence(roots, "capacity", exclusive=True):
            reserved = reserved_local_bytes(connection, roots)
            if shutil.disk_usage(roots.write_base).free - reserved - byte_count < minimum_free_bytes:
                raise ValueError("artifact_space_reserved: insufficient unreserved local space")
            with connection:
                connection.execute("INSERT INTO artifact_reservations VALUES (?,?,?,?,?,NULL)",
                                   (identity, scope, byte_count, owner[:1024], int(time.time())))
        try:
            yield identity
        finally:
            with connection:
                connection.execute("UPDATE artifact_reservations SET released_at=? WHERE reservation_id=?", (int(time.time()), identity))
