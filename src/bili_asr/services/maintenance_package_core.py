"""Offline private-history packages and an unchanged installation baseline.

This protocol is separate from recovery: importing never reschedules a job,
rewrites frozen content, downloads an object, or calls a provider.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from bili_asr import __version__
from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_inventory import (
    check_artifact_collisions,
    collect_artifacts,
    portable_artifact_parts,
    require_no_links,
    require_regular_file,
    stream_hash,
)
from bili_asr.services.migration_preflight import (
    _database_state,
    _hash_file,
    _identity,
    _root_entries,
)
from bili_asr.services.runtime_root_policy import (
    RUNTIME_POLICY,
    validate_runtime_exclusions,
)
from bili_asr.services.snapshot_protocol import (
    _check_bundle_markers,
    _check_references,
    _manifest_object,
    _sync_directory,
    _sync_file,
)
from bili_asr.storage.archive_contracts import runtime_contract
from bili_asr.storage.maintenance_selection import (
    refuse_unfinished,
    select_database,
    table_ledger,
)
from bili_asr.storage.snapshots import required_artifacts, validate_snapshot_database
from bili_asr.storage.upgrade_evidence import readonly

_NAME = "maintenance.json"
_LIMIT = 16 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CONTRACT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FORMAT = "archive-maintenance-package/v1"
_PLAN = "archive-maintenance-import-plan/v1"
_RECEIPT = "archive-maintenance-import-receipt/v1"
_PROFILE = "private-history"


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def absolute(path: Path) -> Path:
    result = Path(os.path.abspath(path))
    require_no_links(result)
    return result


def _record(path: str, file: Path) -> dict:
    size, sha256, _ = _hash_file(file)
    return {"path": path, "size": size, "sha256": sha256}


def _inventory(root: Path, product: Path) -> tuple[dict, dict[str, Path]]:
    database = root / "archive.db"
    _database_state(database)
    schema_digest = validate_snapshot_database(database)
    refuse_unfinished(database)
    bases = tuple(dict.fromkeys((product, root)))
    if product != root and (product.is_relative_to(root) or root.is_relative_to(product)):
        raise ValueError("external artifact and archive roots must be disjoint")
    excluded = [entry for base in bases for entry in _root_entries(base, root, product)]
    files = {"archive.db": database, **collect_artifacts(bases)}
    records = [_record(key, path) for key, path in sorted(files.items())]
    _check_references(database, {entry["path"]: entry for entry in records})
    _check_attempt_artifacts(database, {entry["path"]: entry for entry in records})
    with closing(readonly(database)) as connection:
        contract = runtime_contract(connection)
    value = {"runtime_contract": contract, "database_schema_sha256": schema_digest,
             "files": records, "excluded": excluded, "runtime_policy": RUNTIME_POLICY}
    return {**value, "fingerprint": digest(value)}, files


def _selected_artifacts(database: Path, physical: dict[str, Path], parts: list[int]) -> dict[str, Path]:
    needed = set(required_artifacts(database))
    needed.update(_attempt_artifacts(database))
    with closing(readonly(database)) as connection:
        has_sources = connection.execute("SELECT 1 FROM sqlite_master WHERE name='source_videos'").fetchone()
        for part_id in parts:
            bvid, index = connection.execute("SELECT bvid,page_index FROM video_parts WHERE video_part_id=?", (part_id,)).fetchone()
            if bvid is not None:
                stem = f"{bvid}.p{index}"
            elif has_sources:
                platform, external_id = connection.execute("SELECT v.platform,v.external_id FROM video_parts p JOIN source_videos v USING(source_video_id) WHERE p.video_part_id=?", (part_id,)).fetchone()
                from bili_asr.platform_identity import ContentRef
                from bili_asr.source_identity import artifact_stem
                stem = artifact_stem(ContentRef(platform, external_id, index))
            else:
                raise ValueError("selected part has no portable source identity")
            for key in physical:
                if (key.startswith((f"subtitles/{stem}.", f"audio/{stem}.", f"transcripts/{stem}/",
                                    f"documents/part-{part_id}/", f"publications/part-{part_id}/"))):
                    needed.add(key)
    missing = needed - set(physical)
    if missing:
        raise ValueError("selected dependency artifact is missing: " + min(missing))
    return {key: physical[key] for key in sorted(needed)}


def _attempt_artifacts(database: Path) -> dict[str, str | None]:
    """Registered output paths remain dependencies even after failed attempts."""
    from bili_asr.artifacts import BUNDLE_MARKER_NAME, REQUIRED_ARTIFACT_KEYS
    result = {}
    with closing(readonly(database)) as connection:
        envelopes = [row[0] for row in connection.execute("SELECT result_json FROM workflow_attempts WHERE result_json IS NOT NULL")]
        for version, raw in connection.execute("SELECT schema_version,evidence_json FROM transcript_asr_evidence"):
            value = json.loads(raw)
            if version != 1 or not isinstance(value, dict) or value.get("schema_version") != 1:
                raise ValueError("unsupported ASR evidence version")
            if "audio" in value:
                if not isinstance(value["audio"], dict):
                    raise ValueError("unsupported ASR audio evidence")
                envelopes.append(json.dumps(value["audio"]))
        for raw in envelopes:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("unsupported workflow result envelope")  # noqa: TRY004 - persisted invalid data
            paths = []
            if "storage_key" in value:
                paths.append((value["storage_key"], value.get("sha256")))
            for key in REQUIRED_ARTIFACT_KEYS:
                if key in value:
                    paths.append((value[key], None))
            if "artifacts" in value:
                if not isinstance(value["artifacts"], dict):
                    raise ValueError("unsupported workflow artifact result")
                paths.extend((path, None) for path in value["artifacts"].values())
            if any(key in value for key in REQUIRED_ARTIFACT_KEYS):
                if not all(key in value for key in REQUIRED_ARTIFACT_KEYS):
                    raise ValueError("incomplete workflow bundle result")
                directory = str(Path(value["raw_path"]).parent).replace("\\", "/")
                paths.append((f"{directory}/{BUNDLE_MARKER_NAME}", None))
            for path, sha256 in paths:
                portable_artifact_parts(path)
                if sha256 is not None and (not isinstance(sha256, str) or not _HASH.fullmatch(sha256)):
                    raise ValueError("invalid workflow result artifact hash")
                previous = result.get(path)
                if previous is not None and sha256 is not None and previous != sha256:
                    raise ValueError("conflicting workflow result artifact identity")
                if path not in result or sha256 is not None:
                    result[path] = sha256
    return result


def _check_attempt_artifacts(database: Path, files: dict[str, dict]) -> None:
    for path, expected in _attempt_artifacts(database).items():
        if path not in files or (expected is not None and files[path]["sha256"] != expected):
            raise ValueError("workflow result references a missing or changed artifact: " + path)


def export_package(source_root: Path, output: Path, *, part_ids: list[int] | None = None,
                   bvids: list[str] | None = None, artifact_root: Path | None = None,
                   scope: dict | None = None) -> dict:
    root, out = absolute(source_root), absolute(output)
    if scope is not None and scope["profile"] == "publication":
        raise ValueError("publication profile is not a recoverable database package; use public maintenance export")
    product = root if artifact_root is None else absolute(artifact_root)
    if out.is_relative_to(root) or out.is_relative_to(product):
        raise ValueError("maintenance package output must be outside its source roots")
    if out.exists():
        raise ValueError("maintenance package output already exists")
    if not out.parent.is_dir():
        raise ValueError("maintenance package output parent must exist")
    with ArchiveSession(root, mode=ArchiveAccessMode.MAINTENANCE).access():
        source, physical = _inventory(root, product)
        with tempfile.TemporaryDirectory(prefix=".maintenance-export-", dir=out.parent) as temporary:
            stage = Path(temporary)
            if part_ids or bvids or scope is not None:
                database = stage / "archive.db"
                selection = select_database(root / "archive.db", database, part_ids=part_ids or [], bvids=bvids or [], scope=scope)
                selected = _selected_artifacts(database, physical, selection["resolved_part_ids"])
                selection.update({"mode": "selected", "requested_bvids": sorted(set(bvids or []))})
            else:
                database = root / "archive.db"
                selected = {key: value for key, value in physical.items() if key != "archive.db"}
                selection = {"mode": "full", "policy": "all-private-history/v1"}
            if scope is not None:
                from bili_asr.storage.maintenance_scope import filter_artifacts
                needed = set(required_artifacts(database)) | set(_attempt_artifacts(database))
                selected, excluded = filter_artifacts(selected, needed, scope)
                selection["excluded_artifacts"] = excluded
            selected = {"archive.db": database, **selected}
            files = [_record(key, path) for key, path in sorted(selected.items())]
            _check_references(database, {entry["path"]: entry for entry in files})
            _check_attempt_artifacts(database, {entry["path"]: entry for entry in files})
            # Volatile creation time is outside content identity. Decoded payload
            # and typed row ledgers are stable across repeated exports.
            provenance = {key: value for key, value in source.items() if key != "files"}
            provenance["file_count"] = len(source["files"])
            provenance["total_bytes"] = sum(entry["size"] for entry in source["files"])
            content = {"format": _FORMAT if scope is None else "archive-maintenance-package/v2",
                       "profile": _PROFILE if scope is None else scope["profile"], "source": provenance,
                       "selection": selection, "database_contract": validate_snapshot_database(database),
                       "tables": table_ledger(database), "files": files,
                       "redaction_policy": "none-frozen-private-history; credentials-models-cache-logs-excluded"}
            manifest = {**content, "content_id": digest(content), "producer_version": __version__,
                        "created_at": datetime.now(UTC).isoformat()}
            path = stage / "package.zip"
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
                for entry in files:
                    original = selected[entry["path"]]
                    before = _identity(original)
                    with original.open("rb") as reader, bundle.open(entry["path"], "w", force_zip64=True) as writer:
                        size, sha256 = stream_hash(reader, writer)
                    if (size, sha256) != (entry["size"], entry["sha256"]) or _identity(original) != before:
                        raise ValueError("source changed while packaging: " + entry["path"])
                bundle.writestr(_NAME, canonical(manifest))
                _check_bundle_markers(bundle, {entry["path"]: entry for entry in files})
            current, _ = _inventory(root, product)
            if current != source:
                raise ValueError("source inventory changed during maintenance export")
            with path.open("r+b") as stream:
                _sync_file(stream)
            # Never overwrite a concurrently created output.
            if os.name == "nt":
                path.rename(out)
            else:
                os.link(path, out)
                path.unlink()
            _sync_directory(out.parent)
    return {"operation": "maintenance-export", "valid": True, "package": str(out),
            "content_id": manifest["content_id"], "selection": selection, "file_count": len(files)}


def _json(path: Path) -> dict:
    require_regular_file(path)
    if path.stat().st_size > _LIMIT:
        raise ValueError("oversized maintenance JSON document")
    document = json.loads(path.read_bytes(), object_pairs_hook=_manifest_object)
    if not isinstance(document, dict):
        raise ValueError("maintenance document must be an object")  # noqa: TRY004 - JSON document validation
    return document


def _read_manifest(bundle: zipfile.ZipFile) -> dict:
    members = bundle.infolist()
    names = []
    for member in members:
        if (member.orig_filename != member.filename or member.is_dir() or member.flag_bits & 1
                or member.compress_type != zipfile.ZIP_STORED):
            raise ValueError("invalid maintenance package member")
        mode = member.external_attr >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            raise ValueError("maintenance package cannot contain links or special files")
        if member.filename != _NAME:
            portable_artifact_parts(member.filename)
        names.append(member.filename)
    if len(set(names)) != len(names):
        raise ValueError("duplicate maintenance package member")
    check_artifact_collisions([name for name in names if name != _NAME])
    member = bundle.getinfo(_NAME)
    if member.file_size > _LIMIT:
        raise ValueError("oversized maintenance manifest")
    with bundle.open(member) as stream:
        raw = stream.read(_LIMIT + 1)
    if len(raw) > _LIMIT:
        raise ValueError("oversized maintenance manifest")
    document = json.loads(raw, object_pairs_hook=_manifest_object)
    _validate_manifest(document)
    files = document["files"]
    listed = []
    for entry in files:
        if bundle.getinfo(entry["path"]).file_size != entry["size"]:
            raise ValueError("maintenance declared file size mismatch")
        listed.append(entry["path"])
    if set(names) != {*listed, _NAME}:
        raise ValueError("maintenance member inventory mismatch")
    return document


def _validate_manifest(document: dict) -> None:
    from bili_asr.contracts.json_schema import validate_json
    version2 = document.get("format") == "archive-maintenance-package/v2"
    validate_json(document.get("format", _FORMAT), document)
    keys = {"format", "profile", "source", "selection", "database_contract", "tables", "files",
            "redaction_policy", "content_id", "producer_version", "created_at"}
    if not isinstance(document, dict) or set(document) != keys or (not version2 and (document["format"] != _FORMAT or document["profile"] != _PROFILE)):
        raise ValueError("unsupported maintenance manifest contract")
    content = {key: value for key, value in document.items() if key not in {"content_id", "created_at", "producer_version"}}
    if document["content_id"] != digest(content):
        raise ValueError("maintenance content identity mismatch")
    if (not isinstance(document["database_contract"], str) or not _CONTRACT.fullmatch(document["database_contract"])
            or not isinstance(document["producer_version"], str)
            or document["redaction_policy"] != "none-frozen-private-history; credentials-models-cache-logs-excluded"):
        raise ValueError("invalid maintenance contract or privacy policy")
    try:
        timestamp = datetime.fromisoformat(document["created_at"])
        if timestamp.utcoffset() != UTC.utcoffset(timestamp):
            raise ValueError("not UTC")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("invalid maintenance creation timestamp") from exc
    source = document["source"]
    if (not isinstance(source, dict) or not {"runtime_contract", "database_schema_sha256", "excluded", "fingerprint", "file_count", "total_bytes"} <= set(source)
            or set(source) - {"runtime_contract", "database_schema_sha256", "excluded", "fingerprint", "file_count", "total_bytes", "runtime_policy"}
            or source["runtime_contract"] not in {"bilibili-v1", "universal-v2"}
            or not isinstance(source["fingerprint"], str) or not _HASH.fullmatch(source["fingerprint"])
            or not isinstance(source["database_schema_sha256"], str) or not _CONTRACT.fullmatch(source["database_schema_sha256"])
            or type(source["file_count"]) is not int or source["file_count"] < 1
            or type(source["total_bytes"]) is not int or source["total_bytes"] < 0
            or not isinstance(source["excluded"], list)
            or ("runtime_policy" in source and source["runtime_policy"] != RUNTIME_POLICY)):
        raise ValueError("invalid maintenance source provenance")
    runtime_entries = [item for item in source["excluded"]
                       if item.get("category") in {"runtime-fence", "asr-input-reservation", "runtime-export-lock"}]
    if runtime_entries and source.get("runtime_policy") != RUNTIME_POLICY:
        raise ValueError("maintenance runtime exclusions require an explicit policy")
    validate_runtime_exclusions(runtime_entries)
    selection = document["selection"]
    if not isinstance(selection, dict):
        raise ValueError("invalid maintenance selection")  # noqa: TRY004 - persisted contract validation
    if selection.get("mode") == "full":
        if selection != {"mode": "full", "policy": "all-private-history/v1"}:
            raise ValueError("unsupported maintenance full selection policy")
    elif selection.get("mode") == "selected":
        if version2:
            from bili_asr.storage.maintenance_scope import scope as validate_scope
            specification = selection.get("scope")
            allowed = {"mode", "policy", "requested_part_ids", "resolved_part_ids", "requested_bvids", "scope", "explicit_row_counts", "excluded_artifacts",
                       "observation_policy", "observation_boundary"}
            required_selection = allowed - {"observation_policy", "observation_boundary"}
            if (not required_selection <= set(selection) <= allowed
                    or selection.get("policy") != "native-explicit-scope-fk-owned-json/v2" or not isinstance(specification, dict)
                    or validate_scope(**specification) != specification or specification["profile"] != document["profile"]):
                raise ValueError("unsupported maintenance scoped selection")
            explicit = selection["explicit_row_counts"]
            excluded = selection["excluded_artifacts"]
            if (not isinstance(explicit, dict) or any(not isinstance(key, str) or type(value) is not int or value < 0 for key, value in explicit.items())
                    or not isinstance(excluded, list) or excluded != sorted(set(excluded))):
                raise ValueError("invalid explicit scope accounting")
            for path in excluded:
                portable_artifact_parts(path)
        elif (not set(selection) <= {"mode", "policy", "requested_part_ids", "resolved_part_ids", "requested_bvids",
                                     "observation_policy", "observation_boundary"}
                or selection["policy"] != "native-full-part-history-fk-owned-json/v1"
                or not isinstance(selection["requested_bvids"], list)
                or any(not isinstance(value, str) or not value for value in selection["requested_bvids"])):
            raise ValueError("unsupported maintenance selected policy")
        if ("observation_policy" in selection) != ("observation_boundary" in selection):
            raise ValueError("maintenance observation policy and boundary must appear together")
        if "observation_policy" in selection:
            if selection["observation_policy"] != "source-observations-closure/v1":
                raise ValueError("unsupported maintenance observation policy")
            boundary = selection.get("observation_boundary")
            if (not isinstance(boundary, dict)
                    or set(boundary) != {"shared_creator_pages", "raw_observation_rows"}
                    or not isinstance(boundary["shared_creator_pages"], list)
                    or boundary["shared_creator_pages"] != sorted(set(boundary["shared_creator_pages"]))
                    or any(not isinstance(value, str) or not value for value in boundary["shared_creator_pages"])
                    or type(boundary["raw_observation_rows"]) is not int
                    or boundary["raw_observation_rows"] < 0):
                raise ValueError("invalid maintenance observation boundary")
        for key in ("requested_part_ids", "resolved_part_ids"):
            values = selection[key]
            if not isinstance(values, list) or (not values and not version2) or any(type(value) is not int or value < 1 for value in values) or values != sorted(set(values)):
                raise ValueError("invalid maintenance part selection")
        if not version2 and not set(selection["requested_part_ids"]) <= set(selection["resolved_part_ids"]):
            raise ValueError("maintenance closure excludes requested parts")
    else:
        raise ValueError("unsupported maintenance selection mode")
    tables = document["tables"]
    if not isinstance(tables, list) or not tables:
        raise ValueError("invalid maintenance typed table ledger")
    for table in tables:
        if (not isinstance(table, dict) or set(table) != {"name", "columns", "row_count", "sha256", "includes_rowid"}
                or not isinstance(table["name"], str) or not table["name"]
                or not isinstance(table["columns"], list) or not table["columns"]
                or any(not isinstance(value, str) or not value for value in table["columns"])
                or type(table["row_count"]) is not int or table["row_count"] < 0
                or type(table["includes_rowid"]) is not bool
                or not isinstance(table["sha256"], str) or not _HASH.fullmatch(table["sha256"])):
            raise ValueError("invalid maintenance typed table entry")
    if len({table["name"] for table in tables}) != len(tables):
        raise ValueError("duplicate maintenance table entry")
    files = document["files"]
    if not isinstance(files, list) or not files:
        raise ValueError("invalid maintenance file inventory")
    listed = []
    for entry in files:
        if (not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}
                or type(entry["size"]) is not int or entry["size"] < 0
                or not isinstance(entry["sha256"], str) or not _HASH.fullmatch(entry["sha256"])):
            raise ValueError("invalid maintenance file entry")
        portable_artifact_parts(entry["path"])
        listed.append(entry["path"])
    check_artifact_collisions(listed)
    if len(set(listed)) != len(listed) or "archive.db" not in listed:
        raise ValueError("maintenance member inventory mismatch")


def inspect_package(package: Path) -> dict:
    path = absolute(package)
    require_regular_file(path)
    with zipfile.ZipFile(path) as bundle:
        manifest = _read_manifest(bundle)
    return {"operation": "maintenance-inspect", "valid": True, "payload_verified": False, "manifest": manifest}


def _validate_observation_selection(database: Path, selection: dict) -> None:
    """Bind explicit selective-export boundaries to the actual database rows.

    Older native packages and full exports have no observation boundary. A
    selected database with the extension must declare the policy even when its
    observation table is empty, so rehashing a stripped manifest cannot erase
    the promise to preserve whole shared creator-page payloads.
    """
    if selection["mode"] != "selected":
        return
    from bili_asr.storage.source_observations import require_observations

    with closing(readonly(database)) as connection:
        has_observations = require_observations(connection)
        declared = "observation_policy" in selection
        if has_observations != declared:
            raise ValueError("maintenance selected observation policy does not match database")
        if not has_observations:
            return
        if selection["observation_policy"] != "source-observations-closure/v1":
            raise ValueError("unsupported maintenance observation policy")
        actual = {
            "raw_observation_rows": connection.execute("SELECT COUNT(*) FROM platform_observations").fetchone()[0],
            "shared_creator_pages": [row[0] for row in connection.execute(
                "SELECT observation_id FROM platform_observations WHERE operation='creator-page' ORDER BY observation_id")],
        }
        if selection.get("observation_boundary") != actual:
            raise ValueError("maintenance selected observation boundary differs from database")


def _extract(package: Path, stage: Path, *, artifacts: bool = True) -> dict:
    require_regular_file(package)
    with zipfile.ZipFile(package) as bundle:
        manifest = _read_manifest(bundle)
        for entry in manifest["files"]:
            output = stage.joinpath(*portable_artifact_parts(entry["path"]))
            with bundle.open(entry["path"]) as reader:
                if artifacts or entry["path"] == "archive.db":
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with output.open("xb") as writer:
                        actual = stream_hash(reader, writer)
                        _sync_file(writer)
                else:
                    actual = stream_hash(reader)
            if actual != (entry["size"], entry["sha256"]):
                raise ValueError("maintenance file hash mismatch: " + entry["path"])
        _check_bundle_markers(bundle, {entry["path"]: entry for entry in manifest["files"]})
    database = stage / "archive.db"
    validate_snapshot_database(database, expected_contract=manifest["database_contract"])
    _validate_observation_selection(database, manifest["selection"])
    refuse_unfinished(database)
    if table_ledger(database) != manifest["tables"]:
        raise ValueError("maintenance typed table ledger mismatch")
    _check_references(database, {entry["path"]: entry for entry in manifest["files"]})
    _check_attempt_artifacts(database, {entry["path"]: entry for entry in manifest["files"]})
    return manifest


def verify_package(package: Path) -> dict:
    path = absolute(package)
    before = _hash_file(path)[:2]
    with tempfile.TemporaryDirectory(prefix="maintenance-verify-") as temporary:
        manifest = _extract(path, Path(temporary), artifacts=False)
    if _hash_file(path)[:2] != before:
        raise ValueError("maintenance package changed during verification")
    return {"operation": "maintenance-verify", "valid": True, "content_id": manifest["content_id"],
            "file_count": len(manifest["files"]), "total_bytes": sum(entry["size"] for entry in manifest["files"])}


def _free_space(target: Path, needed: int) -> None:
    ancestor = target.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    if shutil.disk_usage(ancestor).free < needed:
        raise ValueError("maintenance target lacks staging space")



def _receipt_path(root: Path, plan_id: str) -> Path:
    if not isinstance(plan_id, str) or not _HASH.fullmatch(plan_id):
        raise ValueError("invalid maintenance plan identity")
    return root / "documents" / "maintenance" / plan_id / "receipt.json"
