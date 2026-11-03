"""Portable retry evidence with explicit scope, clock and source provenance.

The envelope is inactive evidence. Consumers validate it against their archive
before using its guards; capture and restore never spend or refund a retry.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from bili_asr.artifact_inventory import require_regular_file
from bili_asr.artifact_packages import _open_regular
from bili_asr.storage_targets import unique_json_object

FORMAT = "retry-control/v2"
CLOCK = "unix-seconds-utc/v1"
MAX_BYTES = 8 * 1024**2
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_COUNTERS = {"retries", "last_retry_at", "next_retry_at", "max_retries"}
_LEDGER = _COUNTERS | {"last_error_code", "reserved_attempt_id", "dispatch_outcome"}
_HOLD_TEXT = {"reason", "source", "error_code", "investigation_ref", "repair_ref", "attempt_id"}
_HOLD_TIMES = {"held_at", "created_at", "updated_at"}


def encoded(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def digest(value: dict) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def _bounded_text(value) -> bool:
    return isinstance(value, str) and 0 < len(value.strip()) <= 2048 and "://" not in value and "\0" not in value


def _integer(value) -> bool:
    return type(value) is int and 0 <= value <= 2**63 - 1


def validate_legacy_typed(value: dict) -> None:
    """Keep the registered task-state v1 adapter's historical accepted shape."""
    if (not isinstance(value, dict) or set(value) != {"format", "schema_version", "retry_holds", "retry_ledger"}
            or value["format"] != "retry-control/v1" or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["retry_holds"], dict) or not isinstance(value["retry_ledger"], dict)):
        raise ValueError("unknown retry-control version or layout")
    for key, entry in value["retry_ledger"].items():
        if (not isinstance(key, str) or not key or not isinstance(entry, dict)
                or not {"retries", "last_retry_at"} <= set(entry) or not set(entry) <= _LEDGER
                or any(type(item) is not int or item < 0 for name, item in entry.items() if name in _COUNTERS)
                or any(not isinstance(entry[name], str) or not entry[name] for name in ("last_error_code", "reserved_attempt_id") if name in entry)
                or entry.get("dispatch_outcome", "reserved") not in {"reserved", "requeued", "stale"}):
            raise ValueError("invalid retry budget ledger entry")
    for key, entry in value["retry_holds"].items():
        if not isinstance(key, str) or not key or not isinstance(entry, (str, dict)):
            raise ValueError("invalid preserved retry hold")


def _projection(value: dict) -> dict:
    if not isinstance(value, dict) or not {"retry_holds", "retry_ledger"} <= set(value):
        raise ValueError("retry controls require holds and complete spent-budget ledger")
    result = {key: value[key] for key in ("retry_holds", "retry_ledger")}
    for entries in result.values():
        if (not isinstance(entries, dict) or len(entries) > 50000
                or any(not isinstance(key, str) or not _ID.fullmatch(key) for key in entries)):
            raise ValueError("invalid or oversized retry control identities")
    for entry in result["retry_ledger"].values():
        if (not isinstance(entry, dict) or not {"retries", "last_retry_at"} <= set(entry) or not set(entry) <= _LEDGER
                or any(not _integer(item) for name, item in entry.items() if name in _COUNTERS)
                or any(not isinstance(entry[name], str) or not _ID.fullmatch(entry[name]) for name in ("last_error_code", "reserved_attempt_id") if name in entry)
                or entry.get("dispatch_outcome", "reserved") not in {"reserved", "requeued", "stale"}):
            raise ValueError("unknown or invalid preserved retry budget ledger")
    for entry in result["retry_holds"].values():
        if isinstance(entry, str):
            if not _bounded_text(entry):
                raise ValueError("invalid preserved retry hold reason")
        elif (not isinstance(entry, dict) or not {"reason"} <= set(entry)
                or not set(entry) <= _HOLD_TEXT | _HOLD_TIMES | {"paused", "video_part_id", "transcript_id"}
                or any(not _bounded_text(item) for name, item in entry.items() if name in _HOLD_TEXT)
                or any(not _integer(item) for name, item in entry.items() if name in _HOLD_TIMES | {"video_part_id", "transcript_id"})
                or "paused" in entry and type(entry["paused"]) is not bool):
            raise ValueError("unknown or unsafe preserved retry hold fields")
    if len(encoded(result)) > MAX_BYTES:
        raise ValueError("retry controls exceed size limit")
    return result


def archive_scope(connection, database_sha256: str) -> dict:
    """Bind immutable job identities, allowing new jobs after the observation."""
    jobs = []
    columns = {row[1] for row in connection.execute("PRAGMA table_info(video_parts)")}
    platform = "v.platform,v.external_id" if "source_video_id" in columns else "'bilibili',vp.bvid"
    source_join = "LEFT JOIN source_videos v ON v.source_video_id=vp.source_video_id" if "source_video_id" in columns else ""
    query = ("SELECT j.job_id,j.kind,j.video_part_id,j.profile_id,j.dedupe_key,j.payload_json,j.created_at,"
             "vp.bvid,vp.cid,vp.page_index,p.config_sha256," + platform + " FROM workflow_jobs j "
             "LEFT JOIN video_parts vp USING(video_part_id) LEFT JOIN workflow_asr_profiles p USING(profile_id) "
             + source_join + " ORDER BY j.job_id")
    for row in connection.execute(query):
        jobs.append({"job_id": row[0], "identity_sha256": digest({"frozen_job": list(row)})})
        if len(jobs) > 50000:
            raise ValueError("retry archive scope exceeds bounded job inventory")
    if not jobs:
        raise ValueError("retry controls cannot bind an archive without workflow identities")
    return {"database_sha256": database_sha256, "jobs": jobs}


