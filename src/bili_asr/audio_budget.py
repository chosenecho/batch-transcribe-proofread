"""Audio-directory budget gate for bounded pilot campaigns.

Filesystem-only: measures current `audio/` usage and estimates a
candidate item's audio size so the CLI can skip downloads that would
breach the operator's peak-disk cap. Never opens sockets.
"""

from __future__ import annotations

import math
import os
import stat
from typing import Any, Mapping

# 64 kbps DASH audio ceiling → 8 000 bytes/s.
BYTES_PER_SECOND_CEILING = 8_000
SKIP_REASON = "audio_budget"
_BYTES_PER_GIB = 1024 ** 3


def audio_cap_bytes(max_audio_gb: float) -> int:
    """Convert ``--max-audio-gb`` to a byte cap.

    Non-positive values map to 0 (unlimited, matching ``would_exceed_budget``).
    Any positive GiB value maps to at least 1 byte so truncating ``int()``
    cannot silently disable the cap.
    """
    if max_audio_gb <= 0:
        return 0
    return max(1, math.ceil(max_audio_gb * _BYTES_PER_GIB))


class AudioUsageError(OSError):
    """The audio directory's usage cannot be measured reliably."""


def _audio_directory(archive_root: str | os.PathLike[str]) -> str | None:
    root = os.path.abspath(archive_root)
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            raise AudioUsageError("audio root is not a directory")
        audio_dir = os.path.join(root, "audio")
        try:
            mode = os.lstat(audio_dir).st_mode
        except FileNotFoundError:
            return None  # A verified, existing root may have no audio yet.
        if not stat.S_ISDIR(mode):
            raise AudioUsageError("audio directory is not a directory")
        return audio_dir
    except OSError as exc:
        raise AudioUsageError("audio usage unavailable") from exc


def _audio_file_sizes(archive_root: str | os.PathLike[str]) -> dict[str, int]:
    audio_dir = _audio_directory(archive_root)
    sizes: dict[str, int] = {}
    if audio_dir is None:
        return sizes

    def refuse_walk(exc: OSError) -> None:
        raise AudioUsageError("audio usage unavailable") from exc

    try:
        for dirpath, dirnames, filenames in os.walk(audio_dir, onerror=refuse_walk):
            for name in dirnames:
                if not stat.S_ISDIR(os.lstat(os.path.join(dirpath, name)).st_mode):
                    raise AudioUsageError("audio directory contains a symlink")
            for name in filenames:
                path = os.path.join(dirpath, name)
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode):
                    raise AudioUsageError("audio directory contains a non-regular file")
                sizes[path] = info.st_size
    except OSError as exc:
        raise AudioUsageError("audio usage unavailable") from exc
    return sizes


def audio_dir_usage_bytes(archive_root: str | os.PathLike[str]) -> int:
    """Measure audio usage; refuse unreadable roots instead of reporting zero."""
    return sum(_audio_file_sizes(archive_root).values())


class AudioUsageTracker:
    """One full scan per owned batch, updated by its downloads and reclaims.

    Writers sharing an artifact root must serialize their mutations. A new
    batch always measures again; failed downloads can rescan unknown leftovers.
    """

    def __init__(self, archive_root: str | os.PathLike[str]) -> None:
        self.root = os.path.abspath(archive_root)
        self._sizes = _audio_file_sizes(self.root)
        self._usage = sum(self._sizes.values())

    @property
    def usage_bytes(self) -> int:
        return self._usage

    def rescan(self) -> None:
        self._sizes = _audio_file_sizes(self.root)
        self._usage = sum(self._sizes.values())

    def refresh_entry(self, entry: Mapping[str, Any]) -> None:
        # Reuse the reclaim candidate policy, including legacy bare BVIDs.
        from .audio_reclaim import _candidate_paths

        audio_dir = _audio_directory(self.root)
        if audio_dir is None and self._sizes:
            raise AudioUsageError("audio directory disappeared during the batch")
        for relative in _candidate_paths(entry):
            path = os.path.abspath(os.path.join(self.root, relative))
            measured_dir = os.path.join(self.root, "audio")
            if os.path.commonpath((measured_dir, path)) != measured_dir:
                raise AudioUsageError("audio path outside the measured directory")
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                self._usage -= self._sizes.pop(path, 0)
                continue
            except OSError as exc:
                raise AudioUsageError("audio usage unavailable") from exc
            if not stat.S_ISREG(info.st_mode):
                raise AudioUsageError("audio candidate is not a regular file")
            self._usage += info.st_size - self._sizes.get(path, 0)
            self._sizes[path] = info.st_size


def parse_duration_s(duration_s: Any) -> int | None:
    """Positive duration in seconds, or None if missing/zero/unparseable."""
    if isinstance(duration_s, bool):
        return None
    try:
        seconds = int(duration_s)
    except (TypeError, ValueError, OverflowError):
        return None
    if seconds <= 0:
        return None
    return seconds


def estimate_audio_bytes(duration_s: Any) -> int:
    """Conservative on-disk audio size for one item (64 kbps ceiling).

    Missing/zero/unparseable duration has no known seconds and returns 0
    here. Callers that enforce a peak cap must use ``would_exceed_budget``,
    which fail-closes on unknown duration.
    """
    seconds = parse_duration_s(duration_s)
    if seconds is None:
        return 0
    return seconds * BYTES_PER_SECOND_CEILING


def would_exceed_budget(
    archive_root: str | os.PathLike[str],
    entry: Mapping[str, Any],
    max_bytes: int,
    *,
    usage_bytes: int | None = None,
) -> bool:
    """True if downloading this entry's audio would breach `max_bytes` peak.

    Unknown duration fail-closes against a finite cap so a livestream with
    empty ``duration_s`` cannot look like a 0-byte short. Pass ``usage_bytes``
    to reuse a snapshot instead of walking ``audio/`` again.
    """
    if max_bytes <= 0:
        return False  # unlimited
    if parse_duration_s(entry.get("duration_s")) is None:
        return True
    try:
        usage = audio_dir_usage_bytes(archive_root) if usage_bytes is None else usage_bytes
    except AudioUsageError:
        return True
    return usage + estimate_audio_bytes(entry.get("duration_s")) > max_bytes


def max_duration_exceeded(entry: Mapping[str, Any], max_duration_min: Any) -> bool:
    """True if the row is longer than the campaign duration cap (0 = off).

    Missing/zero/unparseable duration fail-closes when the cap is on so it
    cannot sneak through as a 0-second short.
    """
    try:
        cap_seconds = max(0, int(max_duration_min or 0)) * 60
    except (TypeError, ValueError):
        return False
    if cap_seconds <= 0:
        return False
    duration = parse_duration_s(entry.get("duration_s"))
    if duration is None:
        return True
    return duration > cap_seconds
