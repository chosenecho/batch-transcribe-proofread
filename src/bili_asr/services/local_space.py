"""Count both local reservation ledgers under the caller's capacity fence.

Kernel ownership is authoritative. Receipts and database rows describe the
reserved bytes, and may be retired only after acquiring their owner's fence.
The optional online table is read as installed; this reader installs no schema.
"""
from __future__ import annotations

import json
import stat
import time
from pathlib import Path

from bili_asr.archive_maintenance import ArchiveBusyError
from bili_asr.artifact_fences import resource_fence
from bili_asr.file_ops import checked_path
from bili_asr.storage_targets import unique_json_object

MAX_LOCAL_RESERVATIONS = 4096


class SpaceReservationError(ValueError):
    """Reservation evidence cannot safely be included in capacity admission."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("artifact_space_reserved: " + reason)


def _confirm_owner_released(roots, receipt: Path) -> None:
    """A vanished receipt is safe only after its kernel owner has released it."""
    try:
        with resource_fence(roots, "asr-input:" + receipt.stem, exclusive=True):
            pass
    except ArchiveBusyError:
        raise SpaceReservationError("missing-live-reservation") from None


def _input_reservation_bytes(roots, receipt: Path) -> int:
    try:
        checked_path(receipt)
        info = receipt.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise SpaceReservationError("invalid-reservation")
        try:
            with resource_fence(roots, "asr-input:" + receipt.stem, exclusive=True):
                receipt.unlink(missing_ok=True)
                return 0
        except ArchiveBusyError:
            record = json.loads(receipt.read_text("utf-8"), object_pairs_hook=unique_json_object)
    except FileNotFoundError:
        # Normal owner release can race with glob/stat or the guarded read.
        # A still-live owner with missing evidence must prevent admission.
        _confirm_owner_released(roots, receipt)
        return 0
    except (ValueError, UnicodeError) as error:
        if isinstance(error, SpaceReservationError):
            raise
        raise SpaceReservationError("invalid-reservation") from None
    if (not isinstance(record, dict) or set(record) != {"schema_version", "byte_size"}
            or type(record["schema_version"]) is not int or record["schema_version"] != 1
            or type(record["byte_size"]) is not int or record["byte_size"] < 0):
        raise SpaceReservationError("invalid-reservation")
    return record["byte_size"]


def _database_reservation_bytes(connection, roots, checkpoint) -> int:
    if connection is None or connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='artifact_reservations'"
    ).fetchone() is None:
        return 0
    reserved = 0
    rows = connection.execute(
        "SELECT reservation_id,byte_count FROM artifact_reservations WHERE released_at IS NULL"
    ).fetchmany(MAX_LOCAL_RESERVATIONS + 1)
    if len(rows) > MAX_LOCAL_RESERVATIONS:
        raise SpaceReservationError("reservation-scan-limit")
    for identity, byte_count in rows:
        checkpoint()
        if (not isinstance(identity, str) or not identity
                or type(byte_count) is not int or byte_count < 0):
            raise SpaceReservationError("invalid-database-reservation")
        try:
            with resource_fence(roots, "reservation:" + identity, exclusive=True), connection:
                connection.execute(
                    "UPDATE artifact_reservations SET released_at=? "
                    "WHERE reservation_id=? AND released_at IS NULL", (int(time.time()), identity))
        except ArchiveBusyError:
            reserved += byte_count
    return reserved


def reserved_local_bytes(connection, roots, checkpoint=lambda: None) -> int:
    """Include every configured/fallback scope while holding ``capacity``.

    Separate bindings may use the same filesystem. Counting both is deliberately
    conservative; no filesystem identity is inferred from a root name.
    """
    reserved = _database_reservation_bytes(connection, roots, checkpoint)
    scanned = 0
    for base in roots.read_bases():
        directory = checked_path(base / ".asr-input-reservations")
        if not directory.exists():
            continue
        for receipt in directory.glob("*.json"):
            checkpoint()
            scanned += 1
            if scanned > MAX_LOCAL_RESERVATIONS:
                raise SpaceReservationError("reservation-scan-limit")
            reserved += _input_reservation_bytes(roots, receipt)
    return reserved
