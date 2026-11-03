"""Shared writer access and exclusive snapshot access to a local archive."""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class ArchiveAccessError(ValueError):
    """An archive cannot participate in maintenance coordination."""


class ArchiveBusyError(ArchiveAccessError):
    """A writer or snapshot operation already owns conflicting access."""


_HELD = threading.local()


@dataclass
class _AccessLease:
    exclusive: bool
    descriptor: int
    unlock: Callable[[], None]
    users: int = 1


def _release_access(held: dict[str, _AccessLease], key: str, lease: _AccessLease) -> None:
    lease.users -= 1
    if lease.users:
        return
    held.pop(key, None)
    try:
        lease.unlock()
    finally:
        os.close(lease.descriptor)


def _windows_lock(fd: int, *, exclusive: bool):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(Overlapped),
    ]
    kernel.LockFileEx.restype = wintypes.BOOL
    kernel.UnlockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    kernel.UnlockFileEx.restype = wintypes.BOOL
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
    overlapped = Overlapped()
    # FAIL_IMMEDIATELY plus optional EXCLUSIVE_LOCK; ordinary writers share access.
    if not kernel.LockFileEx(handle, 1 | (2 if exclusive else 0), 0, 1, 0, ctypes.byref(overlapped)):
        error = ctypes.get_last_error()
        if error in (32, 33):
            raise ArchiveBusyError("archive_busy: stop archive writers before saving or restoring")
        raise ctypes.WinError(error)

    def unlock() -> None:
        if not kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
            raise ctypes.WinError(ctypes.get_last_error())

    return unlock


@contextmanager
def archive_access(
    root: str | os.PathLike[str], *, exclusive: bool = False, create_root: bool = True,
    reentrant: bool = True,
) -> Iterator[None]:
    """Refuse conflicting access without serializing independent workflow workers.

    The stable lock lives beside the root so restoring a staging directory can
    atomically publish an archive without moving or replacing the lock inode.
    ``create_root`` permits a missing root and creates only its parent directory.
    Programmatic writers must hold shared access across their entire operation.
    ``reentrant=False`` is an activity probe: an access lease already held by
    this thread is a conflict even if an ordinary nested access could reuse it.
    """
    requested = Path(root)
    if requested.is_symlink():
        raise ArchiveAccessError("archive root must not be a symlink")
    archive = requested.resolve()
    if archive == archive.parent:
        raise ArchiveAccessError("filesystem root cannot be an archive root")
    if archive.exists() and not archive.is_dir():
        raise ArchiveAccessError("archive root is not a directory")
    if not create_root and not archive.is_dir():
        raise ArchiveAccessError("archive root does not exist")
    if create_root:
        archive.parent.mkdir(parents=True, exist_ok=True)
    lock = archive.parent / f".{archive.name}.archive-maintenance.lock"
    key = os.path.normcase(os.fspath(lock))
    held = getattr(_HELD, "locks", None)
    if held is None:
        held = {}
        _HELD.locks = held
    if key in held:
        lease = held[key]
        if not reentrant:
            raise ArchiveBusyError("archive_busy: non-reentrant probe found active access")
        if exclusive and not lease.exclusive:
            raise ArchiveBusyError("archive_busy: cannot upgrade active writer access")
        # Independently opened connections may close in either order. Keep the
        # kernel lease until every nested user releases its own access.
        lease.users += 1
        try:
            yield
        finally:
            _release_access(held, key, lease)
        return

    fd = None
    unlock = None
    try:
        if lock.is_symlink():
            raise ArchiveAccessError("archive maintenance lock must not be a symlink")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ArchiveAccessError("archive maintenance lock is not a regular file")
        if os.name == "nt":
            unlock = _windows_lock(fd, exclusive=exclusive)
        else:
            import fcntl

            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ArchiveBusyError(
                    "archive_busy: stop archive writers before saving or restoring"
                ) from exc
            def unlock() -> None:
                fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        raise ArchiveAccessError("archive maintenance lock cannot be acquired") from exc
    except Exception:
        if fd is not None:
            os.close(fd)
        raise

    lease = _AccessLease(exclusive, fd, unlock)
    held[key] = lease
    try:
        yield
    finally:
        _release_access(held, key, lease)
