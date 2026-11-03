"""Read-only physical inventory and frozen offload previews.

This service never repairs catalog rows or moves/deletes bytes. A plan is an
observation, not permission to release a file: executors must check consumers,
versions, target capacity and source identity again under their own guard.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from bili_asr.artifact_inventory import (
    ARTIFACT_DIRECTORIES,
    ArtifactInventoryError,
    is_temporary_artifact,
    portable_artifact_parts,
    require_no_links,
)
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.artifacts import (
    BUNDLE_MARKER_NAME,
    BUNDLE_SCHEMA, BUNDLE_SCHEMA_V3, BUNDLE_BASENAMES_V3,
    REQUIRED_ARTIFACT_KEYS,
    SOURCE_METADATA_KEY,
    owns_bundle_paths,
)
from bili_asr.storage.archive_contracts import UNIVERSAL_V2, runtime_contract
from bili_asr.storage.database import connect_database

INVENTORY_SCHEMA = "artifact-inventory-v1"
PLAN_SCHEMA = "artifact-offload-plan-v1"
COPY_PLAN_SCHEMA = "artifact-copy-plan-v1"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CHUNK = 1024 * 1024
_OFFLINE_ERRORS = {errno.EIO, errno.ENODEV, errno.ENXIO, errno.ENOTCONN, errno.ESTALE}


class ArtifactInventoryServiceError(ValueError):
    """An inventory request or its frozen plan is invalid."""


@dataclass(frozen=True)
class ArtifactSelection:
    """Selectors match references; retention still considers every reference."""

    kinds: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    creator_ids: tuple[str, ...] = ()
    video_ids: tuple[str, ...] = ()
    part_ids: tuple[int, ...] = ()
    versions: tuple[str, ...] = ()

    def matches(self, reference: Mapping[str, Any]) -> bool:
        context = reference.get("source") or {}
        return all(not choices or value in choices for choices, value in (
            (self.kinds, reference["kind"]),
            (self.platforms, context.get("platform")),
            (self.creator_ids, context.get("creator_id")),
            (self.video_ids, context.get("video_id")),
            (self.part_ids, reference.get("part_id")),
            (self.versions, reference["version"]),
        ))


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _colliding_paths(paths: list[str]) -> set[str]:
    """Include Unicode/case aliases, file-parent conflicts and directory aliases."""
    prefixes: dict[str, tuple[str, str]] = {}
    files = {unicodedata.normalize("NFC", path).casefold(): path for path in paths}
    collisions: set[str] = set()
    for path in paths:
        components = path.split("/")
        for length in range(1, len(components) + 1):
            prefix = "/".join(components[:length])
            canonical = unicodedata.normalize("NFC", prefix).casefold()
            old_spelling, old_path = prefixes.setdefault(canonical, (prefix, path))
            if old_spelling != prefix:
                collisions.update((old_path, path))
            if length < len(components) and canonical in files:
                collisions.update((files[canonical], path))
    return collisions


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _json_object(raw: Any) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("expected a JSON object")
    return value


def _read_facts(database_path: Path) -> tuple[list[dict], list[dict], list[dict], str, dict[str, list[dict]]]:
    """Take a consistent SQLite read transaction without schema initialization."""
    references: list[dict] = []
    diagnostics: list[dict] = []
    with closing(connect_database(database_path, readonly=True, must_exist=True)) as connection:
        connection.execute("BEGIN")
        contract = runtime_contract(connection)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if contract == UNIVERSAL_V2:
            parts = connection.execute(
                "SELECT p.video_part_id,p.page_index,s.platform,s.external_id AS video_id,"
                "c.external_id AS creator_id,p.bvid FROM video_parts p "
                "JOIN source_videos s ON s.source_video_id=p.source_video_id "
                "LEFT JOIN source_creators c ON c.creator_id=s.creator_id"
            )
        else:
            parts = connection.execute(
                "SELECT p.video_part_id,p.page_index,'bilibili' AS platform,p.bvid AS video_id,"
                "CAST(v.mid AS TEXT) AS creator_id,p.bvid FROM video_parts p JOIN videos v ON v.bvid=p.bvid"
            )
        contexts = {int(row["video_part_id"]): dict(row) for row in parts}

        def add(identity, kind, path, digest=None, size=None, part=None, version=None, group=None, binding=True):
            try:
                portable_artifact_parts(path)
                if path == "archive.db":
                    raise ValueError("the production database is outside offload scope")
                if digest is not None and (not isinstance(digest, str) or not _HASH.fullmatch(digest)):
                    raise ValueError("invalid SHA-256")
                if size is not None and (type(size) is not int or size < 0):
                    raise ValueError("invalid byte size")
            except (ArtifactInventoryError, ValueError, TypeError):
                diagnostics.append({"state": "invalid_reference", "reference_id": str(identity),
                                    "kind": kind, "path": path if isinstance(path, str) else None})
                return
            references.append({"reference_id": str(identity), "kind": kind, "path": path,
                               "sha256": digest, "expected_size": size, "part_id": part,
                               "version": str(version if version is not None else identity),
                               "group_id": str(group if group is not None else identity),
                               "version_binding": "verified" if binding and digest else "unverified",
                               "source": contexts.get(part)})

        linked: dict[int, list[int]] = defaultdict(list)
        for row in connection.execute("SELECT audio_id,video_part_id FROM part_audio_objects"):
            linked[row["audio_id"]].append(row["video_part_id"])
        for row in connection.execute("SELECT audio_id,storage_key,sha256,byte_size FROM audio_objects ORDER BY audio_id"):
            for part in linked[row["audio_id"]] or [None]:
                add(f"audio:{row['audio_id']}:{part}", "audio", row["storage_key"], row["sha256"],
                    row["byte_size"], part, f"audio:{row['audio_id']}")
        for row in connection.execute(
            "SELECT a.attempt_id,a.result_json,j.job_id,j.video_part_id FROM workflow_attempts a "
            "JOIN workflow_jobs j ON j.job_id=a.job_id WHERE a.outcome='succeeded' AND j.kind='audio' "
            "ORDER BY a.attempt_id"
        ):
            try:
                value = _json_object(row["result_json"])
            except (ValueError, TypeError):
                diagnostics.append({"state": "invalid_reference", "reference_id": f"attempt:{row['attempt_id']}",
                                    "kind": "audio", "path": None})
                continue
            add(f"attempt:{row['attempt_id']}", "audio", value.get("storage_key"), value.get("sha256"),
                value.get("byte_size"), row["video_part_id"], f"attempt:{row['attempt_id']}")
        for row in connection.execute("SELECT publication_id,video_part_id,transcript_id,artifact_json FROM workflow_publications ORDER BY publication_id"):
            identity = f"bundle:{row['publication_id']}"
            try:
                value = _json_object(row["artifact_json"])
                if not owns_bundle_paths(value):
                    raise ValueError("incomplete bundle")
            except (ValueError, TypeError):
                diagnostics.append({"state": "invalid_reference", "reference_id": identity, "kind": "bundle", "path": None})
                continue
            # Current mutable files cannot certify a historic transcript version.
            version = f"transcript:{row['transcript_id']}"
            frozen = {}
            if "artifact_publication_groups" in tables:
                frozen = dict(connection.execute("SELECT m.role,m.object_id FROM artifact_publication_groups p JOIN artifact_group_members m USING(group_id) WHERE p.publication_id=?", (row["publication_id"],)))
            current = connection.execute("SELECT publication_id FROM workflow_publications WHERE video_part_id=? ORDER BY published_at DESC,publication_id DESC LIMIT 1", (row["video_part_id"],)).fetchone()
            if frozen and current[0] != row["publication_id"]:
                continue  # Its immutable object group is enumerated below.
            for key in REQUIRED_ARTIFACT_KEYS:
                add(f"{identity}:{key}", "bundle", value[key], frozen.get(key), part=row["video_part_id"], version=version, group=identity, binding=bool(frozen))
            if SOURCE_METADATA_KEY in frozen:
                metadata_path = str(PurePosixPath(value["srt_path"]).parent / BUNDLE_BASENAMES_V3[SOURCE_METADATA_KEY])
                add(f"{identity}:{SOURCE_METADATA_KEY}", "bundle", metadata_path, frozen[SOURCE_METADATA_KEY],
                    part=row["video_part_id"], version=version, group=identity, binding=True)
            marker = str(PurePosixPath(value["srt_path"]).parent / BUNDLE_MARKER_NAME)
            add(f"{identity}:marker", "bundle", marker, frozen.get("marker"), part=row["video_part_id"], version=version, group=identity, binding=bool(frozen))
        for row in connection.execute(
            "SELECT d.relative_path,d.content_sha256,d.revision_id,d.artifact_name,i.video_part_id "
            "FROM document_artifacts d JOIN editorial_revisions r ON r.revision_id=d.revision_id "
            "JOIN editorial_inputs i ON i.input_id=r.input_id ORDER BY d.relative_path"
        ):
            identity = f"document:{row['revision_id']}:{row['artifact_name']}"
            add(identity, "document", row["relative_path"], row["content_sha256"], part=row["video_part_id"],
                version=row["revision_id"], group=f"document:{row['revision_id']}")
        for row in connection.execute("SELECT release_id,video_part_id,relative_path,artifact_sha256,status FROM publication_releases ORDER BY release_id"):
            add(f"release:{row['release_id']}", "release", row["relative_path"], row["artifact_sha256"],
                part=row["video_part_id"], version=row["release_id"])
        if "migration_records" in tables:
            for row in connection.execute("SELECT migration_id,report_json FROM migration_records ORDER BY migration_id"):
                identity = f"migration:{row['migration_id']}"
                try:
                    report = _json_object(row["report_json"])
                    if report.get("migration_id") != row["migration_id"]:
                        raise ValueError("migration identity differs")
                    for key in ("id_mapping", "imported_rows"):
                        member = report[key]
                        add(f"{identity}:{key}", "migration", member.get("path"), member.get("sha256"),
                            version=row["migration_id"], group=identity)
                except (ValueError, KeyError, TypeError, AttributeError):
                    diagnostics.append({"state": "invalid_reference", "reference_id": identity, "kind": "migration", "path": None})
                add(f"{identity}:report", "migration", f"documents/migrations/{row['migration_id']}/migration-report.json",
                    hashlib.sha256((row["report_json"] + "\n").encode("utf-8")).hexdigest(),
                    version=row["migration_id"], group=identity)
        if "manuscript_import_baselines" in tables:
            for row in connection.execute("SELECT import_id,video_part_id,body_path,body_sha256,review_path,review_sha256 FROM manuscript_import_baselines ORDER BY import_id"):
                for role in ("body", "review"):
                    add(f"source-evidence:{row['import_id']}:{role}", "source-evidence", row[f"{role}_path"],
                        row[f"{role}_sha256"], part=row["video_part_id"], version=row["import_id"], group=f"source-evidence:{row['import_id']}")
        if "artifact_group_paths" in tables:
            original = {ref["group_id"]: ref for ref in references}
            for row in connection.execute("SELECT g.*,m.role AS member_role,m.object_id,o.byte_size FROM artifact_groups g JOIN artifact_group_members m USING(group_id) JOIN artifact_objects o USING(object_id) ORDER BY g.group_id,m.role"):
                owner = original.get(row["owner_id"])
                part = None if owner is None else owner["part_id"]
                if row["owner_kind"] == "bundle":
                    publication = connection.execute("SELECT video_part_id FROM workflow_publications WHERE publication_id=?", (row["owner_id"].split(":")[-1],)).fetchone()
                    part = publication[0] if publication else None
                if row["owner_kind"] in {"bundle", "document", "release", "source-evidence", "migration"}:
                    add(f"group:{row['group_id']}:{row['member_role']}", row["owner_kind"],
                        f"documents/artifact-objects/{row['object_id']}", row["object_id"], row["byte_size"],
                        part=part, version=row["version"], group=f"group:{row['group_id']}")
        jobs = [dict(row) for row in connection.execute(
            "SELECT j.job_id,j.kind,j.video_part_id,j.profile_id,j.status,"
            "EXISTS(SELECT 1 FROM workflow_attempts a WHERE a.job_id=j.job_id AND a.outcome='running') AS running_attempt "
            "FROM workflow_jobs j ORDER BY j.job_id"
        )]
        job_map = {job["job_id"]: job for job in jobs}
        from bili_asr.services.artifact_consumer_graph import consumer_part_rows
        for job_id, part_id, _status, _updated_at, _running in consumer_part_rows(connection):
            job_map[job_id].setdefault("audio_dependency_parts", []).append(part_id)
        pins: dict[str, list[dict]] = defaultdict(list)
        if "artifact_pins" in tables:
            for row in connection.execute("SELECT pin_id,object_id,reason FROM artifact_pins WHERE released_at IS NULL ORDER BY pin_id"):
                pins[row["object_id"]].append(dict(row))
        return references, jobs, diagnostics, contract, pins


def _error_state(error: OSError) -> str:
    return "storage_unavailable" if error.errno in _OFFLINE_ERRORS else "unreadable"


def _fingerprint(info: os.stat_result) -> list[int]:
    # CPython 3.12 Windows lstat/fstat expose inconsistent ctime semantics
    # (creation time versus change time). Do not diagnose an unchanged file as
    # raced solely from that field; inode/size/mtime still guard replacement.
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns if os.name != "nt" else 0]


def _observe_file(path: Path, *, deep: bool, progress, rate: int | None) -> dict:
    result: dict[str, Any] = {"state": "missing", "size": None, "sha256": None, "allocated_bytes": None,
                              "physical_id": None, "link_count": None, "fingerprint": None}
    try:
        require_no_links(path)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            result["state"] = "nonregular"
            return result
        result.update(size=before.st_size, fingerprint=_fingerprint(before), link_count=before.st_nlink,
                      physical_id=f"{before.st_dev}:{before.st_ino}" if before.st_ino else str(path),
                      allocated_bytes=before.st_blocks * 512 if hasattr(before, "st_blocks") else None)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as source:
            opened = os.fstat(source.fileno())
            if _fingerprint(opened) != _fingerprint(before):
                result["state"] = "changed_during_scan"
                return result
            if not deep:
                result["state"] = "observed"
                return result
            digest, size, started = hashlib.sha256(), 0, time.monotonic()
            while chunk := source.read(_CHUNK):
                from bili_asr.services.artifact_io import observe_io
                observe_io(len(chunk))
                size += len(chunk)
                digest.update(chunk)
                if progress is not None:
                    progress({"path": str(path), "bytes_read": size, "total_bytes": before.st_size})
                if rate is not None:
                    wait = size / rate - (time.monotonic() - started)
                    while wait > 0:
                        time.sleep(min(wait, 0.1))
                        wait = size / rate - (time.monotonic() - started)
            after = os.fstat(source.fileno())
            if size != before.st_size or _fingerprint(before) != _fingerprint(after) or _fingerprint(path.lstat()) != _fingerprint(before):
                result["state"] = "changed_during_scan"
            else:
                result.update(state="verified", sha256=digest.hexdigest())
    except FileNotFoundError:
        pass
    except ArtifactInventoryError:
        result["state"] = "unsafe_path"
    except OSError as error:
        result["state"] = _error_state(error)
    return result


def _marker_issue(path: Path, key: str) -> str | None:
    """A bounded metadata read diagnoses markers without blessing old versions."""
    try:
        require_no_links(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                return "invalid_bundle_marker"
            raw = source.read(65537)
        if len(raw) > 65536:
            return "invalid_bundle_marker"
        payload = _json_object(raw)
        schema = payload.get("schema")
        if schema not in {BUNDLE_SCHEMA, BUNDLE_SCHEMA_V3}:
            return "unknown_format"
        artifacts = payload.get("artifacts")
        roles = (*REQUIRED_ARTIFACT_KEYS, SOURCE_METADATA_KEY) if schema == BUNDLE_SCHEMA_V3 else REQUIRED_ARTIFACT_KEYS
        if not isinstance(artifacts, dict) or set(artifacts) != set(roles):
            return "invalid_bundle_marker"
        paths = {}
        for role, item in artifacts.items():
            if not isinstance(item, dict) or set(item) != {"path", "sha256"} or not isinstance(item["sha256"], str) or not _HASH.fullmatch(item["sha256"]):
                return "invalid_bundle_marker"
            paths[role] = item["path"]
        if (not owns_bundle_paths({name: paths[name] for name in REQUIRED_ARTIFACT_KEYS})
                or PurePosixPath(paths["srt_path"]).parent != PurePosixPath(key).parent):
            return "invalid_bundle_marker"
        if schema == BUNDLE_SCHEMA_V3:
            expected = f"{PurePosixPath(key).parent.as_posix()}/{BUNDLE_BASENAMES_V3[SOURCE_METADATA_KEY]}"
            if paths[SOURCE_METADATA_KEY] != expected:
                return "invalid_bundle_marker"
    except (ArtifactInventoryError, OSError, ValueError, TypeError):
        return "invalid_bundle_marker"
    return None


def _scan_paths(base: Path, diagnostics: list[dict]) -> tuple[set[str], str]:
    paths: set[str] = set()
    try:
        require_no_links(base)
        if not stat.S_ISDIR(base.lstat().st_mode):
            return paths, "non_directory"
        with os.scandir(base) as entries:
            for entry in entries:
                if entry.name not in ARTIFACT_DIRECTORIES and entry.name not in {"archive.db", "archive.db-wal", "archive.db-shm"}:
                    diagnostics.append({"state": "outside_scope", "root": str(base), "path": entry.name})
        complete = True
        for name in sorted(ARTIFACT_DIRECTORIES):
            directory = base / name
            if not directory.exists() and not directory.is_symlink():
                continue
            try:
                require_no_links(directory)
                if not stat.S_ISDIR(directory.lstat().st_mode):
                    diagnostics.append({"state": "unsafe_path", "root": str(base), "path": name})
                    complete = False
                    continue
            except (ArtifactInventoryError, OSError):
                diagnostics.append({"state": "unreadable_directory", "root": str(base), "path": name})
                complete = False
                continue

            def unreadable(error):
                nonlocal complete
                complete = False
                diagnostics.append({"state": _error_state(error), "root": str(base), "path": str(error.filename)})

            for current, directories, files in os.walk(directory, followlinks=False, onerror=unreadable):
                current_path = Path(current)
                retained = []
                for entry in directories:
                    child = current_path / entry
                    try:
                        require_no_links(child)
                    except ArtifactInventoryError:
                        diagnostics.append({"state": "unsafe_path", "root": str(base), "path": child.relative_to(base).as_posix()})
                        complete = False
                    else:
                        retained.append(entry)
                directories[:] = retained
                for entry in files:
                    paths.add((current_path / entry).relative_to(base).as_posix())
        return paths, "available" if complete else "incomplete"
    except FileNotFoundError:
        return paths, "unavailable"
    except ArtifactInventoryError:
        return paths, "unsafe_path"
    except OSError as error:
        return paths, _error_state(error)


def inventory_artifacts(
    database_path: Path, roots: ArtifactRoots, *, deep: bool = False,
    selection: ArtifactSelection | None = None,
    external_holds: Mapping[str, tuple[str, ...]] | None = None,
    progress: Callable[[dict], None] | None = None,
    max_bytes_per_second: int | None = None,
) -> dict[str, Any]:
    """Inspect all supported roots/references, retaining unselected consumers.

    ``external_holds=None`` means no supported operational adapter was supplied;
    every selected object is blocked. An explicit empty mapping means the
    caller checked the hold source. Keys may be job ids, ``part:<id>``,
    ``sha256:<digest>`` or ``path:<relative>``. Callers must not manufacture an
    empty mapping when an operational source is unknown.
    """
    if max_bytes_per_second is not None and (type(max_bytes_per_second) is not int or max_bytes_per_second <= 0):
        raise ArtifactInventoryServiceError("max_bytes_per_second must be a positive integer")
    if type(deep) is not bool:
        raise ArtifactInventoryServiceError("deep must be a boolean")
    if external_holds is not None and any(not isinstance(key, str) or not isinstance(reasons, (list, tuple))
                                         or any(not isinstance(reason, str) or not reason for reason in reasons)
                                         for key, reasons in external_holds.items()):
        raise ArtifactInventoryServiceError("external holds must map identities to reason lists")
    selection = selection or ArtifactSelection()
    try:
        references, jobs, diagnostics, contract, persistent_pins = _read_facts(Path(database_path))
    except (sqlite3.Error, OSError, ValueError) as error:
        raise ArtifactInventoryServiceError(f"cannot read artifact inventory facts: {error}") from error
    by_path: dict[str, list[dict]] = defaultdict(list)
    for reference in references:
        by_path[reference["path"]].append(reference)
    copies: list[dict] = []
    roots_report: list[dict] = []
    for base in dict.fromkeys(roots.read_bases()):
        discovered, root_state = _scan_paths(base, diagnostics)
        roots_report.append({"path": str(base), "state": root_state, "discovered_files": len(discovered)})
        all_paths = sorted(discovered | by_path.keys())
        colliding_paths = _colliding_paths(all_paths)
        for key in all_paths:
            refs = by_path.get(key, [])
            result = _observe_file(base / key, deep=deep, progress=progress, rate=max_bytes_per_second)
            if root_state in {"unavailable", "storage_unavailable", "unreadable", "unsafe_path", "non_directory"}:
                result["state"] = "root_unavailable"
            reasons = []
            if key not in discovered and result["state"] == "missing" and not refs:
                continue
            if any(is_temporary_artifact(part) for part in key.split("/")):
                reasons.append("staging_artifact")
            try:
                portable_artifact_parts(key)
            except ArtifactInventoryError:
                reasons.append("invalid_path")
            if key in colliding_paths:
                reasons.append("path_collision")
            expected_hashes = sorted({ref["sha256"] for ref in refs if ref["sha256"]})
            expected_sizes = sorted({ref["expected_size"] for ref in refs if ref["expected_size"] is not None})
            if len(expected_hashes) > 1 or len(expected_sizes) > 1:
                reasons.append("historical_identity_conflict")
            elif expected_sizes and result["size"] is not None and result["size"] != expected_sizes[0]:
                reasons.append("size_mismatch")
            if expected_hashes and result["sha256"] and result["sha256"] not in expected_hashes:
                reasons.append("digest_mismatch")
            if result["state"] not in {"observed", "verified"}:
                reasons.append(result["state"])
            if not refs:
                reasons.append("unregistered")
            if PurePosixPath(key).name == BUNDLE_MARKER_NAME and result["state"] in {"observed", "verified"}:
                marker_issue = _marker_issue(base / key, key)
                if marker_issue:
                    reasons.append(marker_issue)
            copy = {"copy_id": _sha({"root": str(base), "path": key}), "root": str(base), "path": key,
                    **result, "reference_ids": [ref["reference_id"] for ref in refs],
                    "expected_sha256s": expected_hashes, "issues": sorted(set(reasons))}
            copies.append(copy)
    physical: dict[str, list[dict]] = defaultdict(list)
    for copy in copies:
        if copy["physical_id"]:
            physical[copy["physical_id"]].append(copy)
    for members in physical.values():
        known_paths = len({str(Path(copy["root"]) / copy["path"]) for copy in members})
        if any(copy["link_count"] > known_paths for copy in members):
            for copy in members:
                copy["issues"].append("hardlink_aliases_outside_inventory")
    objects: dict[str, dict] = {}
    for ref in references:
        object_id = f"sha256:{ref['sha256']}" if ref["sha256"] else f"reference:{ref['reference_id']}"
        obj = objects.setdefault(object_id, {"object_id": object_id, "sha256": ref["sha256"], "references": [], "copies": []})
        obj["references"].append(ref)
    copies_by_reference: dict[str, list[dict]] = defaultdict(list)
    for copy in copies:
        for reference_id in copy["reference_ids"]:
            copies_by_reference[reference_id].append(copy)
    jobs_by_part: dict[int, dict[str, dict]] = defaultdict(dict)
    for job in jobs:
        for part in (job["video_part_id"], *job.get("audio_dependency_parts", [])):
            if part is not None:
                jobs_by_part[part][job["job_id"]] = job
    groups: dict[str, list[dict]] = defaultdict(list)
    for ref in references:
        groups[ref["group_id"]].append(ref)
    incomplete_groups: set[str] = set()
    for group_id, group in groups.items():
        expected_members = {"bundle": 6, "document": 2, "source-evidence": 2, "migration": 3}.get(group[0]["kind"])
        if expected_members is not None and (len(group) != expected_members or any(
            not any(not copy["issues"] for copy in copies_by_reference[ref["reference_id"]]) for ref in group
        )):
            incomplete_groups.add(group_id)
    for obj in objects.values():
        obj["copies"] = sorted({copy["copy_id"] for ref in obj["references"] for copy in copies_by_reference[ref["reference_id"]]})
        obj["selected"] = any(selection.matches(ref) for ref in obj["references"])
        reasons: set[str] = set()
        for pin in persistent_pins.get(obj["sha256"], ()):
            reasons.add(f"persistent_pin:{pin['pin_id']}:{pin['reason']}")
        part_ids = {ref["part_id"] for ref in obj["references"] if ref["part_id"] is not None}
        if external_holds is None:
            reasons.add("external_hold_unverified")
        else:
            hold_keys = {obj["object_id"], *(f"part:{part}" for part in part_ids),
                         *(f"path:{ref['path']}" for ref in obj["references"])}
            for part in part_ids:
                hold_keys.update(jobs_by_part[part])
            for key in hold_keys:
                for reason in external_holds.get(key, ()):
                    reasons.add(f"external_hold:{key}:{reason}")
        kinds = {ref["kind"] for ref in obj["references"]}
        related_jobs = {job_id: job for part in part_ids for job_id, job in jobs_by_part[part].items()}
        for job in related_jobs.values():
            linked_to_audio = bool(part_ids.intersection(job.get("audio_dependency_parts", [])))
            relevant = (job["video_part_id"] in part_ids and ("audio" not in kinds or job["kind"] in {"audio", "asr"})) or linked_to_audio
            if relevant and (job["status"] != "succeeded" or job["running_attempt"]):
                state = "running_attempt" if job["running_attempt"] else job["status"]
                reasons.add(f"consumer:{job['job_id']}:{job['kind']}:{state}")
        if not part_ids and kinds & {"audio", "document", "release", "bundle", "source-evidence"}:
            reasons.add("unattributed_object")
        if any(ref["version_binding"] != "verified" for ref in obj["references"]):
            reasons.add("mutable_bundle_version_unverified" if "bundle" in kinds else "version_identity_unverified")
        if any(ref["group_id"] in incomplete_groups for ref in obj["references"]):
            reasons.add("complete_group_unverified")
        obj["retention_reasons"] = sorted(reasons)
    object_list = sorted(objects.values(), key=lambda obj: obj["object_id"])
    selected_by_copy: dict[str, list[dict]] = defaultdict(list)
    for obj in object_list:
        for copy_id in obj["copies"]:
            selected_by_copy[copy_id].append(obj)
    for copy in copies:
        related = selected_by_copy[copy["copy_id"]]
        selected = any(obj["selected"] for obj in related)
        blockers = set(copy["issues"])
        for obj in related:
            blockers.update(obj["retention_reasons"])
        if not deep:
            blockers.add("deep_verification_required")
        copy.update(selected=selected, blockers=sorted(blockers), candidate=selected and not blockers)
    unique_files = [members[0] for members in physical.values()]
    releasable = [members[0] for members in physical.values() if all(copy["candidate"] for copy in members)]
    logical: dict[str, int] = {}
    target: dict[str, int] = {}
    for copy in copies:
        if copy["sha256"] and copy["size"] is not None and copy["state"] == "verified" and copy["reference_ids"]:
            logical.setdefault(copy["sha256"], copy["size"])
            if copy["candidate"]:
                target.setdefault(copy["sha256"], copy["size"])
    return {"schema": INVENTORY_SCHEMA, "database_contract": contract, "integrity_depth": "deep" if deep else "quick",
            "external_holds_verified": external_holds is not None, "selection": asdict(selection),
            "roots": roots_report, "objects": object_list, "copies": copies, "diagnostics": diagnostics,
            "summary": {"references": len(references), "objects": len(object_list), "observed_paths": len(copies),
                        "path_bytes": sum(copy["size"] or 0 for copy in copies),
                        "physical_content_bytes": sum(copy["size"] or 0 for copy in unique_files),
                        "allocated_bytes": sum(copy["allocated_bytes"] for copy in unique_files) if all(copy["allocated_bytes"] is not None for copy in unique_files) else None,
                        "logical_bytes": sum(logical.values()) if deep else None,
                        "reclaimable_content_bytes": sum(copy["size"] or 0 for copy in releasable),
                        "target_payload_bytes": sum(target.values()), "restore_workspace_bytes": max(target.values(), default=0),
                        "candidate_copies": sum(copy["candidate"] for copy in copies),
                        "files_hashed": sum(copy["sha256"] is not None for copy in copies)},
            "limitations": ["Physical content bytes deduplicate reported device/inode identities; allocated bytes are unavailable on some platforms.",
                            "Reflinks, compression, sparse allocation and undetected aliases can differ from payload bytes.",
                            "Target payload excludes container overhead; restore workspace is the largest candidate object, not a concurrent-worker budget.",
                            "Quick mode opens files without reading payloads; successful stat/open is not a checksum verification.",
                            "Mutable bundle paths cannot establish historical transcript-version identity.",
                            "Root absence is unavailable storage, not proof that its referenced files were lost.",
                            "Executors must recheck holds, consumers, source identities and capacity before releasing bytes."]}


def copy_candidate_inventory(inventory: Mapping[str, Any]) -> dict[str, Any]:
    """Select stable bytes for an additional replica, preserving release guards.

    Consumers, pins and external retention holds forbid removing bytes. They
    do not forbid making another verified copy. Identity, complete-group and
    physical safety checks still apply, and no source release uses this view.
    """
    copies = []
    for copy in inventory["copies"]:
        blockers = [reason for reason in copy["blockers"] if not (
            reason.startswith(("consumer:", "persistent_pin:", "external_hold:"))
            or reason in {"external_hold_unverified", "audio_consumer_unfinished"})]
        copies.append({**copy, "blockers": blockers, "candidate": copy["selected"] and not blockers})
    payload = {copy["sha256"]: copy["size"] for copy in copies if copy["candidate"]}
    return {**inventory, "copies": copies, "summary": {**inventory["summary"],
        "candidate_copies": sum(copy["candidate"] for copy in copies),
        "reclaimable_content_bytes": 0, "target_payload_bytes": sum(payload.values()),
        "restore_workspace_bytes": max(payload.values(), default=0)}}


def plan_artifact_copy(inventory: Mapping[str, Any], *, target_id: str) -> dict[str, Any]:
    """Freeze non-destructive copying; this contract never authorizes offload."""
    return _plan_artifact(copy_candidate_inventory(inventory), target_id=target_id, schema=COPY_PLAN_SCHEMA)


def plan_artifact_offload(inventory: Mapping[str, Any], *, target_id: str) -> dict[str, Any]:
    """Freeze verified candidate copies and all reasons into a hashed preview."""
    return _plan_artifact(inventory, target_id=target_id, schema=PLAN_SCHEMA)


def _plan_artifact(inventory: Mapping[str, Any], *, target_id: str, schema: str) -> dict[str, Any]:
    if inventory.get("schema") != INVENTORY_SCHEMA or inventory.get("integrity_depth") != "deep":
        raise ArtifactInventoryServiceError("a frozen offload plan requires a deep inventory")
    if not isinstance(target_id, str) or not target_id.strip():
        raise ArtifactInventoryServiceError("target_id must name a logical storage target")
    items = []
    held = []
    objects = {obj["object_id"]: obj for obj in inventory["objects"]}
    for copy in inventory["copies"]:
        if not copy["selected"]:
            continue
        object_ids = sorted(identity for identity, obj in objects.items() if copy["copy_id"] in obj["copies"])
        if copy["candidate"]:
            items.append({key: copy[key] for key in ("copy_id", "root", "path", "sha256", "size", "fingerprint", "physical_id", "link_count")}
                         | {"object_ids": object_ids,
                            "versions": sorted({ref["version"] for identity in object_ids for ref in objects[identity]["references"]})})
        else:
            held.append({"copy_id": copy["copy_id"], "root": copy["root"], "path": copy["path"], "reasons": copy["blockers"], "object_ids": object_ids})
    body = {"schema": schema, "target_id": target_id, "selection": inventory["selection"],
            "database_contract": inventory["database_contract"], "integrity_depth": "deep",
            "external_holds_verified": inventory["external_holds_verified"], "items": items, "held": held,
            "estimate": {key: inventory["summary"][key] for key in ("reclaimable_content_bytes", "target_payload_bytes", "restore_workspace_bytes")},
            "requires_execution_revalidation": True}
    plan = body | {"plan_sha256": _sha(body)}
    validate_offload_plan(plan)
    return plan


def validate_offload_plan(plan: Mapping[str, Any]) -> None:
    """Validate a serialized frozen plan, including identity and tamper checks."""
    expected_keys = {"schema", "target_id", "selection", "database_contract", "integrity_depth", "external_holds_verified",
                     "items", "held", "estimate", "requires_execution_revalidation", "plan_sha256"}
    try:
        if set(plan) != expected_keys or plan["schema"] not in {PLAN_SCHEMA, COPY_PLAN_SCHEMA} or plan["integrity_depth"] != "deep":
            raise ValueError("unsupported plan shape")
        if not isinstance(plan["target_id"], str) or not plan["target_id"].strip() or plan["requires_execution_revalidation"] is not True:
            raise ValueError("invalid target or execution guard")
        if type(plan["external_holds_verified"]) is not bool or not isinstance(plan["items"], list) or not isinstance(plan["held"], list):
            raise ValueError("invalid plan lists")
        if not isinstance(plan["database_contract"], str) or not plan["database_contract"]:
            raise ValueError("missing database contract")
        selection = plan["selection"]
        if not isinstance(selection, dict) or set(selection) != set(asdict(ArtifactSelection())):
            raise ValueError("invalid selection shape")
        for key, values in selection.items():
            expected_type = int if key == "part_ids" else str
            if not isinstance(values, (list, tuple)) or any(type(value) is not expected_type for value in values):
                raise ValueError("invalid selection values")
        estimate = plan["estimate"]
        if not isinstance(estimate, dict) or set(estimate) != {"reclaimable_content_bytes", "target_payload_bytes", "restore_workspace_bytes"} or any(type(value) is not int or value < 0 for value in estimate.values()):
            raise ValueError("invalid space estimate")
        if plan["schema"] == PLAN_SCHEMA and plan["items"] and not plan["external_holds_verified"]:
            raise ValueError("unverified holds cannot permit candidates")
        seen = set()
        for item in plan["items"]:
            if set(item) != {"copy_id", "root", "path", "sha256", "size", "fingerprint", "physical_id", "link_count", "object_ids", "versions"}:
                raise ValueError("invalid candidate shape")
            portable_artifact_parts(item["path"])
            if item["path"] == "archive.db" or not isinstance(item["sha256"], str) or not _HASH.fullmatch(item["sha256"]):
                raise ValueError("incomplete content identity")
            if type(item["size"]) is not int or item["size"] < 0 or not Path(item["root"]).is_absolute():
                raise ValueError("invalid size/root")
            if not isinstance(item["fingerprint"], list) or len(item["fingerprint"]) != 5 or any(type(value) is not int for value in item["fingerprint"]):
                raise ValueError("invalid source fingerprint")
            if not isinstance(item["object_ids"], list) or not item["object_ids"] or not isinstance(item["versions"], list) or not item["versions"]:
                raise ValueError("missing logical versions")
            if any(not isinstance(value, str) or not value for value in item["object_ids"] + item["versions"]):
                raise ValueError("invalid logical versions")
            if item["object_ids"] != [f"sha256:{item['sha256']}"]:
                raise ValueError("candidate logical identity differs from verified content")
            if type(item["link_count"]) is not int or item["link_count"] < 1 or not isinstance(item["physical_id"], str) or not item["physical_id"]:
                raise ValueError("invalid physical identity")
            if item["fingerprint"][2] != item["size"]:
                raise ValueError("source fingerprint size differs")
            if item["copy_id"] != _sha({"root": item["root"], "path": item["path"]}) or item["copy_id"] in seen:
                raise ValueError("duplicate or conflicting copy identity")
            seen.add(item["copy_id"])
        for item in plan["held"]:
            if not isinstance(item, dict) or set(item) != {"copy_id", "root", "path", "reasons", "object_ids"}:
                raise ValueError("invalid held item shape")
            if not isinstance(item["root"], str) or not Path(item["root"]).is_absolute() or not isinstance(item["path"], str):
                raise ValueError("invalid held location")
            if item["copy_id"] != _sha({"root": item["root"], "path": item["path"]}) or item["copy_id"] in seen:
                raise ValueError("duplicate held copy")
            if not isinstance(item["reasons"], list) or not item["reasons"] or any(not isinstance(reason, str) or not reason for reason in item["reasons"]):
                raise ValueError("missing held reasons")
            if not isinstance(item["object_ids"], list) or not item["object_ids"] or any(not isinstance(value, str) or not value for value in item["object_ids"]):
                raise ValueError("missing held identities")
            seen.add(item["copy_id"])
        if plan["plan_sha256"] != _sha({key: value for key, value in plan.items() if key != "plan_sha256"}):
            raise ValueError("plan digest differs")
    except (KeyError, TypeError, ValueError, ArtifactInventoryError) as error:
        raise ArtifactInventoryServiceError(f"invalid frozen offload plan: {error}") from error
