"""Confined isolation of one journaled audio copy during exclusive maintenance."""
from __future__ import annotations

import hashlib
import os
import stat
from contextlib import contextmanager, nullcontext
from pathlib import Path

from bili_asr.artifact_inventory import portable_artifact_parts
from bili_asr.storage_targets import require_safe_path, sync_directory


@contextmanager
def _artifact_directory(root: Path, parent: tuple[str, ...]):
    directory = root.joinpath(*parent)
    require_safe_path(directory)
    descriptor = None
    if os.name == "posix":
        descriptor = os.open(directory.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in directory.parts[1:]:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        except BaseException:
            os.close(descriptor)
            raise
    try:
        yield directory, descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _info(directory: Path, descriptor: int | None, name: str):
    try:
        info = os.stat(name if descriptor is not None else directory / name,
                       dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("release entry must be a regular file without reparse points")
    return info


def _verify(directory: Path, descriptor: int | None, name: str, identity: str, generation: dict):
    info = _info(directory, descriptor, name)
    if info is None:
        raise ValueError("journaled release copy is missing")
    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != tuple(
        generation[key] for key in ("device", "inode", "size_bytes", "mtime_ns")
    ):
        raise ValueError("release source generation changed")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    file_descriptor = os.open(name if descriptor is not None else directory / name, flags, dir_fd=descriptor)
    with os.fdopen(file_descriptor, "rb") as incoming:
        opened = os.fstat(incoming.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
        ):
            raise ValueError("release source changed while opening")
        digest = hashlib.sha256()
        from bili_asr.services.artifact_io import observe_io
        for block in iter(lambda: incoming.read(1024 * 1024), b""):
            observe_io(len(block))
            digest.update(block)
        after = os.fstat(incoming.fileno())
        if digest.hexdigest() != identity or (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
            opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns
        ):
            raise ValueError("release source bytes changed")
        return after


def _unchanged(directory: Path, descriptor: int | None, name: str, verified):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    opened = os.open(name if descriptor is not None else directory / name, flags, dir_fd=descriptor)
    try:
        current = os.fstat(opened)
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (
            verified.st_dev, verified.st_ino, verified.st_size, verified.st_mtime_ns, verified.st_ctime_ns
        ):
            raise ValueError("release source generation changed before deletion")
    finally:
        os.close(opened)


def release_copy(root: Path, source_key: str, quarantine_key: str, object_id: str, generation: dict,
                 *, allow_delete: bool, isolated=None, before_delete=None, deletion_guard=None) -> dict:
    """Replay a persisted intent; never remove a replaced source or quarantine.

    The quarantine is on the same anchored audio directory. On POSIX, link plus
    verified unlink supplies a no-clobber move; a crash leaving both names is
    distinguishable and replayable. Windows rename already refuses replacement.
    """
    source, quarantine = portable_artifact_parts(source_key), portable_artifact_parts(quarantine_key)
    if len(source) < 2 or source[:-1] != quarantine[:-1] or source == quarantine or not quarantine[-1].startswith(".artifact-release-"):
        raise ValueError("release intent must name two distinct confined artifact entries")
    with _artifact_directory(root, source[:-1]) as (directory, descriptor):
        source, quarantine = (source[0], source[-1]), (quarantine[0], quarantine[-1])
        options = {"dir_fd": descriptor} if descriptor is not None else {}
        src = _info(directory, descriptor, source[1])
        held = _info(directory, descriptor, quarantine[1])
        if src is not None and (src.st_dev, src.st_ino, src.st_size, src.st_mtime_ns) != tuple(
            generation[key] for key in ("device", "inode", "size_bytes", "mtime_ns")
        ):
            raise ValueError("release source generation changed before isolation")
        if src is None and held is None:
            return {"state": "released", "released_bytes": 0, "observation": "already_absent"}
        if not allow_delete:
            if held is not None:
                _verify(directory, descriptor, quarantine[1], object_id, generation)
            if src is not None:
                _verify(directory, descriptor, source[1], object_id, generation)
            if held is not None:
                if src is None:
                    if descriptor is None:
                        os.rename(directory / quarantine[1], directory / source[1])
                    else:
                        os.link(quarantine[1], source[1], src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False)
                        os.unlink(quarantine[1], **options)
                else:
                    os.unlink(quarantine[1] if descriptor is not None else directory / quarantine[1], **options)
                sync_directory(directory)
            return {"state": "cancelled", "released_bytes": 0, "observation": "retention_guard"}
        if held is None:
            if descriptor is None:
                os.rename(directory / source[1], directory / quarantine[1])
            else:
                os.link(source[1], quarantine[1], src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False)
            sync_directory(directory)
        verified_quarantine = _verify(directory, descriptor, quarantine[1], object_id, generation)
        # POSIX isolation makes two names for the same inode. One payload hash
        # plus same-inode descriptor checks verifies both without rereading it.
        source_present = descriptor is not None and _info(directory, descriptor, source[1]) is not None
        # A failed journal update leaves the isolated name for reconcile.
        if isolated is not None:
            isolated()
        # Filesystems may coarsen ctime (and callers can restore mtime). A
        # journal callback is an observable interruption boundary, so metadata
        # alone cannot prove the bytes still match after control returns.
        verified_quarantine = _verify(directory, descriptor, quarantine[1], object_id, generation)
        if before_delete is not None:
            before_delete()
        with deletion_guard() if deletion_guard is not None else nullcontext():
            _unchanged(directory, descriptor, quarantine[1], verified_quarantine)
            if source_present:
                _unchanged(directory, descriptor, source[1], verified_quarantine)
                os.unlink(source[1], **options)
                os.fsync(descriptor)
            remaining = _info(directory, descriptor, quarantine[1])
            if remaining is None or (remaining.st_dev, remaining.st_ino, remaining.st_size, remaining.st_mtime_ns) != tuple(generation[key] for key in ("device", "inode", "size_bytes", "mtime_ns")):
                raise ValueError("isolated copy changed before deletion")
            os.unlink(quarantine[1] if descriptor is not None else directory / quarantine[1], **options)
            sync_directory(directory)
        return {"state": "released", "released_bytes": generation["size_bytes"] if remaining.st_nlink == 1 else 0, "observation": "deleted_verified_copy"}
