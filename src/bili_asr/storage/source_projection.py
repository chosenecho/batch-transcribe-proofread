"""Versioned YouTube field projections in the existing neutral observation table.

The JSON values are derived caches. Original responses remain in the optional
append-only store; a frozen binding is emitted only after verifying that receipt.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import json
import sqlite3
from typing import Any

from bili_asr.canonical_json import canonical, digest
from bili_asr.source_metadata import public_cover_url, valid_source_text
from bili_asr.source_observations import PlatformObservation
from bili_asr.storage.source_observations import require_observations, _validated_row

CONTRACT = "youtube-source-fields/v1"
_KEYS = {"title": "title", "desc": "description", "pic": "thumbnail", "tags": "tags", "pubdate": "timestamp"}
_KNOWN_STATES = {"present", "empty", "missing", "unavailable", "denied"}


def _value(field: str, value: Any) -> tuple[str, Any]:
    if value is None:
        return "missing", None
    if field == "tags":
        if (not isinstance(value, list) or len(value) > 256 or
                any(not valid_source_text(item, limit=256) or not item.strip() for item in value)):
            return "unavailable", None
        result = list(dict.fromkeys(value))
        return ("present" if result else "empty"), result
    if field == "pubdate":
        return ("present", value) if type(value) is int and 0 <= value <= 2**63 - 1 else ("unavailable", None)
    if not valid_source_text(value, limit=65536 if field == "desc" else 2048 if field == "pic" else 512,
                             multiline=field == "desc"):
        return "unavailable", None
    if value == "":
        return ("empty", "") if field == "desc" else ("unavailable", None)
    if field != "desc" and not value.strip():
        return "unavailable", None
    if field == "pic" and public_cover_url(value, platform="youtube", codec_version=2) is None:
        return "unavailable", None
    return "present", value


def store_youtube_fields(connection: sqlite3.Connection, observations: Iterable[PlatformObservation]) -> None:
    stored_raw = require_observations(connection)
    for observation in observations:
        envelope = observation.envelope
        if envelope["platform"] != "youtube" or envelope["operation"] != "details":
            continue
        payload = json.loads(observation.payload_json) if observation.payload_json is not None else {}
        payload = payload if isinstance(payload, dict) else {}
        for field, key in _KEYS.items():
            if envelope["state"] != "success":
                state, value = ("denied" if envelope["state"] == "denied" else "unavailable"), None
            else:
                state, value = _value(field, payload.get(key))
            success = state in {"present", "empty"}
            binding = ({"observationId": observation.observation_id, "payloadSha256": envelope["payloadSha256"],
                        "envelopeSha256": digest(envelope)} if stored_raw else None)
            projected = {"version": CONTRACT, "value": value, "observation": binding}
            stamp = envelope["observedAt"]
            connection.execute(
                """INSERT INTO source_metadata_observations VALUES ('youtube',?,'details',?,?,?,?,?,?)
                ON CONFLICT(platform,external_id,operation,field) DO UPDATE SET
                state=excluded.state,observed_at=excluded.observed_at,error_code=excluded.error_code,
                last_success_at=CASE WHEN excluded.last_success_at IS NULL
                    THEN source_metadata_observations.last_success_at ELSE excluded.last_success_at END,
                value_json=CASE WHEN excluded.last_success_at IS NULL
                    THEN source_metadata_observations.value_json ELSE excluded.value_json END""",
                (envelope["scope"]["externalVideoId"], field, state, stamp, stamp if success else None,
                 envelope["errorCode"], canonical(projected) if success else None))


@dataclass(frozen=True)
class YoutubeFields:
    description: str | None = None
    cover_url: str | None = None
    tags: tuple[str, ...] = ()
    tag_state: str | None = None
    details_observed_at: int | None = None
    bindings: tuple[tuple[str, str, str], ...] = ()
    observed_fields: tuple[str, ...] = ()


def read_youtube_fields_many(connection: sqlite3.Connection, external_ids: tuple[str, ...]) -> dict[str, YoutubeFields]:
    """Use one cache query and at most one original-receipt query per 256 parts."""
    if not external_ids:
        return {}
    if len(external_ids) > 256:
        raise ValueError("YouTube field batch is limited to 256 videos")
    marks = ",".join("?" for _ in external_ids)
    documents: dict[str, dict[str, Any]] = {identity: {} for identity in external_ids}
    receipts: dict[str, tuple[str, str, str]] = {}
    owners: dict[str, str] = {}
    expected_values: dict[str, list[tuple[str, Any, int]]] = {}
    has_raw = require_observations(connection)
    for row in connection.execute(
            f"SELECT * FROM source_metadata_observations WHERE platform='youtube' AND operation='details' "
            f"AND external_id IN ({marks}) ORDER BY external_id,field", external_ids):
        row = dict(row) if hasattr(row, "keys") else dict(zip(("platform", "external_id", "operation", "field",
                   "state", "observed_at", "last_success_at", "error_code", "value_json"), row, strict=True))
        identity, field, state = row["external_id"], row["field"], row["state"]
        if field not in _KEYS or state not in _KNOWN_STATES:
            raise ValueError("invalid YouTube field projection identity")
        values = documents[identity]
        values[field + "_state"] = state
        if row["value_json"] is None:
            continue
        projected = json.loads(row["value_json"])
        if (not isinstance(projected, dict) or set(projected) != {"version", "value", "observation"}
                or projected["version"] != CONTRACT or _value(field, projected["value"])[0] not in {"present", "empty"}
                or row["last_success_at"] is None):
            raise ValueError("invalid YouTube field projection value")
        values[field] = projected["value"]
        values[field + "_observed_at"] = row["last_success_at"]
        binding = projected["observation"]
        if binding is None:
            continue
        if (not has_raw or not isinstance(binding, dict) or set(binding) != {"observationId", "payloadSha256", "envelopeSha256"}):
            raise ValueError("YouTube field projection has unavailable original evidence")
        item = (binding["observationId"], binding["payloadSha256"], binding["envelopeSha256"])
        if any(not isinstance(value, str) for value in item):
            raise ValueError("invalid YouTube source receipt binding")
        if item[0] in receipts and (receipts[item[0]] != item or owners[item[0]] != identity):
            raise ValueError("YouTube source receipt is shared by different video projections")
        receipts[item[0]], owners[item[0]] = item, identity
        expected_values.setdefault(item[0], []).append((field, projected["value"], row["last_success_at"]))
        values.setdefault("bindings", set()).add(item)
    if receipts:
        receipt_marks = ",".join("?" for _ in receipts)
        found: set[str] = set()
        for row in connection.execute(f"SELECT * FROM platform_observations WHERE observation_id IN ({receipt_marks})", tuple(receipts)):
            observation = _validated_row(row)
            expected = receipts[observation.observation_id]
            envelope = observation.envelope
            if (envelope["platform"] != "youtube" or envelope["scope"].get("externalVideoId") != owners[observation.observation_id]
                    or envelope["operation"] != "details"
                    or envelope["state"] != "success"
                    or expected != (observation.observation_id, envelope["payloadSha256"], digest(envelope))):
                raise ValueError("YouTube source receipt digest or scope differs")
            payload = json.loads(observation.payload_json or "null")
            if (not isinstance(payload, dict) or any(stamp != envelope["observedAt"] or
                    _value(field, payload.get(_KEYS[field]))[1] != value
                    for field, value, stamp in expected_values[observation.observation_id])):
                raise ValueError("YouTube projected field differs from its original response")
            found.add(observation.observation_id)
        if found != receipts.keys():
            raise ValueError("YouTube source receipt is missing")
    return {identity: YoutubeFields(description=values.get("desc"), cover_url=values.get("pic"),
        tags=tuple(values.get("tags", ())), tag_state=values.get("tags_state"),
        details_observed_at=max((values[field + "_observed_at"] for field in ("desc", "pic", "tags")
                                 if field + "_observed_at" in values), default=None),
        bindings=tuple(sorted(values.get("bindings", ()))), observed_fields=tuple(field for field in _KEYS if field in values))
        for identity, values in documents.items()}
