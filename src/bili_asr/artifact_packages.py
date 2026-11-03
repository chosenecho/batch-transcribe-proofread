"""Immutable, bounded artifact packages; source removal belongs to orchestration.

Packages are written on the prepared target, reread there, then installed without
overwriting another package. A restore reads only its selected object. Neither
operation changes a source artifact or records production/database state.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import struct
import tempfile
import zipfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from bili_asr.artifact_inventory import (
    check_artifact_collisions,
    portable_artifact_parts,
    require_no_links,
)
from bili_asr.file_generation import stream_generation

_FORMAT = "bili-asr-artifact-package"
_MANIFEST_NAME = "package.json"
_MANIFEST_LIMIT = 16 * 1024 * 1024
_OBJECT_LIMIT = 100_000
_DIRECTORY_LIMIT = 64 * 1024 * 1024
_CHUNK_SIZE = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_OPERATION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_GENERATION_KEYS = {"device", "inode", "size_bytes", "mtime_ns", "ctime_ns"}


class ArtifactPackageError(ValueError):
    """A package or its bytes cannot be trusted or safely installed."""


@dataclass(frozen=True)
class PackageSource:
    object_id: str
    source_path: Path
    storage_key: str
    sha256: str
    size_bytes: int
    source_generation: dict[str, int]


@contextmanager
def _package_errors() -> Iterator[None]:
    try:
        yield
    except ArtifactPackageError:
        raise
    except (OSError, ValueError, RuntimeError, EOFError, zipfile.BadZipFile,
            zipfile.LargeZipFile, RecursionError, UnicodeError, struct.error) as exc:
        raise ArtifactPackageError(str(exc)) from exc


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _no_links(path: Path) -> None:
    require_no_links(path)
    # Windows reparse points include more kinds than symlinks and junctions.
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ArtifactPackageError(f"package paths cannot use reparse points: {component}")


def _generation(info: os.stat_result) -> dict[str, int]:
    return {"device": info.st_dev, "inode": info.st_ino,
            "size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns}


def _regular(path: Path) -> os.stat_result:
    _no_links(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ArtifactPackageError(f"package source is not a regular file: {path}")
    return info


def capture_source_generation(path: Path) -> dict[str, int]:
    """Freeze a JSON-safe identity/version for later copy and release checks."""
    with _package_errors():
        return _generation(_regular(_absolute(path)))


def capture_verified_file_generation(path: Path) -> tuple[int, ...]:
    """Current descriptor generation without changing the frozen v1 manifest."""
    with _package_errors(), _open_regular(_absolute(path)) as source:
        return stream_generation(source)


@contextmanager
def _open_regular(path: Path) -> Iterator[BinaryIO]:
    before = _generation(_regular(path))
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
    if os.name == "posix":
        # Anchor every ancestor, so swapping a parent to a symlink after the
        # lexical check cannot redirect the open. NONBLOCK also rejects a FIFO
        # swapped into the final filename without waiting for a writer.
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent_fd = os.open(path.anchor, directory_flags)
        try:
            for component in path.parts[1:-1]:
                next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            descriptor = os.open(path.name, flags, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    else:
        descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        opened_generation = _generation(opened)
        # On Python 3.12 Windows, path stat ctime is birth time while fstat's
        # ctime can be the NTFS change time. Compare each clock to itself.
        comparable = _GENERATION_KEYS - ({"ctime_ns"} if os.name == "nt" else set())
        if not stat.S_ISREG(opened.st_mode) or any(opened_generation[key] != before[key] for key in comparable):
            raise ArtifactPackageError(f"file changed before reading: {path}")
        yield stream
        if (_generation(os.fstat(stream.fileno())) != opened_generation
                or _generation(_regular(path)) != before):
            raise ArtifactPackageError(f"file changed while reading: {path}")


def _sync_directory(path: Path) -> None:
    if os.name != "posix" or not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
    finally:
        os.close(descriptor)


def _directory_identity(path: Path) -> tuple[int, int]:
    _no_links(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ArtifactPackageError(f"prepared target directory is missing or unsafe: {path}")
    return info.st_dev, info.st_ino


def _install_no_replace(stage: Path, destination: Path) -> None:
    _no_links(destination)
    if os.name == "nt":
        os.rename(stage, destination)
    else:
        os.link(stage, destination, follow_symlinks=False)
        stage.unlink()
    _sync_directory(destination.parent)


def _canonical(document: dict) -> bytes:
    return json.dumps(document, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _valid_hash(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _validate_generation(generation: object) -> None:
    if (not isinstance(generation, dict) or set(generation) != _GENERATION_KEYS
            or any(type(value) is not int for value in generation.values())
            or any(generation[key] < 0 for key in ("device", "inode", "size_bytes"))):
        raise ArtifactPackageError("invalid frozen source generation")


def _source_entry(source: PackageSource) -> dict:
    if (not _valid_hash(source.sha256) or source.object_id != source.sha256
            or type(source.size_bytes) is not int or source.size_bytes < 0):
        raise ArtifactPackageError("invalid frozen object identity, size or SHA-256")
    parts = portable_artifact_parts(source.storage_key)
    if parts == ("archive.db",):
        raise ArtifactPackageError("database state cannot be an artifact package object")
    _validate_generation(source.source_generation)
    if source.source_generation["size_bytes"] != source.size_bytes:
        raise ArtifactPackageError("frozen generation and object size disagree")
    return {"object_id": source.object_id, "member": f"objects/{source.sha256}",
            "storage_key": source.storage_key, "sha256": source.sha256,
            "size_bytes": source.size_bytes, "source_generation": dict(source.source_generation)}


def package_batches(sources: Iterable[PackageSource], *, max_bytes: int,
                    max_objects: int) -> tuple[tuple[PackageSource, ...], ...]:
    """Keep input order; an object exceeding the byte limit gets its own batch."""
    with _package_errors():
        if type(max_bytes) is not int or max_bytes <= 0 or type(max_objects) is not int or not 0 < max_objects <= _OBJECT_LIMIT:
            raise ArtifactPackageError("batch limits must be positive bounded integers")
        batches: list[tuple[PackageSource, ...]] = []
        current: list[PackageSource] = []
        current_bytes = 0
        seen: set[str] = set()
        for source in sources:
            _source_entry(source)
            if source.object_id in seen:
                raise ArtifactPackageError("duplicate object in frozen transfer plan")
            seen.add(source.object_id)
            if current and (len(current) >= max_objects or current_bytes + source.size_bytes > max_bytes):
                batches.append(tuple(current))
                current, current_bytes = [], 0
            current.append(source)
            current_bytes += source.size_bytes
            if current_bytes >= max_bytes or len(current) >= max_objects:
                batches.append(tuple(current))
                current, current_bytes = [], 0
        if current:
            batches.append(tuple(current))
        return tuple(batches)


def package_manifest(sources: tuple[PackageSource, ...], operation_id: str,
              plan_sha256: str, batch_index: int, *, format_version: int = 1,
              compression_level: int = 6) -> dict:
    if type(format_version) is not int or format_version not in {1, 2}:
        raise ArtifactPackageError("unsupported package format version")
    if type(compression_level) is not int or not 0 <= compression_level <= 9:
        raise ArtifactPackageError("DEFLATE compression level must be between 0 and 9")
    if (not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id)
            or not _valid_hash(plan_sha256) or type(batch_index) is not int or batch_index < 0):
        raise ArtifactPackageError("invalid package operation, plan or batch identity")
    if not 0 < len(sources) <= _OBJECT_LIMIT:
        raise ArtifactPackageError("package must have a bounded nonempty object list")
    entries = [_source_entry(source) for source in sources]
    if len({entry["object_id"] for entry in entries}) != len(entries):
        raise ArtifactPackageError("duplicate object in artifact package")
    check_artifact_collisions([entry["storage_key"] for entry in entries])
    manifest = {"format": _FORMAT, "format_version": format_version,
                "operation_id": operation_id, "plan_sha256": plan_sha256,
                "batch_index": batch_index, "objects": entries}
    if format_version == 2:
        manifest["compression"] = {"method": "deflate", "level": compression_level}
    manifest["package_id"] = hashlib.sha256(_canonical(manifest)).hexdigest()
    if len(_canonical(manifest)) > _MANIFEST_LIMIT:
        raise ArtifactPackageError("package manifest exceeds 16 MiB")
    return manifest


def _hash_stream(source: BinaryIO, *, expected_size: int | None = None,
                 destination: BinaryIO | None = None) -> tuple[int, str]:
    from bili_asr.services.artifact_io import observe_io
    digest, size = hashlib.sha256(), 0
    while chunk := source.read(_CHUNK_SIZE):
        observe_io(len(chunk))
        size += len(chunk)
        if expected_size is not None and size > expected_size:
            raise ArtifactPackageError("object exceeds its frozen size")
        digest.update(chunk)
        if destination is not None:
            remaining = memoryview(chunk)
            while remaining:
                written = destination.write(remaining)
                if written is None or written <= 0 or written > len(remaining):
                    raise ArtifactPackageError("object destination did not complete a bounded write")
                remaining = remaining[written:]
    if expected_size is not None and size != expected_size:
        raise ArtifactPackageError("object does not match its frozen size")
    return size, digest.hexdigest()


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    document: dict = {}
    for key, value in pairs:
        if key in document:
            raise ArtifactPackageError(f"duplicate package JSON key: {key}")
        document[key] = value
    return document


def _bounded_zip_directory(stream: BinaryIO) -> None:
    """Bound zipfile's eager central-directory allocation before opening it."""
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    tail_size = min(size, 65535 + 22)
    stream.seek(size - tail_size)
    tail = stream.read(tail_size)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise ArtifactPackageError("package has no valid ZIP directory")
    record = struct.unpack("<4s4H2IH", tail[offset:offset + 22])
    _signature, disk, directory_disk, disk_entries, entries, directory_size, _directory_offset, comment_size = record
    if (disk or directory_disk or disk_entries != entries
            or offset + 22 + comment_size != len(tail)):
        raise ArtifactPackageError("package has an unsupported ZIP directory")
    if entries == 65535 or directory_size == 0xFFFFFFFF:
        end_offset = size - tail_size + offset
        if end_offset < 20:
            raise ArtifactPackageError("package is missing its ZIP64 locator")
        stream.seek(end_offset - 20)
        locator = stream.read(20)
        signature, zip64_disk, zip64_offset, disks = struct.unpack("<4sIQI", locator)
        if signature != b"PK\x06\x07" or zip64_disk or disks != 1:
            raise ArtifactPackageError("package has an invalid ZIP64 locator")
        if zip64_offset + 56 > end_offset - 20:
            raise ArtifactPackageError("package has an invalid ZIP64 directory offset")
        stream.seek(zip64_offset)
        record64 = struct.unpack("<4sQ2H2I4Q", stream.read(56))
        signature, record_size, _made, _needed, disk, directory_disk, disk_entries, entries, directory_size, _offset64 = record64
        if signature != b"PK\x06\x06" or record_size != 44 or disk or directory_disk or disk_entries != entries:
            raise ArtifactPackageError("package has an unsupported ZIP64 directory")
    if not 1 < entries <= _OBJECT_LIMIT + 1 or directory_size > _DIRECTORY_LIMIT:
        raise ArtifactPackageError("package ZIP directory exceeds bounded limits")
    stream.seek(0)


