"""Append test diagnostics without following a log or parent directory link."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
from typing import Iterator, TextIO


_USE_DIR_FD = (
    os.name == "posix"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
)


def _regular(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and not _reparse(info)


def _reparse(info: os.stat_result) -> bool:
    # Junctions are directory reparse points on Windows, even when they do
    # not report S_ISLNK. Refuse both forms before using a lexical path.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _open_at(path: Path) -> int:
    """Walk every parent through held descriptors; create only the log dir."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = os.open(path.anchor, flags)
    try:
        directories = path.parent.parts[1:]
        for index, component in enumerate(directories):
            try:
                child_fd = os.open(component, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                if index != len(directories) - 1:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    # A competing writer can create the directory; the open
                    # below still refuses a link substituted in its place.
                    pass
                child_fd = os.open(component, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        fd = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
            | getattr(os, "O_NONBLOCK", 0),
            0o600,
            dir_fd=parent_fd,
        )
        try:
            if not _regular(os.fstat(fd)):
                raise OSError("forensic log is not a regular file")
        except BaseException:
            os.close(fd)
            raise
        return fd
    finally:
        os.close(parent_fd)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _open_checked(path: Path) -> int:
    """Portable link refusal with descriptor and parent identity checks.

    Windows lacks dir_fd/O_NOFOLLOW. Check all lexical parents and reparse
    points before opening, and verify them again before writing any facts.
    """
    parents: list[tuple[Path, os.stat_result]] = []
    current = Path(path.anchor)
    components = path.parent.parts[1:]
    for index, component in enumerate(components):
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            if index != len(components) - 1:
                raise
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = current.lstat()
        if _reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise OSError("forensic log parent is not a plain directory")
        parents.append((current, info))
    try:
        expected = path.lstat()
    except FileNotFoundError:
        expected = None
    if expected is not None and not _regular(expected):
        raise OSError("forensic log is not a regular file")
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if expected is None:
        # Exclusive creation also refuses a link appearing after lstat.
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        actual = os.fstat(fd)
        visible = path.lstat()
        if not _regular(actual) or not _regular(visible):
            raise OSError("forensic log is not a regular file")
        if _identity(actual) != _identity(visible):
            raise OSError("forensic log changed while opening")
        if expected is not None and _identity(expected) != _identity(actual):
            raise OSError("forensic log changed while opening")
        for parent, prior in parents:
            after = parent.lstat()
            if _reparse(after) or _identity(prior) != _identity(after):
                raise OSError("forensic log parent changed while opening")
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def open_forensic_log(path: str | Path) -> Iterator[TextIO]:
    """Open a regular UTF-8 log for append, retaining existing diagnostics."""
    absolute = Path(os.path.abspath(path))
    fd = _open_at(absolute) if _USE_DIR_FD else _open_checked(absolute)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        yield handle
