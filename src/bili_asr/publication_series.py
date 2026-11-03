"""Editor-confirmed, versioned series, separate from immutable manuscript content."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from bili_asr.file_ops import (
    FileOperationError as ExportSnapshotError, atomic_replace_file, checked_path, json_bytes,
    exclusive_lock as _exclusive_lock, fsync_directory as sync_directory,
)


def _object(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ExportSnapshotError(f"series {name} fields differ from the contract")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ExportSnapshotError(f"invalid series {name}")
    return value.strip()


def _ordinal(value):
    if type(value) is not int or not 1 <= value <= 9007199254740991:
        raise ExportSnapshotError("series ordinal must be a positive safe integer")
    return value


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ExportSnapshotError(f"duplicate series JSON key: {key}")
        value[key] = item
    return value


def _invalid_constant(value):
    raise ExportSnapshotError(f"invalid series JSON constant: {value}")


def read_series_with_sha256(path: Path) -> tuple[dict, str]:
    path = checked_path(path)
    body = path.read_bytes()
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=_invalid_constant)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ExportSnapshotError(f"invalid series JSON: {exc}") from exc
    return validate_editorial_series(value), hashlib.sha256(body).hexdigest()


def read_series(path: Path) -> dict:
    return read_series_with_sha256(path)[0]


def _definitions(value, *, public=False):
    if not isinstance(value, list):
        raise ExportSnapshotError("series must be an array")
    result = []
    ids = set()
    for item in value:
        _object(item, {"id", "title", "sourceUrl", "evidence", "members", "knownMissing"}, "definition")
        identity = item["id"]
        if not isinstance(identity, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identity) or identity in ids:
            raise ExportSnapshotError("invalid or duplicate series ID")
        ids.add(identity)
        source_url = _text(item["sourceUrl"], "source URL")
        try:
            url = urlsplit(source_url)
            valid_url = url.scheme == "https" and url.hostname and not url.username and not url.password and not url.fragment
            _ = url.port
        except ValueError:
            valid_url = False
        if (not valid_url or not source_url.startswith("https://") or "#" in source_url or "\\" in source_url
                or any(c.isspace() for c in source_url)):
            raise ExportSnapshotError("series source URL must be an absolute HTTPS evidence URL without credentials or fragment")
        if not isinstance(item["members"], list) or not item["members"]:
            raise ExportSnapshotError("series must contain at least one confirmed member")
        members, bvids, ordinals = [], set(), set()
        previous = 0
        for member in item["members"]:
            fields = {"bvid", "label", "ordinal"} | ({"entries"} if public else set())
            _object(member, fields, "member")
            bvid = member["bvid"]
            ordinal = _ordinal(member["ordinal"])
            if not isinstance(bvid, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid) or bvid in bvids:
                raise ExportSnapshotError("invalid or duplicate series BVID")
            if ordinal <= previous:
                raise ExportSnapshotError("series members must be strictly ordered by ordinal")
            previous = ordinal
            bvids.add(bvid)
            ordinals.add(ordinal)
            normalized = {"bvid": bvid, "ordinal": ordinal, "label": _text(member["label"], "member label")}
            if public:
                normalized["entries"] = member["entries"]
            members.append(normalized)
        if not isinstance(item["knownMissing"], list):
            raise ExportSnapshotError("series knownMissing must be an array")
        missing = []
        previous = 0
        for member in item["knownMissing"]:
            _object(member, {"ordinal", "label", "note"}, "known missing member")
            ordinal = _ordinal(member["ordinal"])
            if ordinal in ordinals or ordinal <= previous:
                raise ExportSnapshotError("known missing ordinals must be ordered and disjoint from confirmed members")
            previous = ordinal
            missing.append({"ordinal": ordinal, "label": _text(member["label"], "missing label"),
                            "note": _text(member["note"], "missing note")})
        result.append({"id": identity, "title": _text(item["title"], "title"), "sourceUrl": source_url,
                       "evidence": _text(item["evidence"], "evidence"), "members": members, "knownMissing": missing})
    return sorted(result, key=lambda item: item["id"])


def validate_editorial_series(value: object) -> dict:
    _object(value, {"schemaVersion", "updatedBy", "series"}, "editorial envelope")
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        raise ExportSnapshotError("unsupported series schema version")
    return {"schemaVersion": 1, "updatedBy": _text(value["updatedBy"], "editor actor"),
            "series": _definitions(value["series"])}


def editorial_version(value: dict) -> str:
    return hashlib.sha256(json_bytes(validate_editorial_series(value))).hexdigest()


def edit_series(input_path: Path, output: Path, *, actor: str, expected_sha256: str) -> dict:
    """Compare raw file SHA-256 under a lock, then atomically save a complete edit."""
    value = read_series(input_path)
    value["updatedBy"] = _text(actor, "editor actor")
    output = checked_path(output)
    if expected_sha256 != "new" and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ExportSnapshotError("expected series SHA-256 must be 64 lowercase hex characters or new")
    output.parent.mkdir(parents=True, exist_ok=True)
    content = json_bytes(value)
    with _exclusive_lock(output):
        if output.exists():
            if not output.is_file():
                raise ExportSnapshotError("series output must be a regular file")
            _, current = read_series_with_sha256(output)
        else:
            current = "new"
        if current != expected_sha256:
            raise ExportSnapshotError("series edit version conflict; reload the current file")
        atomic_replace_file(output, content, sync_parent=sync_directory)
    return {"sha256": hashlib.sha256(content).hexdigest(), "editorialVersion": editorial_version(value),
            "seriesCount": len(value["series"]), "output": str(output)}


def _bindings(articles: list, bvid: str) -> list:
    selected = [entry for entry in articles if entry.get("bvid") == bvid or (
        entry.get("platform") == "bilibili" and entry.get("externalVideoId") == bvid)]
    selected.sort(key=lambda entry: (entry.get("pageIndex", entry.get("partIndex")), entry["slug"]))
    return [{key: entry[key] for key in ("slug", "editionId", "contentSha256", "artifactSha256")} for entry in selected]


def project_series(value: dict, articles: list, manuscript_type: str) -> dict:
    value = validate_editorial_series(value)
    if manuscript_type not in {"publication", "publication-draft"}:
        raise ExportSnapshotError("invalid series manuscript category")
    definitions = []
    for definition in value["series"]:
        definitions.append({**definition, "members": [{**member, "entries": _bindings(articles, member["bvid"])}
                                                      for member in definition["members"]]})
    return {"schemaVersion": 1, "manuscriptType": manuscript_type,
            "editorialVersion": editorial_version(value), "series": definitions}


def validate_public_series(value: object, articles: list, manuscript_type: str) -> dict:
    _object(value, {"schemaVersion", "manuscriptType", "editorialVersion", "series"}, "public envelope")
    if (type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1
            or value["manuscriptType"] != manuscript_type or manuscript_type not in {"publication", "publication-draft"}
            or not isinstance(value["editorialVersion"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["editorialVersion"])):
        raise ExportSnapshotError("invalid public series identity")
    definitions = _definitions(value["series"], public=True)
    for definition in definitions:
        for member in definition["members"]:
            if member["entries"] != _bindings(articles, member["bvid"]):
                raise ExportSnapshotError("series member bindings differ from the exact category catalog")
    normalized = {**value, "series": definitions}
    if normalized != value:
        raise ExportSnapshotError("public series is not normalized")
    return normalized
