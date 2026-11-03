"""Kernel-owned reservations for private ASR inputs on their actual filesystem."""
from __future__ import annotations

import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from bili_asr.archive_maintenance import ArchiveBusyError
from bili_asr.artifact_fences import resource_fence
from bili_asr.file_ops import atomic_replace_file, checked_path, json_bytes
from bili_asr.services.local_space import SpaceReservationError, reserved_local_bytes
from bili_asr.workflow_errors import JobExecutionError

INPUT_MINIMUM_FREE_BYTES = 64 * 1024 * 1024


@contextmanager
def _capacity_fence(roots, checkpoint):
    deadline = time.monotonic() + 5
    while True:
        checkpoint()
        lock = resource_fence(roots, "capacity", exclusive=True)
        try:
            lock.__enter__()
        except ArchiveBusyError:
            if time.monotonic() >= deadline:
                raise JobExecutionError("audio_input_space_unavailable", {"reason": "reservation-busy"}) from None
            time.sleep(.02)
        else:
            break
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


@contextmanager
def reserve_input_space(roots, base: Path, byte_size: int, checkpoint, *, connection=None):
    """No timer guesses: a live reservation owns its kernel fence until exit."""
    if type(byte_size) is not int or byte_size < 0:
        raise ValueError("space reservation must be a nonnegative integer")
    directory = checked_path(base / ".asr-input-reservations")
    directory.mkdir(exist_ok=True)
    identity = uuid4().hex
    receipt = directory / f"{identity}.json"
    with resource_fence(roots, "asr-input:" + identity, exclusive=False):
        with _capacity_fence(roots, checkpoint):
            try:
                reserved = reserved_local_bytes(connection, roots, checkpoint)
            except SpaceReservationError as error:
                raise JobExecutionError("audio_input_space_unavailable", {"reason": error.reason}) from None
            free = shutil.disk_usage(base).free
            if free - reserved - byte_size < INPUT_MINIMUM_FREE_BYTES:
                raise JobExecutionError("audio_input_space_unavailable", {
                    "reason": "insufficient-unreserved-space", "byte_size": byte_size,
                    "reserved_byte_size": reserved, "free_byte_size": free,
                    "minimum_free_byte_size": INPUT_MINIMUM_FREE_BYTES})
            atomic_replace_file(receipt, json_bytes({"schema_version": 1, "byte_size": byte_size}))
        try:
            checkpoint()
            yield
        finally:
            receipt.unlink(missing_ok=True)
