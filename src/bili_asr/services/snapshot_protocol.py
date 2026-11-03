"""Portable, validated snapshots of the database and its external artifacts."""

from __future__ import annotations

import errno
import json
import lzma
import os
import re
import sqlite3
import stat
import uuid
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from bili_asr.artifact_inventory import (
    check_artifact_collisions as _check_path_collisions,
)
from bili_asr.artifact_inventory import (
    portable_artifact_parts as _path_key,
)
from bili_asr.artifact_inventory import (
    require_no_links as _no_links,
)
from bili_asr.artifact_inventory import (
    require_regular_file as _regular_file,
)
from bili_asr.artifact_inventory import (
    stream_hash as _stream_hash,
)
from bili_asr.artifacts import BUNDLE_BASENAMES as _BUNDLE_NAMES
from bili_asr.artifacts import BUNDLE_BASENAMES_V3 as _BUNDLE_NAMES_V3
from bili_asr.artifacts import BUNDLE_MARKER_NAME, BUNDLE_SCHEMA, BUNDLE_SCHEMA_V3
from bili_asr.storage.snapshots import (
    required_artifacts,
    validate_snapshot_database,
)

_FORMAT = "bili-asr-snapshot"
_FORMAT_VERSION = 1
_MANIFEST_NAME = "snapshot.json"
_MANIFEST_LIMIT = 16 * 1024 * 1024
_MARKER_LIMIT = 8192
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class SnapshotError(ValueError):
    """The archive cannot be safely saved, checked or restored."""

@contextmanager
def _snapshot_errors() -> Iterator[None]:
    try:
        yield
    except SnapshotError:
        raise
    except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile,
            zipfile.LargeZipFile, RuntimeError, EOFError, zlib.error, lzma.LZMAError) as exc:
        raise SnapshotError(str(exc)) from exc

def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))

def _sync_file(stream: BinaryIO) -> None:
    stream.flush()
    os.fsync(stream.fileno())

def _sync_directory(path: Path) -> None:
    if os.name != "posix" or not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
    finally:
        os.close(descriptor)

def _check_references(database: Path, files: dict[str, dict[str, object]]) -> None:
    for path, expected_hash in required_artifacts(database).items():
        _path_key(path)
        actual = files.get(path)
        if actual is None:
            raise SnapshotError(f"database references a missing artifact: {path}")
        if expected_hash is not None and actual["sha256"] != expected_hash:
            raise SnapshotError(f"database artifact hash mismatch: {path}")

def _check_bundle_markers(bundle: zipfile.ZipFile, files: dict[str, dict[str, object]]) -> None:
    for marker_path in files:
        parts = marker_path.split("/")
        if parts[-1] != BUNDLE_MARKER_NAME:
            continue
        if len(parts) != 3 or parts[0] != "transcripts":
            raise SnapshotError(f"invalid transcript bundle marker path: {marker_path}")
        member = bundle.getinfo(marker_path)
        if member.file_size > _MARKER_LIMIT:
            raise SnapshotError(f"oversized transcript bundle marker: {marker_path}")
        try:
            with bundle.open(member) as source:
                encoded = source.read(_MARKER_LIMIT + 1)
            if len(encoded) > _MARKER_LIMIT:
                raise SnapshotError(f"oversized transcript bundle marker: {marker_path}")
            document = json.loads(encoded.decode("ascii"), object_pairs_hook=_manifest_object)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise SnapshotError(f"invalid transcript bundle marker: {marker_path}") from exc
        schema = document.get("schema") if isinstance(document, dict) else None
        names = _BUNDLE_NAMES if schema == BUNDLE_SCHEMA else _BUNDLE_NAMES_V3 if schema == BUNDLE_SCHEMA_V3 else None
        if (names is None or not isinstance(document.get("artifacts"), dict)
                or set(document["artifacts"]) != set(names)):
            raise SnapshotError(f"invalid transcript bundle marker: {marker_path}")
        directory = "/".join(parts[:-1])
        for key, basename in names.items():
            item = document["artifacts"][key]
            path = f"{directory}/{basename}"
            entry = files.get(path)
            if (not isinstance(item, dict) or set(item) != {"path", "sha256"}
                    or item.get("path") != path or entry is None
                    or item.get("sha256") != entry["sha256"]):
                raise SnapshotError(f"transcript bundle marker hash or path mismatch: {marker_path}")

def _manifest_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotError(f"duplicate manifest JSON key: {key}")
        result[key] = value
    return result

