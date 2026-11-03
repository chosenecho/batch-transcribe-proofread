"""Portable artifact inventory, confinement and bounded hashing contracts.

Snapshot and migration use the same physical inventory policy; neither owns
these rules. Recorded relative paths and selected read-base order are retained.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
import os
from pathlib import Path
import re
import stat
from typing import BinaryIO
import unicodedata

ARTIFACT_DIRECTORIES = frozenset({"audio", "transcripts", "documents", "subtitles", "publications"})
_CHUNK_SIZE = 1024 * 1024
_RESERVED_PATTERN = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9\u00b9\u00b2\u00b3]|LPT[1-9\u00b9\u00b2\u00b3])\Z", re.IGNORECASE)
_TEMP_SUFFIXES = frozenset({".tmp", ".temp", ".partial", ".part", ".download"})


class ArtifactInventoryError(ValueError):
    """An artifact inventory cannot preserve portable, trustworthy bytes."""


def require_no_links(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink() or (
            hasattr(component, "is_junction") and component.is_junction()
        ):
            raise ArtifactInventoryError(f"snapshot paths cannot use symlinks or junctions: {component}")


def portable_artifact_parts(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not path or "\\" in path:
        raise ArtifactInventoryError(f"unsafe snapshot path: {path!r}")
    parts = tuple(path.split("/"))
    for part in parts:
        if (not part or part in {".", ".."} or part.endswith((".", " "))
                or any(ord(char) < 32 or ord(char) == 127 or char in '<>:"|?*' for char in part)
                or len(part.encode("utf-8")) > 255
                or _RESERVED_PATTERN.fullmatch(part.split(".", 1)[0].rstrip(" "))):
            raise ArtifactInventoryError(f"unsafe or non-portable snapshot path: {path!r}")
    if path != "archive.db" and (len(parts) < 2 or parts[0] not in ARTIFACT_DIRECTORIES):
        raise ArtifactInventoryError(f"unsupported snapshot artifact path: {path!r}")
    return parts


def _canonical(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def check_artifact_collisions(paths: list[str]) -> None:
    files: set[str] = set()
    prefixes: dict[str, str] = {}
    for path in paths:
        parts = portable_artifact_parts(path)
        canonical = _canonical(path)
        if canonical in files:
            raise ArtifactInventoryError(f"duplicate or colliding snapshot path: {path}")
        files.add(canonical)
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            key = _canonical(prefix)
            previous = prefixes.setdefault(key, prefix)
            if previous != prefix:
                raise ArtifactInventoryError(f"case or Unicode collision: {previous!r} and {prefix!r}")
    for path in paths:
        parts = path.split("/")
        if any(_canonical("/".join(parts[:length])) in files
               for length in range(1, len(parts))):
            raise ArtifactInventoryError(f"snapshot file is also a parent directory: {path}")


def require_regular_file(path: Path) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ArtifactInventoryError(f"snapshot source is not a regular file: {path}")
    require_no_links(path)
    return info


def is_temporary_artifact(name: str) -> bool:
    return (name.startswith((".audio-stage-", ".workflow-audio-", ".archive-bundle-stage-", ".bili-asr-probe-", ".render-", ".import-", ".manuscript-"))
            or name == ".tmp"
            or Path(name).suffix.lower() in _TEMP_SUFFIXES)


def collect_artifacts(bases: tuple[Path, ...]) -> dict[str, Path]:
    found: dict[str, Path] = {}

    def unreadable(error: OSError) -> None:
        raise ArtifactInventoryError(f"artifact directory cannot be fully read: {error.filename}") from error

    for base in bases:
        require_no_links(base)
        if not base.is_dir():
            raise ArtifactInventoryError(f"archive or artifact root is not a directory: {base}")
        for name in sorted(ARTIFACT_DIRECTORIES):
            directory = base / name
            if not directory.exists() and not directory.is_symlink():
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise ArtifactInventoryError(f"artifact directory is unsafe: {directory}")
            for current, directories, files in os.walk(directory, followlinks=False, onerror=unreadable):
                current_path = Path(current)
                for entry in sorted(directories + files):
                    path = current_path / entry
                    if is_temporary_artifact(entry):
                        raise ArtifactInventoryError(f"unfinished temporary artifact must be resolved before saving: {path}")
                    require_no_links(path)
                for entry in sorted(files):
                    path = current_path / entry
                    require_regular_file(path)
                    key = path.relative_to(base).as_posix()
                    portable_artifact_parts(key)
                    found.setdefault(key, path)
    check_artifact_collisions(list(found))
    return found


def stream_hash(source: BinaryIO, destination: BinaryIO | None = None, *,
                checkpoint: Callable[[], None] | None = None) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while True:
        if checkpoint is not None:
            checkpoint()
        chunk = source.read(_CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
        if destination is not None:
            remaining = memoryview(chunk)
            while remaining:
                written = destination.write(remaining)
                if not written:
                    raise OSError("cannot complete artifact copy")
                remaining = remaining[written:]
    return size, digest.hexdigest()

