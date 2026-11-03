"""Strict filesystem boundaries for immutable manuscript artifacts."""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import tempfile
from typing import Iterable, Iterator


def _reject_link(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise ValueError(f"manuscript-path: link or reparse point is forbidden: {path}")


def validate_root(root: Path) -> Path:
    """Check the lexical root and every ancestor before any resolution."""
    path = Path(root).absolute()
    for ancestor in (*reversed(path.parents), path):
        _reject_link(ancestor)
        if ancestor.exists() and not ancestor.is_dir():
            raise ValueError(f"manuscript-path: root ancestor is not a directory: {ancestor}")
    return path


def secure_path(root: Path, relative_path: str, *, create_parents: bool = False) -> Path:
    """Accept canonical relative POSIX paths under an unlinked root."""
    if not isinstance(relative_path, str) or not relative_path or "\\" in relative_path:
        raise ValueError("manuscript-path: artifact path must be relative POSIX text")
    relative = PurePosixPath(relative_path)
    if (relative.is_absolute() or PureWindowsPath(relative_path).drive
            or ":" in relative_path or "\x00" in relative_path
            or any(p in {"", ".", ".."} for p in relative_path.split("/"))
            or relative.as_posix() != relative_path):
        raise ValueError("manuscript-path: noncanonical or escaping artifact path")
    base = validate_root(root)
    target = base.joinpath(*relative.parts)
    for path in (*reversed(target.parent.parents), target.parent, target):
        if not path.is_relative_to(base):
            continue
        _reject_link(path)
        if path != target and path.exists() and not path.is_dir():
            raise ValueError(f"manuscript-path: artifact parent is not a directory: {path}")
    if target.exists() and not target.is_file():
        raise ValueError("manuscript-path: artifact must be a regular file")
    if create_parents:
        target.parent.mkdir(parents=True, exist_ok=True)
        secure_path(base, relative_path)
    return target


def read_artifact(relative_path: str, sha256: str, roots: Iterable[Path], *, max_bytes: int | None = None) -> bytes:
    """Read the first present exact artifact, rejecting corruption or links."""
    for root in roots:
        target = secure_path(Path(root), relative_path)
        if not target.exists():
            continue
        if max_bytes is None:
            data = target.read_bytes()
        else:
            with target.open("rb") as stream:
                data = stream.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("manuscript-integrity: artifact exceeds reader byte limit")
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError("manuscript-integrity: artifact hash mismatch")
        try:
            data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("manuscript-integrity: artifact is not UTF-8") from exc
        return data
    raise ValueError(f"manuscript-integrity: artifact is missing: {relative_path}")


@dataclass(frozen=True)
class StagedArtifact:
    root: Path
    relative_path: str
    target: Path
    temporary: Path | None
    content: bytes

    def install(self) -> Path:
        """Install already durable bytes under the caller's ownership fence."""
        secure_path(self.root, self.relative_path)
        if self.temporary is None:
            if self.target.read_bytes() != self.content:
                raise ValueError("manuscript-integrity: existing artifact has different bytes")
            return self.target
        try:
            os.link(self.temporary, self.target)
        except FileExistsError:
            if self.target.read_bytes() != self.content:
                raise ValueError("manuscript-integrity: concurrent artifact has different bytes")
        if os.name != "nt":
            directory_fd = os.open(self.target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return self.target


@contextmanager
def stage_artifact(root: Path, relative_path: str, data: bytes, *, temporary_root: Path | None = None) -> Iterator[StagedArtifact]:
    """Prepare and fsync outside SQLite transactions; always clean staging."""
    target = secure_path(root, relative_path, create_parents=True)
    if target.exists():
        if target.read_bytes() != data:
            raise ValueError("manuscript-integrity: existing artifact has different bytes")
        yield StagedArtifact(root, relative_path, target, None, data)
        return
    temporary_directory = validate_root(temporary_root) if temporary_root is not None else target.parent
    fd, temporary = tempfile.mkstemp(prefix=".manuscript-", dir=temporary_directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        yield StagedArtifact(root, relative_path, target, Path(temporary), data)
    finally:
        os.unlink(temporary)


def atomic_write_artifact(root: Path, relative_path: str, data: bytes) -> Path:
    """Durably install bytes once; retries never overwrite conflicting content."""
    with stage_artifact(root, relative_path, data) as staged:
        return staged.install()
