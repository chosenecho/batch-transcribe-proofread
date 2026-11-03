"""Explicit persistent cache namespace, without enabling compilation or deleting files."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import time
from contextlib import contextmanager
from uuid import uuid4
from bili_asr.persistence import file_lock, PersistenceError

from .deployment import cache_identity


class RuntimeCacheError(ValueError):
    """Cache ownership, compatibility or startup capacity could not be verified."""


def _private_path(path: Path) -> None:
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeCacheError("runtime cache cannot use symlinked or reparse paths")


@contextmanager
def _startup_lock(target: Path):
    path = target / ".startup.lock"
    _private_path(path)
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise RuntimeCacheError("invalid runtime cache lock")
    deadline = time.monotonic() + 10
    while True:
        lock = file_lock(path, blocking=False)
        try:
            lock.__enter__()
            break
        except PersistenceError as exc:
            if not isinstance(exc.__cause__, BlockingIOError):
                raise RuntimeCacheError("runtime cache lock unavailable") from None
            if time.monotonic() >= deadline:
                raise RuntimeCacheError("runtime cache initialization busy") from None
            time.sleep(0.02)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def configure_cache(root: str, identity: dict, *, max_bytes: int) -> dict:
    """Install/validate one compatibility manifest and check startup disk capacity."""
    target = Path(root)
    if not target.is_absolute() or type(max_bytes) is not int or max_bytes < 1:
        raise RuntimeCacheError("cache root must be absolute and startup byte limit positive")
    _private_path(target)
    target.mkdir(parents=True, exist_ok=True)
    with _startup_lock(target):
        return _configure_cache_locked(target, identity, max_bytes=max_bytes)


def _configure_cache_locked(target: Path, identity: dict, *, max_bytes: int) -> dict:
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    namespace = target / digest
    namespace.mkdir(parents=True, exist_ok=True)
    _private_path(namespace)
    manifest = namespace / "identity.json"
    if not manifest.exists():
        temporary = namespace / (".identity-" + uuid4().hex)
        try:
            with temporary.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, manifest)
            except FileExistsError:
                pass  # Another compatible process initialized it atomically.
        finally:
            temporary.unlink(missing_ok=True)
    _private_path(manifest)
    info = manifest.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != len(encoded):
        raise RuntimeCacheError("invalid runtime cache manifest")
    if manifest.read_bytes() != encoded:
        raise RuntimeCacheError("runtime cache manifest is corrupt or incompatible")
    total = count = 0
    # Check the whole managed root, including incompatible namespaces. This is
    # a startup admission limit; filesystem quotas bound writes during execution.
    for directory, dirs, files in os.walk(target, followlinks=False):
        count += len(dirs) + len(files)
        if count > 10_000:
            raise RuntimeCacheError("runtime cache inventory exceeds inspection limit")
        for name in (*dirs, *files):
            item = Path(directory) / name
            _private_path(item)
            try:
                item_info = item.stat()
            except FileNotFoundError:
                continue  # An active compiler may remove a temporary file.
            mode = item_info.st_mode
            if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
                raise RuntimeCacheError("runtime cache contains unsupported file types")
            if stat.S_ISREG(mode):
                total += item_info.st_size
        if total > max_bytes:
            raise RuntimeCacheError("runtime cache exceeds startup byte limit")
    if shutil.disk_usage(target).free < 1024 * 1024:
        raise RuntimeCacheError("runtime cache has insufficient free space")
    for name, directory in (("TORCHINDUCTOR_CACHE_DIR", "inductor"), ("TRITON_CACHE_DIR", "triton")):
        location = namespace / directory
        location.mkdir(exist_ok=True)
        _private_path(location)
        os.environ[name] = str(location)
    return {"schema_version": 1, "namespace_sha256": digest, "startup_bytes": total,
            "startup_limit_bytes": max_bytes, "compile_enabled": False}


def prepare_runtime_cache(config, root: str, *, max_bytes: int, binding: dict) -> dict:
    import torch

    device = torch.device(config.device)
    index = device.index if device.index is not None else torch.cuda.current_device()
    properties = torch.cuda.get_device_properties(index)
    hardware = {"name": str(properties.name), "memory_bytes": int(properties.total_memory),
                "architecture": str(getattr(properties, "gcnArchName", "")) or
                                list(torch.cuda.get_device_capability(index)),
                "cuda": getattr(torch.version, "cuda", None), "hip": getattr(torch.version, "hip", None)}
    # Runtime bindings carry verified file manifests. Unpinned local paths or
    # mutable hub refs are insufficient to reuse persistent executable caches.
    for name in ("model", "aligner"):
        entry = binding.get(name)
        if not isinstance(entry, dict) or not entry.get("manifest_sha256"):
            raise RuntimeCacheError("persistent cache requires verified runtime bindings for both checkpoints")
    identity = cache_identity(config, hardware=hardware)
    identity["checkpoint_binding"] = binding
    return configure_cache(root, identity, max_bytes=max_bytes)
