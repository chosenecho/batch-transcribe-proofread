"""Small immutable directories with an inventory, shared by delivery and operations."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from bili_asr.canonical_json import digest
from bili_asr.services.retry_maintenance_io import maintenance_lock, read_json


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def bundle_file(root: Path, name: str) -> Path:
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or PurePosixPath(name).is_absolute()):
        raise ValueError("bundle paths must be portable relative file names")
    current = root
    for part in name.split("/"):
        stem = part.split(".")[0].upper()
        if (part.endswith((" ", ".")) or any(ord(char) < 32 or char in '<>"|?*' for char in part)
                or stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}):
            raise ValueError("bundle path is not portable across operating systems")
        current = current / part
        if current.is_symlink():
            raise ValueError("bundle files must not use symbolic links")
    return current


def file_entry(root: Path, name: str) -> dict:
    path = bundle_file(root, name)
    if not path.is_file():
        raise ValueError("bundle file is missing: " + name)
    hasher, size = hashlib.sha256(), 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(block)
            size += len(block)
    return {"path": name, "size": size, "sha256": hasher.hexdigest()}


def write_file(root: Path, name: str, value: bytes) -> None:
    path = bundle_file(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def copy_file(root: Path, name: str, source: Path) -> None:
    """Stream a caller-selected attachment rather than buffering its whole file."""
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise ValueError("bundle source must be a regular file")
    before = source.stat()
    path = bundle_file(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, path.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    after = source.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("bundle source changed during copy")


def seal_bundle(root: Path, *, format: str, files: list[str], **metadata) -> dict:
    manifest = {"format": format, "created_at": datetime.now(UTC).isoformat(),
                **metadata, "files": [file_entry(root, name) for name in sorted(files)]}
    manifest["bundle_id"] = digest(manifest)
    write_file(root, "manifest.json", json_bytes(manifest))
    return manifest


def verify_bundle(root: Path, *, format: str | None = None) -> dict:
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("bundle must be an existing directory without links")
    path = bundle_file(root, "manifest.json")
    if not path.is_file() or path.stat().st_size > 16 * 1024**2:
        raise ValueError("bundle manifest is missing or exceeds 16 MiB")
    manifest = read_json(path)
    if format is not None and manifest.get("format") != format:
        raise ValueError("unsupported bundle format")
    if manifest.get("bundle_id") != digest({k: v for k, v in manifest.items() if k != "bundle_id"}):
        raise ValueError("bundle manifest identity mismatch")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("bundle inventory is empty")
    names, portable_names = set(), set()
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}
                or not isinstance(entry["path"], str) or not isinstance(entry["sha256"], str)
                or type(entry["size"]) is not int or entry["size"] < 0):
            raise ValueError("invalid bundle inventory entry")
        name = entry["path"]
        portable_name = unicodedata.normalize("NFC", name).casefold()
        if portable_name == "manifest.json" or portable_name in portable_names:
            raise ValueError("bundle inventory contains duplicate/reserved paths")
        if file_entry(root, name) != entry:
            raise ValueError("bundle file identity mismatch: " + name)
        names.add(name)
        portable_names.add(portable_name)
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("bundle contains symbolic links")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != names | {"manifest.json"}:
        raise ValueError("bundle inventory differs from its files")
    return manifest


@contextmanager
def new_directory(output: Path, *, validator=None):
    """Stage a new directory; cooperating writers never replace a prior bundle."""
    output = Path(output).absolute()
    for parent in (output, *output.parents):
        if parent.is_symlink():
            raise ValueError("bundle destination must not use symbolic links")
    output.parent.mkdir(parents=True, exist_ok=True)
    with maintenance_lock(output.parent / ("." + output.name + ".bundle.lock")):
        if output.exists():
            raise ValueError("bundle destination already exists; choose a new snapshot name")
        with tempfile.TemporaryDirectory(prefix="." + output.name + "-", dir=output.parent) as temporary:
            stage = Path(temporary) / "bundle"
            stage.mkdir()
            yield stage
            if validator is not None:
                validator(stage)
            if output.exists():
                raise ValueError("bundle destination was created during staging")
            if os.name != "nt":
                for directory in sorted((p for p in stage.rglob("*") if p.is_dir()), reverse=True) + [stage]:
                    descriptor = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            for attempt in range(10):
                try:
                    if output.exists():
                        raise ValueError("bundle destination appeared during publication")
                    stage.rename(output)
                    break
                except PermissionError:
                    if os.name != "nt" or attempt == 9:
                        raise
                    time.sleep(0.05 * (attempt + 1))
            if os.name != "nt":
                descriptor = os.open(output.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)


@contextmanager
def new_bundle(output: Path):
    with new_directory(output, validator=verify_bundle) as stage:
        yield stage
