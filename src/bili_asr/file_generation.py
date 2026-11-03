"""Descriptor identity including a content change clock on Windows and POSIX."""
from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO


@contextmanager
def change_watch(path: Path) -> Iterator[Callable[[], bool]]:
    """Observe Linux writes even when same-tick ABA preserves the stat tuple."""
    if sys.platform != "linux":
        yield lambda: False
        return
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    initialize = libc.inotify_init1
    initialize.argtypes, initialize.restype = [ctypes.c_int], ctypes.c_int
    add = libc.inotify_add_watch
    add.argtypes, add.restype = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32], ctypes.c_int
    descriptor = initialize(os.O_CLOEXEC | os.O_NONBLOCK)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "cannot establish audio write observation")
    try:
        # MODIFY, ATTRIB, DELETE_SELF and MOVE_SELF; overflow/ignored also fail closed.
        if add(descriptor, os.fsencode(path), 0x2 | 0x4 | 0x400 | 0x800) < 0:
            raise OSError(ctypes.get_errno(), "cannot observe private audio input")
        changed = False

        def observed() -> bool:
            nonlocal changed
            try:
                changed = bool(os.read(descriptor, 4096)) or changed
            except BlockingIOError:
                pass
            return changed

        yield observed
    finally:
        os.close(descriptor)


def stream_generation(source: BinaryIO) -> tuple[int, ...]:
    info = os.fstat(source.fileno())
    value = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if os.name == "nt":
        # Python's Windows st_ctime is creation time. NTFS ChangeTime also
        # changes after content/mtime are restored, so ABA writes remain visible.
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class FileBasicInfo(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in
                        ("CreationTime", "LastAccessTime", "LastWriteTime", "ChangeTime")] + [
                        ("FileAttributes", wintypes.DWORD)]
        basic = FileBasicInfo()
        read = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
        read.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        read.restype = wintypes.BOOL
        if not read(msvcrt.get_osfhandle(source.fileno()), 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            raise ctypes.WinError(ctypes.get_last_error())
        value += (basic.ChangeTime,)
    return value
