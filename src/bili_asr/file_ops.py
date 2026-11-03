"""Filesystem safety, durable writes and process locks without application dependencies.

The lock filename and marker are retained for compatibility with installed locks.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

EXPORT_LOCK_MARKER = b"bili-asr-export-lock-v1\n"
_EXPORT_LOCKS = threading.local()


class FileOperationError(ValueError):
    """An unsafe path, damaged ownership marker or unavailable process lock."""


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def checked_path(path: Path) -> Path:
    """Check lexical ancestors before resolution, including Windows junctions."""
    absolute = Path(os.path.abspath(path))
    for current in (*reversed(absolute.parents), absolute):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise FileOperationError(f"export path contains a link or reparse point: {current}")
    return absolute


def fsync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def install_prepared_file(source: Path, destination: Path) -> None:
    """Install already-fsynced bytes, then persist every affected directory.

    The caller owns its result/lease fence and must fsync the prepared file
    before entering a database write transaction. Failure after replacement
    leaves the destination available for explicit verification and retry.
    Windows has no directory fsync contract here; fsync_directory deliberately
    keeps that platform policy rather than claiming a power-loss guarantee.
    """
    source, destination = checked_path(source), checked_path(destination)
    if source != destination:
        os.replace(source, destination)
    fsync_directory(destination.parent)
    if source.parent != destination.parent:
        fsync_directory(source.parent)


def write_new_file(path: Path, content: bytes) -> None:
    checked_path(path)
    stream = path.open("xb")
    try:
        with stream:
            _write_all(stream, content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # The exclusive create succeeded, so this partial file belongs to us.
        # Close it before unlinking to support Windows file sharing rules.
        path.unlink(missing_ok=True)
        raise


def _write_all(stream: BinaryIO, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        count = stream.write(remaining)
        if not count:
            raise OSError("cannot complete file write")
        remaining = remaining[count:]


def atomic_replace_file(path: Path, content: bytes, *, prefix: str | None = None,
                        sync_parent: Callable[[Path], None] | None = None) -> None:
    """Durably write private bytes, then replace one file within its directory.

    The caller owns serialization and any surrounding process lock. A failure
    before replacement preserves the destination; a directory sync failure
    after replacement propagates and leaves the new bytes in place.
    """
    path = checked_path(path)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=prefix or f".{path.name}.")
    temporary = Path(name)
    try:
        try:
            stream = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            _write_all(stream, content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        (sync_parent or fsync_directory)(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _open_initialized_lock(path: Path, flags: int, marker: bytes) -> int:
    try:
        return os.open(path, flags)
    except FileNotFoundError:
        pass
    # Publish a complete inode without overwriting an existing lock. Writing
    # directly to the public name lets a Windows contender lock the empty file
    # and prohibit its creator from writing the marker.
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.init-")
    temporary = Path(name)
    try:
        try:
            written = 0
            while written < len(marker):
                count = os.write(descriptor, marker[written:])
                if count <= 0:
                    raise OSError("cannot initialize export lock")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, path)
        except FileExistsError:
            # Another initializer published first; everyone locks its inode.
            pass
    finally:
        temporary.unlink()
    return os.open(path, flags)


@contextmanager
def exclusive_lock(output: Path, *, blocking: bool = False, existing_only: bool = False) -> Iterator[None]:
    """Hold the product export lock; probes never initialize a missing lock.

    Acquisitions are deliberately non-reentrant, including on systems whose
    kernel lock API permits another descriptor in the same thread to succeed.
    The stable lock inode is retained when the lease ends.
    """
    path = output.parent / f".{output.name}.export.lock"
    checked_path(path)
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    marker = EXPORT_LOCK_MARKER
    held = getattr(_EXPORT_LOCKS, "held", None)
    if held is None:
        held = _EXPORT_LOCKS.held = set()
    identity = os.path.normcase(os.fspath(path.absolute()))
    if identity in held:
        raise FileOperationError("another export holds the output lock")
    descriptor = os.open(path, flags) if existing_only else _open_initialized_lock(path, flags, marker)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise FileOperationError("export lock is not a regular file")
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(descriptor, mode, 1)
            else:
                import fcntl

                flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
                fcntl.flock(descriptor, flags)
        except OSError as exc:
            raise FileOperationError("another export holds the output lock") from exc
        # Windows byte-range locks also prohibit reads. Acquire the lock before
        # inspecting its marker so blocking callers can wait for the owner.
        os.lseek(descriptor, 0, os.SEEK_SET)
        actual_marker = os.read(descriptor, len(marker) + 1)
        if actual_marker != marker:
            raise FileOperationError("export lock file is unmanaged or damaged")
        opened, current = os.fstat(descriptor), path.lstat()
        if (opened.st_nlink != 1 or current.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise FileOperationError("export lock identity changed or has multiple links")
        held.add(identity)
        try:
            yield
        finally:
            held.remove(identity)
    finally:
        os.close(descriptor)
