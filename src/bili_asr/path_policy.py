"""Shared confinement policy for persisted archive audio paths."""

from __future__ import annotations

import errno
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

AUDIO_EXTENSIONS = frozenset({".m4a", ".flac", ".webm", ".opus", ".ogg", ".mp4", ".wav", ".mp3", ".aac", ".mkv"})


def _audio_parts(declared_path: str | os.PathLike[str]) -> tuple[str, ...] | None:
    try:
        declared = Path(os.fspath(declared_path))
    except (TypeError, ValueError):
        return None
    if declared.is_absolute() or not declared.parts:
        return None
    if any(part in ("", ".", "..") for part in declared.parts):
        return None
    if declared.parts[0] != "audio" or len(declared.parts) != 2:
        return None
    if Path(declared.parts[-1]).suffix.lower() not in AUDIO_EXTENSIONS:
        return None
    return declared.parts


def open_audio_directory(
    archive_root: str | os.PathLike[str], *, create: bool = False
) -> int:
    """Open the archive audio directory without following either component."""
    if os.name != "posix" or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise OSError("descriptor-safe audio operations are unsupported")
    root_fd = os.open(
        os.fspath(archive_root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        try:
            return os.open(
                "audio", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir("audio", mode=0o755, dir_fd=root_fd)
            os.fsync(root_fd)
            return os.open(
                "audio", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
    finally:
        os.close(root_fd)


def _windows_audio_directory(
    archive_root: str | os.PathLike[str], *, create: bool = False
) -> Path:
    """Return the validated audio directory for platforms without dir_fds."""
    root = Path(os.path.abspath(os.fspath(archive_root)))
    if root.is_symlink() or not root.is_dir():
        raise OSError("archive root is not a directory")
    audio = root / "audio"
    if audio.exists() or audio.is_symlink():
        if audio.is_symlink() or not audio.is_dir():
            raise OSError("audio root is not a directory")
    elif create:
        audio.mkdir(mode=0o755)
    else:
        raise FileNotFoundError(os.fspath(audio))
    return audio


def _open_audio_file(archive_root: str | os.PathLike[str], parts: tuple[str, ...]) -> int:
    if os.name == "nt":
        path = _windows_audio_directory(archive_root) / parts[1]
        if path.is_symlink() or not path.is_file():
            raise OSError("audio file is not regular")
        return os.open(path, os.O_RDONLY)
    audio_fd = open_audio_directory(archive_root)
    try:
        fd = os.open(
            parts[1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=audio_fd
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("audio file is not regular")
        except Exception:
            os.close(fd)
            raise
        return fd
    finally:
        os.close(audio_fd)


def confined_audio_path(
    archive_root: str | os.PathLike[str],
    declared_path: str | os.PathLike[str] | None,
    *,
    require_exists: bool,
) -> Path | None:
    """Return a confined lexical audio path after no-follow validation."""
    if not isinstance(declared_path, (str, os.PathLike)):
        return None
    parts = _audio_parts(declared_path)
    if parts is None:
        return None
    try:
        root = Path(os.fspath(archive_root))
        if not root.is_absolute():
            root = Path(os.path.abspath(root))
        if os.name == "nt":
            audio = _windows_audio_directory(root, create=not require_exists)
            candidate = audio / parts[1]
            if candidate.exists() or candidate.is_symlink():
                if candidate.is_symlink() or not candidate.is_file():
                    return None
            elif require_exists:
                return None
            return root.joinpath(*parts)
        if require_exists:
            fd = _open_audio_file(root, parts)
            os.close(fd)
        else:
            audio_fd = open_audio_directory(root, create=True)
            try:
                try:
                    info = os.stat(parts[1], dir_fd=audio_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                        return None
            finally:
                os.close(audio_fd)
        return root.joinpath(*parts)
    except (OSError, RuntimeError, ValueError):
        return None


def descriptor_path(fd: int) -> str:
    """Return a same-process descriptor path or fail closed."""
    for prefix in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(prefix):
            return f"{prefix}/{fd}"
    raise OSError("descriptor-backed paths are unsupported")


@contextmanager
def confined_audio_file(
    archive_root: str | os.PathLike[str],
    declared_path: str | os.PathLike[str] | None,
    *,
    require_exists: bool = True,
) -> Iterator[str]:
    """Keep a confined audio descriptor open through the consuming operation."""
    if not isinstance(declared_path, (str, os.PathLike)):
        raise OSError("invalid audio path")
    parts = _audio_parts(declared_path)
    if parts is None or not require_exists:
        raise OSError("invalid audio path")
    if os.name == "nt":
        audio = _windows_audio_directory(archive_root)
        path = audio / parts[1]
        if path.is_symlink() or not path.is_file():
            raise OSError("audio file is not regular")
        yield os.fspath(path)
        return
    fd = _open_audio_file(archive_root, parts)
    try:
        yield descriptor_path(fd)
    finally:
        os.close(fd)


def unlink_confined_audio(
    archive_root: str | os.PathLike[str],
    declared_path: str | os.PathLike[str],
) -> bool:
    """Move, validate, and unlink one entry through its anchored parent fd."""
    parts = _audio_parts(declared_path)
    if parts is None:
        raise ValueError("invalid audio path")
    if os.name == "nt":
        audio = _windows_audio_directory(archive_root)
        target = audio / parts[1]
        if not target.exists() and not target.is_symlink():
            return False
        if target.is_symlink() or not target.is_file():
            raise ValueError("invalid audio path")
        quarantine = audio / f".audio-reclaim-{secrets.token_hex(16)}"
        os.replace(target, quarantine)
        try:
            if quarantine.is_symlink() or not quarantine.is_file():
                raise ValueError("invalid audio path")
            quarantine.unlink()
            return True
        except Exception:
            if quarantine.exists() and not target.exists():
                os.replace(quarantine, target)
            raise
    audio_fd = open_audio_directory(archive_root)
    quarantine_name = f".audio-reclaim-{secrets.token_hex(16)}"
    moved = False
    try:
        try:
            os.replace(
                parts[1], quarantine_name,
                src_dir_fd=audio_fd, dst_dir_fd=audio_fd,
            )
            moved = True
        except FileNotFoundError:
            return False

        try:
            fd = os.open(
                quarantine_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=audio_fd
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ValueError("invalid audio path") from exc
            raise
        try:
            info = os.fstat(fd)
            current = os.stat(
                quarantine_name, dir_fd=audio_fd, follow_symlinks=False
            )
            if not stat.S_ISREG(info.st_mode) or (
                info.st_dev, info.st_ino
            ) != (current.st_dev, current.st_ino):
                raise ValueError("invalid audio path")
            os.unlink(quarantine_name, dir_fd=audio_fd)
            moved = False
            os.fsync(audio_fd)
            return True
        finally:
            os.close(fd)
    except (OSError, ValueError):
        if moved:
            try:
                os.stat(parts[1], dir_fd=audio_fd, follow_symlinks=False)
            except FileNotFoundError:
                os.replace(
                    quarantine_name, parts[1],
                    src_dir_fd=audio_fd, dst_dir_fd=audio_fd,
                )
                moved = False
                os.fsync(audio_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(audio_fd)