def _read_manifest(bundle: zipfile.ZipFile) -> dict[str, object]:
    members = bundle.infolist()
    manifests = [member for member in members if member.filename == _MANIFEST_NAME]
    if len(manifests) != 1 or manifests[0].file_size > _MANIFEST_LIMIT:
        raise SnapshotError("snapshot must contain one manifest of at most 16 MiB")
    with bundle.open(manifests[0]) as source:
        encoded = source.read(_MANIFEST_LIMIT + 1)
    if len(encoded) > _MANIFEST_LIMIT:
        raise SnapshotError("snapshot manifest exceeds 16 MiB")
    try:
        manifest = json.loads(encoded, object_pairs_hook=_manifest_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SnapshotError("snapshot manifest is not valid JSON") from exc
    required = {"format", "format_version", "snapshot_id", "created_at", "producer_version", "database_contract", "files"}
    allowed = required | {"runtime_policy", "excluded"}
    if not isinstance(manifest, dict) or not required <= set(manifest) or not set(manifest) <= allowed:
        raise SnapshotError("snapshot manifest has invalid fields")
    if ("runtime_policy" in manifest or "excluded" in manifest):
        if manifest.get("runtime_policy") != "archive-runtime-v1" or not isinstance(manifest.get("excluded"), list):
            raise SnapshotError("snapshot runtime exclusion policy is invalid")
        from bili_asr.services.runtime_root_policy import (
            RuntimeRootPolicyError,
            validate_runtime_exclusions,
        )
        try:
            validate_runtime_exclusions(manifest["excluded"])
        except RuntimeRootPolicyError as exc:
            raise SnapshotError("snapshot runtime exclusion entry is invalid") from exc
    if manifest["format"] != _FORMAT or type(manifest["format_version"]) is not int or manifest["format_version"] != _FORMAT_VERSION:
        raise SnapshotError("unsupported snapshot format or version")
    try:
        if not isinstance(manifest["snapshot_id"], str) or str(uuid.UUID(manifest["snapshot_id"])) != manifest["snapshot_id"]:
            raise ValueError
        created = datetime.fromisoformat(manifest["created_at"])
        if created.tzinfo is None or created.utcoffset().total_seconds() != 0:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise SnapshotError("snapshot UUID or UTC creation time is invalid") from exc
    for key in ("producer_version", "database_contract"):
        if not isinstance(manifest[key], str) or not manifest[key] or len(manifest[key]) > 256:
            raise SnapshotError(f"snapshot {key} is invalid")
    entries = manifest["files"]
    if not isinstance(entries, list) or not entries:
        raise SnapshotError("snapshot files must be a nonempty list")
    paths: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise SnapshotError("invalid snapshot file entry")
        _path_key(entry["path"])
        if type(entry["size"]) is not int or entry["size"] < 0 or not isinstance(entry["sha256"], str) or not _HASH_PATTERN.fullmatch(entry["sha256"]):
            raise SnapshotError(f"invalid size or hash for snapshot file: {entry['path']}")
        paths.append(entry["path"])
    _check_path_collisions(paths)
    if "archive.db" not in paths:
        raise SnapshotError("snapshot is missing archive.db")
    expected = {*paths, _MANIFEST_NAME}
    names: set[str] = set()
    for member in members:
        mode = member.external_attr >> 16
        if (member.orig_filename != member.filename or member.filename in names
                or member.filename not in expected or member.is_dir()
                or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                or member.flag_bits & 1):
            raise SnapshotError(f"unsafe, duplicate or unlisted ZIP member: {member.filename!r}")
        names.add(member.filename)
    if names != expected:
        raise SnapshotError("snapshot ZIP is missing files listed in its manifest")
    return manifest

def _validate_into(snapshot: Path, stage: Path, *, extract_artifacts: bool = True) -> dict[str, object]:
    _regular_file(snapshot)
    with zipfile.ZipFile(snapshot, "r") as bundle:
        manifest = _read_manifest(bundle)
        files = {entry["path"]: entry for entry in manifest["files"]}
        for key, entry in files.items():
            member = bundle.getinfo(key)
            if member.file_size != entry["size"]:
                raise SnapshotError(f"snapshot file size mismatch: {key}")
            with bundle.open(member) as source:
                if extract_artifacts or key == "archive.db":
                    target = stage.joinpath(*_path_key(key))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as destination:
                        size, digest = _stream_hash(source, destination)
                        _sync_file(destination)
                else:
                    size, digest = _stream_hash(source)
            if size != entry["size"] or digest != entry["sha256"]:
                raise SnapshotError(f"snapshot file hash mismatch: {key}")
        _check_bundle_markers(bundle, files)
    validate_snapshot_database(stage / "archive.db", expected_contract=manifest["database_contract"])
    _check_references(stage / "archive.db", files)
    return manifest

def _empty_destination(root: Path) -> bool:
    _no_links(root)
    if not root.exists():
        return False
    if not root.is_dir() or any(root.iterdir()):
        raise SnapshotError(f"restore target must be nonexistent or empty: {root}")
    return True