def _read_manifest(bundle: zipfile.ZipFile) -> tuple[dict, bytes]:
    members = bundle.infolist()
    if not 1 < len(members) <= _OBJECT_LIMIT + 1:
        raise ArtifactPackageError("package has an invalid member count")
    names: set[str] = set()
    for member in members:
        mode = member.external_attr >> 16
        if (member.orig_filename != member.filename or member.filename in names
                or member.is_dir() or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                or member.flag_bits & 1):
            raise ArtifactPackageError(f"unsafe or duplicate ZIP member: {member.filename!r}")
        names.add(member.filename)
    if _MANIFEST_NAME not in names:
        raise ArtifactPackageError("package is missing its manifest")
    info = bundle.getinfo(_MANIFEST_NAME)
    # The manifest is always stored. Reader selection never decompresses an
    # untrusted manifest before knowing which bounded payload format it declares.
    if info.compress_type != zipfile.ZIP_STORED or info.compress_size != info.file_size:
        raise ArtifactPackageError("compressed package manifest is unsupported")
    if info.file_size > _MANIFEST_LIMIT:
        raise ArtifactPackageError("package manifest exceeds 16 MiB")
    with bundle.open(info) as stream:
        encoded = stream.read(_MANIFEST_LIMIT + 1)
    if len(encoded) > _MANIFEST_LIMIT:
        raise ArtifactPackageError("package manifest exceeds 16 MiB")
    manifest = json.loads(encoded, object_pairs_hook=_unique_json_object)
    keys = {"format", "format_version", "operation_id", "plan_sha256", "batch_index", "objects", "package_id"}
    if isinstance(manifest, dict) and manifest.get("format_version") == 2:
        keys.add("compression")
    if not isinstance(manifest, dict) or set(manifest) != keys:
        raise ArtifactPackageError("invalid package manifest fields")
    entries = manifest["objects"]
    if not isinstance(entries, list) or not 0 < len(entries) <= _OBJECT_LIMIT:
        raise ArtifactPackageError("invalid package object list")
    sources = []
    for entry in entries:
        required = {"object_id", "member", "storage_key", "sha256", "size_bytes", "source_generation"}
        if not isinstance(entry, dict) or set(entry) != required:
            raise ArtifactPackageError("invalid package object fields")
        source = PackageSource(entry["object_id"], Path("."), entry["storage_key"], entry["sha256"],
                               entry["size_bytes"], entry["source_generation"])
        expected_entry = _source_entry(source)
        if expected_entry != entry:
            raise ArtifactPackageError("package member does not match its object identity")
        sources.append(source)
    compression = manifest.get("compression", {})
    if "compression" in manifest and (not isinstance(compression, dict)
            or set(compression) != {"method", "level"} or compression["method"] != "deflate"):
        raise ArtifactPackageError("unsupported package compression")
    expected = package_manifest(tuple(sources), manifest["operation_id"], manifest["plan_sha256"], manifest["batch_index"],
                                format_version=manifest["format_version"], compression_level=compression.get("level", 6))
    if manifest != expected or type(manifest["format_version"]) is not int:
        raise ArtifactPackageError("unsupported package identity or format")
    expected_members = {_MANIFEST_NAME, *(entry["member"] for entry in entries)}
    if names != expected_members:
        raise ArtifactPackageError("package contains missing or unlisted members")
    for entry in entries:
        member = bundle.getinfo(entry["member"])
        method = zipfile.ZIP_STORED if manifest["format_version"] == 1 else zipfile.ZIP_DEFLATED
        if member.compress_type != method or (method == zipfile.ZIP_STORED and member.compress_size != member.file_size):
            raise ArtifactPackageError("compressed member differs from declared package format")
        if member.file_size != entry["size_bytes"]:
            raise ArtifactPackageError("package member size differs from manifest")
    return manifest, encoded


