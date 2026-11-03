"""Private application receipts for the metadata values actually accepted.

This codec is independent of the network SDK and database schema. A receipt
links an actual normalized DTO value to one immutable provider observation;
an acceptance node preserves that selection after the mutable cache changes.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bili_asr.canonical_json import canonical, digest

if TYPE_CHECKING:
    from bili_asr.source_observations import PlatformObservation

CONTRACT = "metadata-acceptance/v1"
OPERATION = "accepted-metadata"
ENDPOINT = "bili-asr/metadata/acceptance"
FIELD_OPERATIONS = {"title": "summary", "pubdate": "summary", "aid": "summary",
                    "pic": "details", "desc": "details", "tid": "details",
                    "parts": "parts", "tags": "tags"}
NORMALIZERS = frozenset({"bilibili-summary/v1", "bilibili-details/v1",
                        "bilibili-completed-aid/v1", "bilibili-parts/v1", "bilibili-tags/v1"})
_SOURCE_KEYS = frozenset({"field", "value", "source", "jsonPointer", "normalizer", "context"})
_BINDING_KEYS = frozenset({"observationId", "payloadSha256", "envelopeSha256"})


def binding(observation: PlatformObservation) -> dict[str, str]:
    envelope = observation.envelope
    return {"observationId": observation.observation_id,
            "payloadSha256": envelope["payloadSha256"], "envelopeSha256": digest(envelope)}


def binding_tuple(value: object) -> tuple[str, str, str]:
    if not isinstance(value, dict) or set(value) != _BINDING_KEYS:
        raise ValueError("invalid metadata receipt binding")
    result = tuple(value[key] for key in ("observationId", "payloadSha256", "envelopeSha256"))
    if any(not isinstance(item, str) or re.fullmatch("[0-9a-f]{" + str(length) + "}", item) is None
           for item, length in zip(result, (32, 64, 64), strict=True)):
        raise ValueError("invalid metadata receipt digest or ID")
    return result


@dataclass(frozen=True, slots=True)
class SourceFieldReceipt:
    """A successful gateway DTO's input selection, never an inferred latest row."""

    document_json: str

    def __post_init__(self) -> None:
        document = json.loads(self.document_json)
        if (not isinstance(document, dict) or set(document) != _SOURCE_KEYS
                or not isinstance(document["field"], str) or document["field"] not in FIELD_OPERATIONS
                or not isinstance(document["normalizer"], str) or document["normalizer"] not in NORMALIZERS
                or not isinstance(document["jsonPointer"], str) or len(document["jsonPointer"]) > 512
                or not isinstance(document["context"], dict) or len(canonical(document["context"])) > 4096
                or canonical(document) != self.document_json):
            raise ValueError("invalid accepted source field receipt")
        binding_tuple(document["source"])

    @classmethod
    def create(cls, *, field: str, value: Any, observation: PlatformObservation,
               pointer: str, normalizer: str, context: dict) -> SourceFieldReceipt:
        return cls(canonical({"field": field, "value": value, "source": binding(observation),
                              "jsonPointer": pointer, "normalizer": normalizer, "context": context}))

    @property
    def document(self) -> dict:
        return json.loads(self.document_json)

    @property
    def field(self) -> str:
        return self.document["field"]


def unpack_cache(raw: str | None) -> tuple[Any, dict | None]:
    """Old scalar/array JSON remains readable and carries unknown selection."""
    if raw is None:
        return None, None
    value = json.loads(raw)
    if not isinstance(value, dict):
        return value, None
    if (set(value) != {"version", "value", "receipt", "acceptance"} or value["version"] != CONTRACT
            or value["value"] != SourceFieldReceipt(canonical(value["receipt"])).document["value"]):
        raise ValueError("invalid metadata acceptance cache")
    binding_tuple(value["acceptance"])
    return value["value"], value


def acceptance_document(observation: PlatformObservation) -> dict:
    """Validate the application owner and closed acceptance payload vocabulary."""
    envelope = observation.envelope
    payload = json.loads(observation.payload_json or "null")
    if (envelope["platform"] != "bilibili" or envelope["operation"] != OPERATION
            or envelope["endpoint"] != ENDPOINT or envelope["extractor"] != {"name": "bili-asr", "version": CONTRACT}
            or envelope["state"] != "success" or set(envelope["scope"]) != {"externalVideoId"}
            or not isinstance(payload, dict) or set(payload) != {"version", "externalVideoId", "fields"}
            or payload["version"] != CONTRACT or payload["externalVideoId"] != envelope["scope"]["externalVideoId"]
            or not isinstance(payload["fields"], list) or len(payload["fields"]) != 1):
        raise ValueError("invalid application metadata acceptance")
    fields = tuple(SourceFieldReceipt(canonical(item)) for item in payload["fields"])
    if len({item.field for item in fields}) != len(fields):
        raise ValueError("duplicate accepted metadata field")
    return payload


def validate_frozen_acceptance(document: dict, source) -> None:
    """Match selected private DTO facts to the frozen public source projection."""
    from bili_asr.source_metadata import SourceMetadataSnapshot, public_cover_url
    if (not isinstance(source, SourceMetadataSnapshot) or source.ref.platform != "bilibili"
            or source.codec_version != 2 or document.get("externalVideoId") != source.ref.external_video_id
            or source.creator_id is None or not source.creator_id.isdecimal()):
        raise ValueError("frozen metadata acceptance scope or owner differs")
    if (document.get("version") != CONTRACT or set(document) != {"version", "externalVideoId", "fields"}
            or not isinstance(document["fields"], list) or len(document["fields"]) != 1):
        raise ValueError("invalid frozen metadata acceptance payload")
    item = SourceFieldReceipt(canonical(document["fields"][0])).document
    if binding_tuple(item["source"]) not in source.observation_bindings:
        raise ValueError("frozen metadata acceptance is missing original source binding")
    owner_mid = item["context"].get("ownerMid")
    if owner_mid is not None and str(owner_mid) != source.creator_id:
        raise ValueError("frozen metadata owner differs from its immutable acceptance")
    source_fields = {"title": source.title, "pubdate": source.pubdate, "aid": source.aid,
                     "desc": source.description, "tid": source.category_id}
    field, value = item["field"], item["value"]
    if field in source_fields and value != source_fields[field]:
        raise ValueError("frozen metadata field differs from its immutable acceptance")
    if field == "pic" and public_cover_url(value, platform="bilibili", codec_version=2) != source.cover_url:
        raise ValueError("frozen cover differs from its immutable acceptance")
    if field == "tags" and tuple(dict.fromkeys(tag["tagName"] for tag in value)) != source.tags:
        raise ValueError("frozen tags differ from their immutable acceptance")
    if field == "parts":
        matching = [part for part in value if part["pageIndex"] == source.ref.part_index]
        if (len(matching) != 1 or matching[0]["title"] != source.part_title
                or matching[0]["durationMs"] != source.duration_ms):
            raise ValueError("frozen part differs from its immutable acceptance")
