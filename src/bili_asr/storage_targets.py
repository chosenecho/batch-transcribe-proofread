"""Explicit directory target identity, separate from machine-independent catalog facts."""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from bili_asr.artifact_inventory import require_no_links
from bili_asr.artifact_root import ArtifactRoots

MARKER = ".bili-asr-storage-target.json"
_IDENTITY = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,255}\Z")


def require_safe_path(path: Path) -> None:
    require_no_links(path)
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("storage path cannot use a reparse point")


def unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def sync_directory(directory: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _root(root: Path, target_id: str, roots: ArtifactRoots) -> Path:
    if not isinstance(target_id, str) or not _IDENTITY.fullmatch(target_id) or target_id in {"local", "local-artifacts"}:
        raise ValueError("invalid or reserved external target identity")
    root = Path(os.path.abspath(root))
    require_safe_path(root)
    if not root.is_dir():
        raise ValueError("storage target must be an existing directory; a missing mount is never created")
    if any(root.is_relative_to(base) or base.is_relative_to(root) for base in roots.read_bases()):
        raise ValueError("storage target and archive/artifact roots must not contain each other")
    return root


def _marker(root: Path, target_id: str) -> None:
    path = root / MARKER
    require_safe_path(path)
    if not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("storage target identity marker is missing or invalid; bind the prepared target first")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("storage target marker must be a regular file")
        encoded = stream.read(65537)
        after = os.fstat(stream.fileno())
        if len(encoded) > 65536 or (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("storage target marker exceeded its bound or changed while reading")
    try:
        document = json.loads(encoded, object_pairs_hook=unique_json_object)
    except RecursionError as error:
        raise ValueError("storage marker JSON nesting exceeds the bound") from error
    if not isinstance(document, dict) or document != {"version": 1, "target_id": target_id} or type(document.get("version")) is not int:
        raise ValueError("storage target identity mismatch")


@dataclass(frozen=True)
class DirectoryTarget:
    root: Path
    target_id: str
    device: int
    inode: int

    def check(self) -> None:
        require_safe_path(self.root)
        current = self.root.stat()
        if not self.root.is_dir() or (current.st_dev, current.st_ino) != (self.device, self.inode):
            raise ValueError("storage target directory changed or is unavailable")
        _marker(self.root, self.target_id)
        after = self.root.stat()
        if (after.st_dev, after.st_ino) != (self.device, self.inode):
            raise ValueError("storage target changed while reading its identity")


def open_directory_target(root: Path, target_id: str, *, roots: ArtifactRoots) -> DirectoryTarget:
    root = _root(root, target_id, roots)
    info = root.stat()
    target = DirectoryTarget(root, target_id, info.st_dev, info.st_ino)
    target.check()
    return target


def bind_directory_target(root: Path, target_id: str, *, roots: ArtifactRoots) -> dict:
    """Attach an explicit identity to a prepared directory without overwriting it."""
    root = _root(root, target_id, roots)
    info = root.stat()
    marker = root / MARKER
    require_safe_path(marker)
    if marker.exists():
        open_directory_target(root, target_id, roots=roots)
        return {"target_id": target_id, "bound": True, "created": False}
    with tempfile.TemporaryDirectory(prefix=".artifact-target-", dir=root) as staging:
        temporary = Path(staging) / "identity.json"
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump({"version": 1, "target_id": target_id}, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        require_safe_path(root)
        if (root.stat().st_dev, root.stat().st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("storage target directory changed during binding")
        if os.name == "nt":
            os.rename(temporary, marker)
        else:
            os.link(temporary, marker)
        sync_directory(root)
    return {"target_id": target_id, "bound": True, "created": True}