def _summary(path: Path, manifest: dict, encoded: bytes) -> dict:
    return {**manifest, "package_path": str(path),
            "package_key": f"packages/artifact-{manifest['package_id']}.zip",
            "manifest_sha256": hashlib.sha256(encoded).hexdigest()}


def read_package_manifest(package_path: Path) -> dict:
    """Validate bounded manifest/member structure without reading object bytes."""
    with _package_errors():
        path = _absolute(package_path)
        with _open_regular(path) as stream:
            _bounded_zip_directory(stream)
            with zipfile.ZipFile(stream) as bundle:
                manifest, encoded = _read_manifest(bundle)
        return _summary(path, manifest, encoded)


def check_artifact_package(package_path: Path, *, expected_sha256: str | None = None) -> dict:
    """Reread every target object and its container digest using bounded memory."""
    with _package_errors():
        if expected_sha256 is not None and not _valid_hash(expected_sha256):
            raise ArtifactPackageError("invalid expected package SHA-256")
        path = _absolute(package_path)
        with _open_regular(path) as stream:
            size, digest = _hash_stream(stream)
            if expected_sha256 is not None and digest != expected_sha256:
                raise ArtifactPackageError("package SHA-256 mismatch")
            _bounded_zip_directory(stream)
            with zipfile.ZipFile(stream) as bundle:
                manifest, encoded = _read_manifest(bundle)
                for entry in manifest["objects"]:
                    with bundle.open(entry["member"]) as member:
                        _, actual = _hash_stream(member, expected_size=entry["size_bytes"])
                    if actual != entry["sha256"]:
                        raise ArtifactPackageError(f"package object SHA-256 mismatch: {entry['object_id']}")
                payload_bytes = sum(entry["size_bytes"] for entry in manifest["objects"])
                stored_bytes = sum(bundle.getinfo(entry["member"]).compress_size for entry in manifest["objects"])
        summary = {**_summary(path, manifest, encoded), "package_sha256": digest,
                   "package_size_bytes": size, "valid": True}
        if manifest["format_version"] == 2:
            summary["compression_statistics"] = {"original_payload_bytes": payload_bytes,
                "compressed_payload_bytes": stored_bytes, "container_bytes": size,
                "container_to_original_ratio": size / payload_bytes if payload_bytes else None}
        return summary


