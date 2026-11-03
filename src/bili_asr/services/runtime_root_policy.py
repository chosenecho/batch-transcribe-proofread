"""Explicit policy for product-owned runtime directories at archive roots.

Runtime coordination leaves a small amount of filesystem state behind after a
worker exits.  Maintenance must account for that state explicitly: stopped
fence lock files are safe to exclude, while active or malformed state remains a
hard failure.  This module is intentionally independent of the database so all
maintenance entry points can apply the same rule.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from bili_asr.archive_maintenance import ArchiveBusyError, archive_access
from bili_asr.artifact_inventory import (
    ArtifactInventoryError,
    require_no_links,
    require_regular_file,
)
from bili_asr.file_ops import exclusive_lock

RUNTIME_POLICY = "archive-runtime-v1"
ARTIFACT_RUNTIME = ".artifact-runtime"
ASR_INPUT_RESERVATIONS = ".asr-input-reservations"
PRESERVED_IMPORT_LOCK = ".preserved-body-import.export.lock"
_FENCE = re.compile(r"^\.([0-9a-f]{64})\.archive-maintenance\.lock$")
_RESERVATION = re.compile(r"^([0-9a-f]{32})\.json$")
_MAX_RECEIPT_BYTES = 64 * 1024


class RuntimeRootPolicyError(ValueError):
    """A product-owned runtime directory is unsafe for maintenance."""


def _entry(path: Path, category: str, *, state: str, count: int = 1) -> dict:
    return {"path": path.name, "category": category, "state": state, "count": count}


def _fence_entries(directory: Path) -> list[dict]:
    result: list[dict] = []
    for item in sorted(directory.iterdir(), key=lambda path: path.name):
        try:
            require_no_links(item)
        except ArtifactInventoryError as exc:
            raise RuntimeRootPolicyError(str(exc)) from exc
        match = _FENCE.fullmatch(item.name)
        if match is None:
            raise RuntimeRootPolicyError("unknown file in .artifact-runtime")
        try:
            before = require_regular_file(item)
        except ArtifactInventoryError as exc:
            raise RuntimeRootPolicyError("runtime fence is not a regular file") from exc
        if before.st_size != 0 or before.st_nlink != 1:
            raise RuntimeRootPolicyError("runtime fence must be an empty regular file with one link")
        # archive_access uses the requested path's parent and the same stable
        # lock basename as resource_fence.  Acquiring it exclusively proves
        # that no worker currently owns the fence; the lock file itself is
        # intentionally retained as stopped runtime evidence.
        requested = directory / match.group(1)
        try:
            with archive_access(requested, exclusive=True, reentrant=False):
                after = require_regular_file(item)
                if (after.st_dev, after.st_ino, after.st_size, after.st_nlink) != (
                        before.st_dev, before.st_ino, 0, 1):
                    raise RuntimeRootPolicyError("runtime fence changed while inspecting")
        except ArchiveBusyError as exc:
            raise RuntimeRootPolicyError("active runtime fence blocks maintenance") from exc
        except RuntimeRootPolicyError:
            raise
        except (OSError, ValueError) as exc:
            raise RuntimeRootPolicyError("runtime fence cannot be inspected safely") from exc
        result.append(_entry(item, "runtime-fence", state="stopped-runtime-fence"))
    return result


def _reservation_entries(directory: Path) -> list[dict]:
    result: list[dict] = []
    for item in sorted(directory.iterdir(), key=lambda path: path.name):
        try:
            require_no_links(item)
        except ArtifactInventoryError as exc:
            raise RuntimeRootPolicyError(str(exc)) from exc
        match = _RESERVATION.fullmatch(item.name)
        if match is None:
            raise RuntimeRootPolicyError("unknown file in .asr-input-reservations")
        try:
            info = require_regular_file(item)
            if info.st_size > _MAX_RECEIPT_BYTES:
                raise RuntimeRootPolicyError("ASR input reservation receipt is oversized")
            value = json.loads(item.read_text(encoding="utf-8"))
        except RuntimeRootPolicyError:
            raise
        except (ArtifactInventoryError, OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise RuntimeRootPolicyError("malformed ASR input reservation receipt") from exc
        if (not isinstance(value, dict) or set(value) != {"schema_version", "byte_size"}
                or type(value.get("schema_version")) is not int or value["schema_version"] != 1
                or type(value.get("byte_size")) is not int or value["byte_size"] < 0):
            raise RuntimeRootPolicyError("malformed ASR input reservation receipt")
        # A receipt is the durable indication that a reservation may still be
        # held after a crash.  It cannot be silently retired by maintenance.
        raise RuntimeRootPolicyError("active ASR input reservation blocks maintenance")
    return result


def inspect_runtime_root(root: Path) -> list[dict]:
    """Validate product-owned runtime directories and return exclusions.

    Missing directories and an empty runtime directory are valid.  Returned
    entries are manifest-safe, deterministic records; no runtime bytes are
    included in portable archives.
    """
    base = Path(root)
    if not base.exists() and not base.is_symlink():
        return []
    try:
        require_no_links(base)
    except ArtifactInventoryError as exc:
        raise RuntimeRootPolicyError(str(exc)) from exc
    result: list[dict] = []
    lock = base / PRESERVED_IMPORT_LOCK
    if lock.exists() or lock.is_symlink():
        try:
            require_no_links(lock)
            require_regular_file(lock)
            with exclusive_lock(base / "preserved-body-import", existing_only=True):
                result.append(_entry(lock, "runtime-export-lock", state="stopped-export-lock"))
        except (OSError, ValueError) as exc:
            raise RuntimeRootPolicyError("preserved body import export lock is active or damaged") from exc
    for name, inspector, category in (
        (ARTIFACT_RUNTIME, _fence_entries, "runtime-fence"),
        (ASR_INPUT_RESERVATIONS, _reservation_entries, "asr-input-reservation"),
    ):
        directory = base / name
        if not directory.exists() and not directory.is_symlink():
            continue
        try:
            require_no_links(directory)
        except ArtifactInventoryError as exc:
            raise RuntimeRootPolicyError(str(exc)) from exc
        if not directory.is_dir():
            raise RuntimeRootPolicyError(f"runtime directory is not a directory: {name}")
        entries = inspector(directory)
        if entries:
            result.extend({**item, "path": f"{name}/{item['path']}"} for item in entries)
        else:
            result.append({"path": name, "category": category, "state": "empty-runtime-directory", "count": 0})
    return result


def inspect_runtime_roots(roots: tuple[Path, ...] | list[Path]) -> list[dict]:
    """Inspect multiple roots, preserving deterministic root-qualified paths."""
    result: list[dict] = []
    seen: set[Path] = set()
    for root in roots:
        base = Path(root)
        if base in seen:
            continue
        seen.add(base)
        for item in inspect_runtime_root(base):
            result.append({**item, "base": str(base)})
    return result


def validate_runtime_exclusions(entries: list[dict]) -> None:
    """Validate persisted exclusions without trusting their descriptive labels."""
    if not isinstance(entries, list):
        raise RuntimeRootPolicyError("invalid runtime exclusions")
    seen = set()
    for item in entries:
        required = {"path", "category", "state", "count"}
        if (not isinstance(item, dict) or not required <= item.keys()
                or item.keys() - required - {"base"}
                or not isinstance(item["path"], str)
                or type(item["count"]) is not int
                or ("base" in item and (not isinstance(item["base"], str) or not item["base"]))):
            raise RuntimeRootPolicyError("invalid runtime exclusion entry")
        path = item["path"]
        if path == ARTIFACT_RUNTIME:
            expected = ("runtime-fence", "empty-runtime-directory", 0)
        elif path == ASR_INPUT_RESERVATIONS:
            expected = ("asr-input-reservation", "empty-runtime-directory", 0)
        elif path == PRESERVED_IMPORT_LOCK:
            expected = ("runtime-export-lock", "stopped-export-lock", 1)
        elif path.startswith(ARTIFACT_RUNTIME + "/") and _FENCE.fullmatch(path[len(ARTIFACT_RUNTIME) + 1:]):
            expected = ("runtime-fence", "stopped-runtime-fence", 1)
        else:
            raise RuntimeRootPolicyError("unsupported runtime exclusion path")
        if (item["category"], item["state"], item["count"]) != expected:
            raise RuntimeRootPolicyError("runtime exclusion state does not match path")
        identity = (item.get("base"), path)
        if identity in seen:
            raise RuntimeRootPolicyError("duplicate runtime exclusion entry")
        seen.add(identity)


__all__ = [
    "ARTIFACT_RUNTIME", "ASR_INPUT_RESERVATIONS", "PRESERVED_IMPORT_LOCK", "RUNTIME_POLICY",
    "RuntimeRootPolicyError", "inspect_runtime_root", "inspect_runtime_roots",
    "validate_runtime_exclusions",
]
