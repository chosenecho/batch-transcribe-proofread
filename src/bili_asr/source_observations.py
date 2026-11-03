"""Immutable private platform observations and versioned offline projections.

Payload keys belong to the platform. Transport credentials are excluded at
the capture boundary with explicit JSON pointers; unknown video fields remain
unchanged, including nested values, nulls and empty collections.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
from uuid import uuid4
from typing import Any, cast

from bili_asr.canonical_json import canonical, digest
from bili_asr.error_codes import validate_error_code
from bili_asr.source_metadata import valid_source_text

CONTRACT = "source-observations/v1"
PROJECTION = "source-superset/v1"
MAX_PAYLOAD_BYTES = 32 * 1024 * 1024
_TRANSPORT_KEYS = frozenset({"http_headers", "request_headers", "cookie", "cookies",
                            "authorization", "proxy", "password", "api_key", "access_token"})


def _pointer(path: str, key: str) -> str:
    return path + "/" + key.replace("~", "~0").replace("/", "~1")


def _metadata_payload(value: Any, *, path: str = "", exclusions: list[str] | None = None) -> Any:
    if exclusions is None:
        exclusions = []
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("platform JSON object keys must be strings")
            location = _pointer(path, key)
            if key.casefold() in _TRANSPORT_KEYS:
                exclusions.append(location)
            else:
                result[key] = _metadata_payload(item, path=location, exclusions=exclusions)
        return result
    if isinstance(value, list):
        return [_metadata_payload(item, path=path + "/" + str(index), exclusions=exclusions)
                for index, item in enumerate(value)]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise ValueError("platform payload must contain JSON values only")


@dataclass(frozen=True, slots=True)
class PlatformObservation:
    observation_id: str
    envelope_json: str
    payload_json: str | None

    @classmethod
    def capture(cls, *, platform: str, operation: str, endpoint: str, scope: dict[str, Any],
                extractor: str, extractor_version: str, payload: Any = None,
                state: str = "success", error_code: str | None = None,
                observed_at: int | None = None) -> "PlatformObservation":
        if platform not in {"bilibili", "youtube"}:
            raise ValueError("unsupported observation platform")
        if state not in {"success", "failed", "denied"}:
            raise ValueError("invalid observation state")
        if state == "success" and error_code is not None:
            raise ValueError("successful observation cannot carry an error")
        if state != "success":
            validate_error_code(error_code)
        for value in (operation, endpoint, extractor, extractor_version):
            if not valid_source_text(value, limit=512) or not value.strip():
                raise ValueError("invalid observation acquisition identity")
        stamp = int(time.time()) if observed_at is None else observed_at
        if type(stamp) is not int or not 0 <= stamp <= 2**63 - 1:
            raise ValueError("invalid observation timestamp")
        if (not isinstance(scope, dict) or not scope or
                any(key not in {"externalVideoId", "partIndex", "creatorId", "pageNumber", "pageSize"}
                    for key in scope)):
            raise ValueError("invalid observation scope")
        if "externalVideoId" not in scope and "creatorId" not in scope:
            raise ValueError("observation needs a video or creator identity")
        for key, value in scope.items():
            if key in {"externalVideoId", "creatorId"}:
                if not valid_source_text(value, limit=128) or not value.strip():
                    raise ValueError("invalid observation scope identity")
            elif type(value) is not int or value < (0 if key == "partIndex" else 1):
                raise ValueError("invalid observation scope range")
        exclusions: list[str] = []
        raw = None
        if state == "success" or payload is not None:
            retained = _metadata_payload(payload, exclusions=exclusions)
            raw = json.dumps(retained, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            if len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
                raise ValueError("platform observation exceeds payload budget")
        envelope = {"version": 1, "platform": platform, "operation": operation,
                    "endpoint": endpoint, "scope": scope, "observedAt": stamp,
                    "extractor": {"name": extractor, "version": extractor_version},
                    "adapterVersion": CONTRACT, "state": state, "errorCode": error_code,
                    "payloadSha256": None if raw is None else hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                    "payloadEncoding": "json-utf8", "excludedTransportPaths": exclusions,
                    "captureCoverage": "metadata-domain"}
        return cls(uuid4().hex, canonical(envelope), raw)

    @property
    def envelope(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.envelope_json))

    def to_dict(self) -> dict[str, Any]:
        return {"observationId": self.observation_id, "envelope": self.envelope,
                "envelopeSha256": digest(self.envelope),
                "payload": None if self.payload_json is None else json.loads(self.payload_json)}


def validate_observation(observation: PlatformObservation) -> None:
    """Validate an external or stored receipt without changing its raw payload."""
    if (not isinstance(observation, PlatformObservation) or len(observation.observation_id) != 32
            or any(character not in "0123456789abcdef" for character in observation.observation_id)):
        raise ValueError("invalid platform observation ID")
    envelope = observation.envelope
    expected = {"version", "platform", "operation", "endpoint", "scope", "observedAt", "extractor",
                "adapterVersion", "state", "errorCode", "payloadSha256", "payloadEncoding",
                "excludedTransportPaths", "captureCoverage"}
    if (not isinstance(envelope, dict) or set(envelope) != expected or type(envelope["version"]) is not int
            or envelope["version"] != 1 or envelope["adapterVersion"] != CONTRACT
            or envelope["payloadEncoding"] != "json-utf8" or envelope["captureCoverage"] != "metadata-domain"
            or not isinstance(envelope["extractor"], dict) or set(envelope["extractor"]) != {"name", "version"}
            or not isinstance(envelope["excludedTransportPaths"], list)
            or any(not isinstance(path, str) or not path.startswith("/") for path in envelope["excludedTransportPaths"])):
        raise ValueError("invalid platform observation envelope")
    raw = observation.payload_json
    payload = None if raw is None else json.loads(raw)
    # Revalidate the domain and bounded scope using the capture validator.
    candidate = PlatformObservation.capture(platform=envelope["platform"], operation=envelope["operation"],
        endpoint=envelope["endpoint"], scope=envelope["scope"], extractor=envelope["extractor"]["name"],
        extractor_version=envelope["extractor"]["version"], payload=payload, state=envelope["state"],
        error_code=envelope["errorCode"], observed_at=envelope["observedAt"])
    if candidate.payload_json != raw:
        raise ValueError("platform payload includes forbidden transport fields or noncanonical JSON")
    if envelope["payloadSha256"] != (None if raw is None else hashlib.sha256(raw.encode("utf-8")).hexdigest()):
        raise ValueError("platform observation payload digest differs")


def project_superset(observations: tuple[PlatformObservation, ...], *, platform: str,
                     external_video_id: str, mapping_version: str = PROJECTION) -> dict[str, Any]:
    """Reproject retained observations offline; absent values never clear facts."""
    if mapping_version not in {PROJECTION, "source-superset/v2"}:
        raise ValueError("unsupported superset mapping version")
    if platform not in {"bilibili", "youtube"} or not valid_source_text(external_video_id, limit=128):
        raise ValueError("invalid superset source identity")
    values: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    states: dict[str, str] = {}
    namespace: dict[str, Any] = {}
    coverage: list[dict[str, Any]] = []
    for observation in observations:
        envelope = observation.envelope
        # Application acceptance nodes are private selection evidence, never
        # provider payloads or acquisition coverage. Their own codec verifies
        # the distinct application owner before any provenance use.
        if envelope["operation"] == "accepted-metadata":
            from bili_asr.metadata_acceptance import acceptance_document
            acceptance_document(observation)
            continue
        if envelope["platform"] != platform:
            raise ValueError("mixed platform observation projection")
        if envelope["scope"].get("externalVideoId", external_video_id) != external_video_id:
            raise ValueError("mixed video observation projection")
        coverage.append({"observationId": observation.observation_id, "operation": envelope["operation"],
                         "scope": envelope["scope"], "state": envelope["state"],
                         "errorCode": envelope["errorCode"], "observedAt": envelope["observedAt"],
                         "excludedTransportPaths": envelope["excludedTransportPaths"]})
        if envelope["state"] != "success":
            continue
        if observation.payload_json is None:
            raise ValueError("successful observation has no raw payload")
        payload = json.loads(observation.payload_json)
        namespace[observation.observation_id] = payload
        base = ""
        document = payload
        if envelope["operation"] == "creator-page":
            listing = payload.get("list") if isinstance(payload, dict) else None
            page = listing.get("vlist", []) if isinstance(listing, dict) else []
            page = page if isinstance(page, list) else []
            match = next(((index, item) for index, item in enumerate(page)
                          if isinstance(item, dict) and item.get("bvid") == external_video_id), None)
            if match is None:
                continue
            index, document = match
            base = f"/list/vlist/{index}"
        if not isinstance(document, dict):
            if envelope["operation"] == "tags" and isinstance(document, list):
                values["tags"] = [item["tag_name"] for item in document
                                  if isinstance(item, dict) and isinstance(item.get("tag_name"), str)]
                states["tags"] = "empty" if not values["tags"] else "present"
                provenance["tags"] = {"observationId": observation.observation_id, "jsonPointer": "",
                                      "mapping": "bilibili-tag-names/v1"}
            continue
        mappings: tuple[tuple[str, str], ...]
        if platform == "youtube":
            mappings = (("title", "title"), ("description", "description"), ("coverUrl", "thumbnail"),
                        ("tags", "tags"), ("creatorId", "channel_id"), ("creatorName", "channel"),
                        ("durationSeconds", "duration"), ("originalLanguage", "language"),
                        ("sourcePublishedAt", "timestamp"))
        elif envelope["operation"] == "creator-page":
            mappings = (("title", "title"), ("description", "description"), ("coverUrl", "pic"),
                        ("bilibiliCategoryId", "typeid"), ("sourcePublishedAt", "created"), ("aid", "aid"))
        else:
            mappings = (("title", "title"), ("description", "desc"), ("coverUrl", "pic"),
                        ("bilibiliCategoryId", "tid"), ("sourcePublishedAt", "pubdate"), ("aid", "aid"))
        for field, key in mappings:
            if key not in document:
                states.setdefault(field, "missing")
                continue
            value = document[key]
            state = "null" if value is None else "empty" if value in ("", []) else "present"
            states[field] = state
            # A null means the provider supplied no usable value; explicit
            # empty prose/list is an observation that clears the prior value.
            if value is None:
                continue
            values[field] = value
            provenance[field] = {"observationId": observation.observation_id,
                                 "jsonPointer": _pointer(base, key), "mapping": mapping_version}
        if mapping_version == "source-superset/v2" and platform == "youtube":
            duration = document.get("duration")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool) and 0 < duration < 2**53:
                values["durationMs"] = round(duration * 1000)
                provenance["durationMs"] = {"observationId": observation.observation_id,
                    "jsonPointer": "/duration", "mapping": "seconds-to-milliseconds/v2"}
    expected_operations = ("details",) if platform == "youtube" else ("creator-page", "details", "parts", "tags")
    operations = {operation: "not-requested" for operation in expected_operations}
    for entry in coverage:
        operations[entry["operation"]] = entry["state"]
    return {"version": 2 if mapping_version.endswith("v2") else 1,
            "mappingVersion": mapping_version, "platform": platform,
            "externalVideoId": external_video_id, "fields": values, "fieldStates": states,
            "provenance": provenance, "platformFields": {platform: namespace},
            "coverage": {"observations": coverage, "operations": operations,
                         "gaps": [name for name, state in operations.items() if state != "success"],
                         "rawHistory": "available" if coverage else "unavailable",
                         "requestedObservationsSucceeded": bool(coverage) and all(
                             entry["state"] == "success" for entry in coverage),
                         "complete": False, "limit": "only explicitly requested endpoints and ranges"}}