def create_artifact_package(target_root: Path, sources: Iterable[PackageSource], *,
                            operation_id: str, plan_sha256: str, batch_index: int,
                            format_version: int = 1, compression_level: int = 6) -> dict:
    """Copy a frozen batch, reread it on target, and publish it without overwrite."""
    with _package_errors():
        selected = tuple(sources)
        manifest = package_manifest(selected, operation_id, plan_sha256, batch_index,
                                    format_version=format_version, compression_level=compression_level)
        root = _absolute(target_root)
        root_identity = _directory_identity(root)  # Never recreate a missing mount.
        directory = root / "packages"
        for source in selected:
            parts = portable_artifact_parts(source.storage_key)
            source_path = _absolute(source.source_path)
            if tuple(source_path.parts[-len(parts):]) != parts:
                raise ArtifactPackageError("source path does not match its portable storage key")
            source_root = source_path.parents[len(parts) - 1]
            if root.is_relative_to(source_root):
                raise ArtifactPackageError("package target must be outside the source artifact root")
        _no_links(directory)
        directory.mkdir(exist_ok=True)
        directory_identity = _directory_identity(directory)
        _sync_directory(root)
        output = directory / f"artifact-{manifest['package_id']}.zip"
        if output.exists() or output.is_symlink():
            checked = check_artifact_package(output)
            if {key: checked[key] for key in manifest} != manifest:
                raise ArtifactPackageError("existing package operation, plan or batch differs")
            return checked
        with tempfile.TemporaryDirectory(prefix=".artifact-package-stage-", dir=directory) as temporary:
            stage = Path(temporary) / "package.zip"
            method = zipfile.ZIP_STORED if format_version == 1 else zipfile.ZIP_DEFLATED
            with zipfile.ZipFile(stage, "w", compression=method, compresslevel=compression_level, allowZip64=True) as bundle:
                for source, entry in zip(selected, manifest["objects"], strict=True):
                    with _open_regular(_absolute(source.source_path)) as incoming:
                        if _generation(_regular(_absolute(source.source_path))) != source.source_generation:
                            raise ArtifactPackageError("source generation changed since planning")
                        with bundle.open(entry["member"], "w", force_zip64=True) as outgoing:
                            _, digest = _hash_stream(incoming, expected_size=source.size_bytes, destination=outgoing)
                        if digest != source.sha256:
                            raise ArtifactPackageError("source SHA-256 changed since planning")
                bundle.writestr(_MANIFEST_NAME, _canonical(manifest), compress_type=zipfile.ZIP_STORED)
            with stage.open("r+b") as stream:
                os.fsync(stream.fileno())
            checked = check_artifact_package(stage)
            if _directory_identity(root) != root_identity or _directory_identity(directory) != directory_identity:
                raise ArtifactPackageError("prepared target directory changed during transfer")
            _install_no_replace(stage, output)
            return {**checked, "package_path": str(output)}


