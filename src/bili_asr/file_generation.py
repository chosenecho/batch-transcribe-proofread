"""Descriptor identity including a content change clock on Windows and POSIX."""
from __future__ import annotations

import os
from typing import BinaryIO


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
