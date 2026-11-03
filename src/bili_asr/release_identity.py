"""Build evidence captured once per interpreter, independent of the work queue."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import marshal
import os
from pathlib import Path
import re
import stat
import struct
import types
import zipfile

MANIFEST_NAME = "_build_identity.json"
PACKAGE_ROOT = Path(__file__).resolve().parent
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_PYC_OPTIMIZATION = re.compile(r"\.opt-([0-2])\.pyc\Z")
_MAX_PYC_SIZE = 64 * 1024 * 1024


class ReleaseIdentityError(RuntimeError):
    """The selected immutable release does not match this interpreter."""


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _verify_runtime_bytecode(path: Path) -> None:
    """Accept only interpreter caches whose code exactly matches their source."""
    try:
        if path.parent.name != "__pycache__" or path.stat().st_size > _MAX_PYC_SIZE:
            raise ReleaseIdentityError("package contains invalid bytecode")
        source = Path(importlib.util.source_from_cache(os.fspath(path))).resolve(strict=True)
        data = path.read_bytes()
        if len(data) < 16 or data[:4] != importlib.util.MAGIC_NUMBER:
            raise ReleaseIdentityError("package contains invalid bytecode")
        flags = struct.unpack("<I", data[4:8])[0]
        if flags & ~0x03 or (flags & 0x02 and not flags & 0x01):
            raise ReleaseIdentityError("package contains invalid bytecode")
        source_bytes = source.read_bytes()
        if flags & 0x01:
            if flags & 0x02 and data[8:16] != importlib.util.source_hash(source_bytes):
                raise ReleaseIdentityError("package contains stale bytecode")
        else:
            source_stat = source.stat()
            cached_mtime, cached_size = struct.unpack("<II", data[8:16])
            if cached_mtime != (int(source_stat.st_mtime) & 0xFFFFFFFF) or cached_size != (source_stat.st_size & 0xFFFFFFFF):
                raise ReleaseIdentityError("package contains stale bytecode")
        cached_code = marshal.loads(data[16:])
        if not isinstance(cached_code, types.CodeType) or not isinstance(cached_code.co_filename, str):
            raise ReleaseIdentityError("package contains invalid bytecode")
        match = _PYC_OPTIMIZATION.search(path.name)
        optimize = int(match.group(1)) if match else 0
        # The filename is diagnostic metadata and may differ when a pure-Python
        # wheel moves between hosts. Compare executable code independently of it.
        compiled_code = compile(source_bytes, cached_code.co_filename, "exec", dont_inherit=True, optimize=optimize)
        if cached_code != compiled_code:
            raise ReleaseIdentityError("package bytecode does not match its source")
    except ReleaseIdentityError:
        raise
    except Exception as exc:
        raise ReleaseIdentityError("package contains unverifiable bytecode") from exc


def package_files(root: Path, *, allow_runtime_bytecode: bool = True) -> dict[str, str]:
    """Hash package sources and verify any runtime cache against its source."""
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ReleaseIdentityError("package contains an unsupported file")
        if path.is_dir():
            if path.suffix in {".py", ".json", ".sql"} or relative.as_posix() == MANIFEST_NAME:
                raise ReleaseIdentityError("package contains an unsupported file")
            continue
        if not path.is_file():
            raise ReleaseIdentityError("package contains an unsupported file")
        if "__pycache__" in relative.parts and path.suffix == ".pyc":
            if not allow_runtime_bytecode:
                raise ReleaseIdentityError("release source contains bytecode")
            _verify_runtime_bytecode(path)
            continue
        if relative.as_posix() == MANIFEST_NAME:
            continue
        if path.suffix not in {".py", ".json", ".sql"}:
            raise ReleaseIdentityError("package contains an unsupported file")
        result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def wheel_package_files(archive: zipfile.ZipFile) -> dict[str, str]:
    """Hash allowed package members; reject untracked wheel payloads."""
    result = {}
    seen = set()
    for info in archive.infolist():
        name = info.filename
        if (not name or "\\" in name or name.startswith("/")
                or name.endswith("//")
                or any(part in {"", ".", ".."} for part in name.rstrip("/").split("/"))):
            raise ReleaseIdentityError("wheel contains an unsafe member")
        normalized = name.rstrip("/")
        if Path(normalized).suffix == ".pyc":
            raise ReleaseIdentityError("wheel contains bytecode")
        if normalized in seen:
            raise ReleaseIdentityError("wheel contains duplicate members")
        seen.add(normalized)
        is_directory = info.is_dir()
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if file_type and file_type not in ({stat.S_IFDIR} if is_directory else {stat.S_IFREG}):
            raise ReleaseIdentityError("wheel contains an unsupported member")
        if is_directory:
            if normalized == "bili_asr" or normalized.startswith("bili_asr/"):
                continue
            if normalized.endswith(".dist-info") and "/" not in normalized:
                continue
            # A directory entry under one top-level dist-info directory is metadata.
            parts = normalized.split("/")
            if parts[0].endswith(".dist-info") and len(parts) > 1:
                continue
            raise ReleaseIdentityError("wheel contains an untracked member")

        parts = normalized.split("/")
        if parts[0] == "bili_asr" and len(parts) > 1:
            relative_parts = parts[1:]
            if normalized == f"bili_asr/{MANIFEST_NAME}":
                continue
            if Path(normalized).suffix not in {".py", ".json", ".sql"}:
                raise ReleaseIdentityError("wheel contains an unsupported package member")
            with archive.open(info, "r") as stream:
                result["/".join(relative_parts)] = hashlib.file_digest(stream, "sha256").hexdigest()
            continue
        if parts[0].endswith(".dist-info") and len(parts) > 1:
            continue
        raise ReleaseIdentityError("wheel contains an untracked member")
    return result


def create_manifest(root: Path, source_commit: str, lock_sha256: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40,64}", source_commit) or not _HEX64.fullmatch(lock_sha256):
        raise ReleaseIdentityError("invalid release identity")
    value = {"schema_version": 1, "source_commit": source_commit,
             "lock_sha256": lock_sha256, "files": package_files(root, allow_runtime_bytecode=False)}
    return {**value, "build_id": _digest(value)}


def validate_manifest(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {
            "schema_version", "source_commit", "lock_sha256", "files", "build_id"}:
        raise ReleaseIdentityError("invalid build manifest")
    if value["schema_version"] != 1 or not isinstance(value["source_commit"], str) or not re.fullmatch(
            r"[0-9a-f]{40,64}", value["source_commit"]):
        raise ReleaseIdentityError("invalid build manifest")
    for key in ("lock_sha256", "build_id"):
        if not isinstance(value[key], str) or not _HEX64.fullmatch(value[key]):
            raise ReleaseIdentityError("invalid build manifest")
    files = value["files"]
    if not isinstance(files, dict) or not files or len(files) > 10000:
        raise ReleaseIdentityError("invalid build file inventory")
    for name, checksum in files.items():
        if (not isinstance(name, str) or "\\" in name or name.startswith("/")
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or Path(name).suffix not in {".py", ".json", ".sql"}
                or not isinstance(checksum, str) or not _HEX64.fullmatch(checksum)):
            raise ReleaseIdentityError("invalid build file inventory")
    if _digest({k: v for k, v in value.items() if k != "build_id"}) != value["build_id"]:
        raise ReleaseIdentityError("build manifest digest mismatch")
    return value


def _capture() -> tuple[dict | None, str | None]:
    path = PACKAGE_ROOT / MANIFEST_NAME
    try:
        if not path.exists():
            return None, "unattested"
        if path.stat().st_size > 2 * 1024 * 1024 or path.is_symlink():
            return None, "invalid_manifest"
        return validate_manifest(json.loads(path.read_text(encoding="utf-8"))), None
    except (OSError, ValueError, ReleaseIdentityError):
        return None, "invalid_manifest"


# This value never changes after package import. Re-reading an overwritten
# wheel must not make an old PID claim it loaded the new build.
_STARTUP_MANIFEST, _STARTUP_ERROR = _capture()


def build_status(*, expected_build_id: str | None = None) -> dict:
    expected = expected_build_id or os.environ.get("BILI_ASR_EXPECTED_BUILD_ID")
    manifest = _STARTUP_MANIFEST
    result = {"schema_version": 1, "build_id": manifest["build_id"] if manifest else None,
              "source_commit": manifest["source_commit"] if manifest else None,
              "lock_sha256": manifest["lock_sha256"] if manifest else None,
              "state": _STARTUP_ERROR or "verified", "production_acceptance": "pending"}
    if expected is not None and (manifest is None or expected != manifest["build_id"]):
        return {**result, "state": "unexpected_build"}
    if manifest is None:
        return result
    try:
        disk_manifest, disk_error = _capture()
        if disk_error or disk_manifest != manifest or package_files(PACKAGE_ROOT) != manifest["files"]:
            result["state"] = "disk_drift"
    except ReleaseIdentityError:
        result["state"] = "disk_drift"
    except OSError:
        result["state"] = "disk_unreadable"
    return result


def require_expected_build(expected_build_id: str | None = None) -> dict:
    status = build_status(expected_build_id=expected_build_id)
    if (expected_build_id or os.environ.get("BILI_ASR_EXPECTED_BUILD_ID")) and status["state"] != "verified":
        raise ReleaseIdentityError("release verification failed: " + status["state"])
    if status["state"] not in {"verified", "unattested"}:
        raise ReleaseIdentityError("release verification failed: " + status["state"])
    return status