def restore_package_object(package_path: Path, object_id: str, destination_path: Path, *,
                           expected_sha256: str, expected_size: int) -> dict:
    """Read only one object, verify it in target-side staging, and install safely."""
    with _package_errors():
        if (not _valid_hash(expected_sha256) or object_id != expected_sha256
                or type(expected_size) is not int or expected_size < 0):
            raise ArtifactPackageError("invalid expected restore object identity")
        package, destination = _absolute(package_path), _absolute(destination_path)
        if package == destination:
            raise ArtifactPackageError("restore destination cannot overwrite its source package")
        _no_links(destination)
        parent_identity = _directory_identity(destination.parent)
        if destination.exists():
            with _open_regular(destination) as stream:
                _, digest = _hash_stream(stream, expected_size=expected_size)
            if digest != expected_sha256:
                raise ArtifactPackageError("restore destination contains different bytes")
            return {"object_id": object_id, "restored_path": str(destination), "installed": False,
                    "sha256": digest, "size_bytes": expected_size}
        with _open_regular(package) as stream:
            _bounded_zip_directory(stream)
            with zipfile.ZipFile(stream) as bundle:
                return _restore_from_bundle(bundle, object_id, destination, expected_sha256,
                                            expected_size, parent_identity)


def copy_package_object(package_path: Path, object_id: str, destination: BinaryIO, *,
                        expected_size: int, package_id: str, manifest_sha256: str) -> dict:
    """Stream one exact member into caller-owned staging, then verify its bytes.

    The caller must discard staging on any error. This allows complete snapshots
    to collect external audio directly without restoring it on the source disk.
    """
    with _package_errors():
        if not _valid_hash(object_id) or type(expected_size) is not int or expected_size < 0:
            raise ArtifactPackageError("invalid expected object identity")
        with _open_regular(_absolute(package_path)) as stream:
            _bounded_zip_directory(stream)
            with zipfile.ZipFile(stream) as bundle:
                manifest, encoded = _read_manifest(bundle)
                if manifest["package_id"] != package_id or hashlib.sha256(encoded).hexdigest() != manifest_sha256:
                    raise ArtifactPackageError("package differs from frozen catalog identity")
                entry = next((entry for entry in manifest["objects"] if entry["object_id"] == object_id), None)
                if entry is None or entry["size_bytes"] != expected_size:
                    raise ArtifactPackageError("package is missing the expected object")
                with bundle.open(entry["member"]) as incoming:
                    size, digest = _hash_stream(incoming, expected_size=expected_size, destination=destination)
                if digest != object_id:
                    raise ArtifactPackageError("package object SHA-256 mismatch")
        return {"size": size, "sha256": digest}


