"""Fix each ASR input to one verified audio generation, including legacy archives."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from bili_asr.artifact_inventory import stream_hash
from bili_asr.artifact_root import ArtifactRoots, iter_audio_paths
from bili_asr.file_generation import stream_generation as _generation
from bili_asr.path_policy import confined_audio_file, confined_audio_path
from bili_asr.storage.artifact_catalog import ArtifactCatalog
from bili_asr.workflow_errors import JobExecutionError


@dataclass(frozen=True)
class VerifiedAudioInput:
    path: Path
    evidence: Mapping[str, Any]
    byte_size: int
    sha256: str
    generation: tuple[int, ...]
    source_base: Path
    source_key: str
    source_generation: tuple[int, ...]
    checkpoint: Callable[[], None]

    def assert_unchanged(self) -> None:
        """Refuse results if the fixed inode was written during inference."""
        with confined_audio_file(self.path.parent.parent, f"audio/{self.path.name}") as path, open(path, "rb") as source:
            before = _generation(source)
            actual_size, actual_sha256 = stream_hash(source, checkpoint=self.checkpoint)
            after = _generation(source)
        if (actual_size, actual_sha256) != (self.byte_size, self.sha256):
            raise JobExecutionError("audio_digest_mismatch", {
                "stage": "after-inference", "expected_sha256": self.sha256,
                "expected_byte_size": self.byte_size, "actual_sha256": actual_sha256,
                "actual_byte_size": actual_size, "action": "restore-input-then-retry"})
        if before != self.generation or after != self.generation:
            raise _generation_changed("after-inference", self.generation, after)
        try:
            with confined_audio_file(self.source_base, self.source_key) as path, open(path, "rb") as source:
                source_generation = _generation(source)
        except OSError:
            raise JobExecutionError("audio_generation_changed", {
                "stage": "after-inference", "reason": "source-generation-unavailable"}) from None
        if source_generation != self.source_generation:
            raise _generation_changed("after-inference", self.source_generation, source_generation)


def _generation_sha256(generation: tuple[int, ...]) -> str:
    return hashlib.sha256(repr(generation).encode("ascii")).hexdigest()


def _generation_changed(stage: str, expected: tuple[int, ...], actual: tuple[int, ...]):
    return JobExecutionError("audio_generation_changed", {
        "stage": stage, "expected_generation": _generation_sha256(expected),
        "actual_generation": _generation_sha256(actual), "action": "restore-input-then-retry"})


def _clone_or_copy(source, destination, checkpoint):
    """CoW when supported; otherwise copy one bounded buffer per checkpoint."""
    if os.name == "posix":
        import fcntl
        destination.flush()
        try:
            fcntl.ioctl(destination.fileno(), 0x40049409, source.fileno())  # Linux FICLONE
        except OSError:
            destination.seek(0)
            destination.truncate()
        else:
            checkpoint()
            with open(destination.name, "rb") as cloned:
                return stream_hash(cloned, checkpoint=checkpoint)
    return stream_hash(source, destination, checkpoint=checkpoint)


def _expected_identity(connection: sqlite3.Connection, part_id: int,
                       result: Mapping[str, Any], retained: Mapping[str, Any] | None):
    sha256 = result.get("sha256")
    if sha256 is None:
        sha256 = result.get("object_id")
    byte_size = result.get("byte_size")
    if sha256 is not None and (not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None):
        raise JobExecutionError("artifact_input_identity_mismatch", {"reason": "invalid-sha256"})
    if result.get("object_id") is not None and result["object_id"] != sha256:
        raise JobExecutionError("artifact_input_identity_mismatch", {"reason": "dependency-sha-conflict"})
    if byte_size is not None and (type(byte_size) is not int or byte_size < 1):
        raise JobExecutionError("artifact_input_identity_mismatch", {"reason": "invalid-byte-size"})
    if retained is not None:
        if byte_size is not None and byte_size != retained["byte_size"]:
            raise JobExecutionError("artifact_input_identity_mismatch", {"reason": "dependency-size-conflict"})
        return retained["object_id"], retained["byte_size"]
    if sha256 is not None:
        row = connection.execute(
            "SELECT a.byte_size FROM part_audio_objects p JOIN audio_objects a USING(audio_id) "
            "WHERE p.video_part_id=? AND a.sha256=?", (part_id, sha256)).fetchone()
        if row is not None:
            if byte_size is not None and byte_size != row["byte_size"]:
                raise JobExecutionError("artifact_input_identity_mismatch", {"reason": "dependency-size-conflict"})
            byte_size = row["byte_size"]
    return sha256, byte_size


@contextmanager
def verified_audio_input(connection: sqlite3.Connection, roots: ArtifactRoots, part_id: int,
                         result: Mapping[str, Any], *, retained: Mapping[str, Any] | None,
                         checkpoint: Callable[[], None]) -> Iterator[VerifiedAudioInput]:
    """Verify the exact prerequisite and keep its inode available to spawn children.

    A private inode cannot change when another profile links or replaces the
    acquisition filename. CoW is preferred; the bounded-copy fallback reserves
    one compressed input's size plus headroom before any bytes are copied.
    """
    storage_key = result.get("storage_key")
    if not isinstance(storage_key, str) or not storage_key:
        raise JobExecutionError("audio_missing", {"reason": "dependency-storage-key-missing"})
    expected_sha256, expected_size = _expected_identity(connection, part_id, result, retained)
    keys = [storage_key]
    if retained is not None:
        for replica in ArtifactCatalog(connection).replicas_for_object(retained["object_id"]):
            if replica["package_id"] is None and replica["target_kind"] == "local" and replica["relative_key"] not in keys:
                keys.append(replica["relative_key"])
    mismatch = None
    from bili_asr.services.asr_input_space import reserve_input_space
    for base, key, candidate in iter_audio_paths(roots, keys):
        checkpoint()
        fixed_key = f"audio/.workflow-audio-input-{uuid4().hex}{candidate.suffix}"
        fixed = confined_audio_path(roots.write_base, fixed_key, require_exists=False)
        if fixed is None:
            raise JobExecutionError("audio_input_space_unavailable", {"reason": "private-input-directory-unavailable"})
        reservation = ExitStack()
        try:
            with confined_audio_file(base, key) as source_path, open(source_path, "rb") as source:
                source_generation = _generation(source)
                reservation.enter_context(reserve_input_space(
                    roots, roots.write_base, source_generation[2], checkpoint, connection=connection))
                with fixed.open("xb") as destination:
                    size, sha256 = _clone_or_copy(source, destination, checkpoint)
                after = _generation(source)
            with fixed.open("rb") as copied:
                generation = _generation(copied)
            checkpoint()
            if source_generation != after:
                raise _generation_changed("before-inference", source_generation, after)
        except OSError:
            fixed.unlink(missing_ok=True)
            reservation.close()
            continue
        except BaseException:
            fixed.unlink(missing_ok=True)
            reservation.close()
            raise
        if ((expected_sha256 is not None and sha256 != expected_sha256)
                or (expected_size is not None and size != expected_size)):
            mismatch = {"stage": "before-inference", "expected_sha256": expected_sha256,
                        "expected_byte_size": expected_size, "actual_sha256": sha256,
                        "actual_byte_size": size, "action": "restore-input-then-retry"}
            fixed.unlink(missing_ok=True)
            reservation.close()
            continue
        verification = {"schema_version": 1, "status": "matched" if expected_sha256 is not None else "observed-only",
                        "expected_sha256": expected_sha256, "expected_byte_size": expected_size,
                        "observed_sha256": sha256, "observed_byte_size": size,
                        "generation_sha256": _generation_sha256(source_generation), "verified_at": int(time.time())}
        try:
            yield VerifiedAudioInput(fixed, {**dict(result), "verification": verification}, size, sha256, generation,
                                     base, key, source_generation, checkpoint)
        finally:
            fixed.unlink(missing_ok=True)
            reservation.close()
        return
    if retained is not None:
        raise JobExecutionError("artifact_input_unavailable", {
            "object_id": retained["object_id"], "action": "artifacts-restore", "reason": "local-copy-unavailable",
            **({"identity": mismatch} if mismatch is not None else {})})
    if mismatch is not None:
        raise JobExecutionError("audio_digest_mismatch", mismatch)
    raise JobExecutionError("audio_missing", {"reason": "local-copy-unavailable",
                                             "expected_sha256": expected_sha256,
                                             "expected_byte_size": expected_size})