def validate_control(value: dict, *, connection=None, database_sha256=None, clock_domain=CLOCK, now=None) -> None:
    fields = {"format", "schema_version", "source", "projection_sha256", "archive_scope", "clock", "retry_holds", "retry_ledger", "content_sha256"}
    if (not isinstance(value, dict) or set(value) != fields or value["format"] != FORMAT
            or type(value["schema_version"]) is not int or value["schema_version"] != 2):
        raise ValueError("unknown portable retry control version or fields")
    source = value["source"]
    if (not isinstance(source, dict) or set(source) != {"format", "sha256"}
            or source["format"] not in {"production-retry-state/unversioned", "retry-control/v1", FORMAT}
            or not isinstance(source["sha256"], str) or not _HASH.fullmatch(source["sha256"])):
        raise ValueError("invalid original retry control digest")
    projection = _projection(value)
    if (value["projection_sha256"] != digest(projection)
            or value["content_sha256"] != digest({key: item for key, item in value.items() if key != "content_sha256"})):
        raise ValueError("retry control digest mismatch")
    clock = value["clock"]
    if (not isinstance(clock, dict) or set(clock) != {"domain", "captured_at"}
            or clock["domain"] != CLOCK or clock_domain != CLOCK or not _integer(clock["captured_at"])
            or now is not None and (not _integer(now) or now < clock["captured_at"])):
        raise ValueError("retry control clock domain or capture time mismatch")
    scope = value["archive_scope"]
    if (not isinstance(scope, dict) or set(scope) != {"database_sha256", "jobs"}
            or not isinstance(scope["database_sha256"], str) or not _HASH.fullmatch(scope["database_sha256"])
            or not isinstance(scope["jobs"], list) or not 1 <= len(scope["jobs"]) <= 50000):
        raise ValueError("invalid retry control archive scope")
    identities = {}
    for item in scope["jobs"]:
        if (not isinstance(item, dict) or set(item) != {"job_id", "identity_sha256"}
                or not isinstance(item["job_id"], str) or not _ID.fullmatch(item["job_id"])
                or not isinstance(item["identity_sha256"], str) or not _HASH.fullmatch(item["identity_sha256"])
                or item["job_id"] in identities):
            raise ValueError("invalid retry control job scope identity")
        identities[item["job_id"]] = item["identity_sha256"]
    if not (set(projection["retry_holds"]) | set(projection["retry_ledger"])) <= identities.keys():
        raise ValueError("retry control references jobs outside its archive scope")
    if database_sha256 is not None and scope["database_sha256"] != database_sha256:
        raise ValueError("retry control source database digest mismatch")
    if connection is not None:
        actual = {item["job_id"]: item["identity_sha256"] for item in archive_scope(connection, scope["database_sha256"])["jobs"]}
        if any(actual.get(identity) != fingerprint for identity, fingerprint in identities.items()):
            raise ValueError("retry control archive identity mismatch")


def seal_control(value: dict) -> None:
    """Seal an explicit owner's ledger update without changing its origin."""
    value["projection_sha256"] = digest(_projection(value))
    value["content_sha256"] = digest({key: item for key, item in value.items() if key != "content_sha256"})


def capture_control(path: Path, connection, *, database_sha256: str, captured_at: int) -> dict:
    require_regular_file(path)
    with _open_regular(path) as reader:
        raw = reader.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("retry controls exceed size limit")
    source = json.loads(raw, object_pairs_hook=unique_json_object)
    if not isinstance(source, dict):
        raise ValueError("retry controls must be an object")  # noqa: TRY004 - serialized input contract
    source_format = source.get("format", "production-retry-state/unversioned")
    if source_format == "retry-control/v1":
        validate_legacy_typed(source)
    elif source_format == FORMAT:
        validate_control(source, connection=connection, now=captured_at)
    elif source_format != "production-retry-state/unversioned" or "schema_version" in source or "clock_domain" in source:
        raise ValueError("unknown retry source version or clock domain")
    projection = _projection(source)
    value = {"format": FORMAT, "schema_version": 2,
             "source": {"format": source_format, "sha256": hashlib.sha256(raw).hexdigest()},
             "projection_sha256": digest(projection), "archive_scope": archive_scope(connection, database_sha256),
             "clock": {"domain": CLOCK, "captured_at": captured_at}, **projection}
    value["content_sha256"] = digest(value)
    validate_control(value, connection=connection, database_sha256=database_sha256)
    return value