def _restore_from_bundle(bundle: zipfile.ZipFile, object_id: str, destination: Path,
                         expected_sha256: str, expected_size: int,
                         parent_identity: tuple[int, int]) -> dict:
    manifest, encoded = _read_manifest(bundle)
    entry = next((item for item in manifest["objects"] if item["object_id"] == object_id), None)
    if entry is None or entry["sha256"] != expected_sha256 or entry["size_bytes"] != expected_size:
        raise ArtifactPackageError("package does not contain the expected restore object")
    descriptor, name = tempfile.mkstemp(prefix=".artifact-restore-stage-", dir=destination.parent)
    stage = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as outgoing, bundle.open(entry["member"]) as incoming:
            _, digest = _hash_stream(incoming, expected_size=expected_size, destination=outgoing)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if digest != expected_sha256:
            raise ArtifactPackageError("restored object SHA-256 mismatch")
        if _directory_identity(destination.parent) != parent_identity:
            raise ArtifactPackageError("restore directory changed during extraction")
        _install_no_replace(stage, destination)
    finally:
        if stage.exists():
            stage.unlink()
    return {"object_id": object_id, "package_id": manifest["package_id"],
            "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
            "restored_path": str(destination), "installed": True,
            "sha256": digest, "size_bytes": expected_size}


__all__ = [
    "ArtifactPackageError",
    "PackageSource",
    "capture_source_generation",
    "check_artifact_package",
    "copy_package_object",
    "create_artifact_package",
    "package_batches",
    "package_manifest",
    "read_package_manifest",
    "restore_package_object",
]
