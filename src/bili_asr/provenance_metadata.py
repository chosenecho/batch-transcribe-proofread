"""Pure verification of frozen provider evidence and actual application choices."""
from __future__ import annotations

import hashlib
import json

from bili_asr.canonical_json import canonical
from bili_asr.metadata_acceptance import (
    CONTRACT,
    OPERATION,
    SourceFieldReceipt,
    acceptance_document,
    binding,
    binding_tuple,
    validate_frozen_acceptance,
)
from bili_asr.source_metadata import SourceMetadataSnapshot
from bili_asr.source_observations import PlatformObservation, validate_observation
from bili_asr.sources.bilibili_metadata_codec import (
    receipt_values,
    verify_source_receipt,
)
from bili_asr.sources.models import GatewayShapeError

OBSERVATION_KINDS = frozenset({"observation", "metadata-acceptance"})
_RECORD_KEYS = frozenset({"envelope_json", "payload_json", "envelope_sha256", "payload_sha256"})


def is_acceptance(node: dict) -> bool:
    """Recognize the original generic-node representation without changing it."""
    return (node["kind"] == "metadata-acceptance" or (node["kind"] == "observation"
            and json.loads(node["record"]["envelope_json"])["operation"] == OPERATION))


class MetadataEvidence:
    """Cache bounded graph records and shared normalizations for this verification."""

    def __init__(self, nodes: dict[str, dict]):
        self.nodes = nodes
        self.observations: dict[str, PlatformObservation] = {}
        self.identity_bindings: dict[str, tuple] = {}
        self.normalized: dict[tuple, tuple] = {}
        self.legacy_verified: set[str] = set()

    def observation(self, node: dict) -> PlatformObservation:
        key = node["sha256"]
        if key in self.observations:
            return self.observations[key]
        record = node["record"]
        expected = _RECORD_KEYS | {"owner", "codec"} if node["kind"] == "metadata-acceptance" else _RECORD_KEYS
        if set(record) != expected or (node["kind"] == "metadata-acceptance"
                and (record["owner"] != "application" or record["codec"] != CONTRACT)):
            raise ValueError("provenance: invalid observation owner or codec")
        observation = PlatformObservation(node["identity"], record["envelope_json"], record["payload_json"])
        validate_observation(observation)
        if (canonical(observation.envelope) != record["envelope_json"]
                or hashlib.sha256(record["envelope_json"].encode("utf-8")).hexdigest() != record["envelope_sha256"]
                or observation.envelope["payloadSha256"] != record["payload_sha256"]):
            raise ValueError("provenance: raw observation digest mismatch")
        identity_binding = binding_tuple(binding(observation))
        previous = self.identity_bindings.setdefault(observation.observation_id, identity_binding)
        if previous != identity_binding:
            raise ValueError("provenance: conflicting immutable observation identity")
        if node["kind"] == "metadata-acceptance" or is_acceptance(node):
            acceptance_document(observation)
        elif node["dependencies"]:
            raise ValueError("provenance: provider observation has unexpected dependencies")
        self.observations[key] = observation
        return observation

    def acceptance(self, node: dict, *, owner_mid: int | None = None,
                   legacy_sources: dict[str, dict] | None = None) -> dict:
        document = acceptance_document(self.observation(node))
        sources = {}
        for key in node["dependencies"]:
            source = self.nodes[key]
            if source["kind"] != "observation" or is_acceptance(source) or source["identity"] in sources:
                raise ValueError("provenance: acceptance needs exact provider dependencies")
            sources[source["identity"]] = source
        if node["kind"] == "observation" and not sources and legacy_sources is not None:
            # Older, unpublished v1 graphs used generic nodes. Their frozen
            # source closure supplies the exact provider edge recorded inside
            # the receipt; values and paths are still fully verified below.
            sources = {item["source"]["observationId"]: legacy_sources[item["source"]["observationId"]]
                       for item in document["fields"] if item["source"]["observationId"] in legacy_sources}
        expected_ids = {item["source"]["observationId"] for item in document["fields"]}
        if set(sources) != expected_ids:
            raise ValueError("provenance: acceptance original provider closure is missing or differs")
        for item in document["fields"]:
            source = self.observation(sources[item["source"]["observationId"]])
            key = (binding_tuple(binding(source)), item["normalizer"], canonical(item["context"]))
            try:
                if key not in self.normalized:
                    self.normalized[key] = receipt_values(source, item["normalizer"], item["context"])
                verify_source_receipt(SourceFieldReceipt(canonical(item)), source,
                    bvid=document["externalVideoId"], owner_mid=owner_mid, normalized=self.normalized[key])
            except (GatewayShapeError, ValueError) as error:
                raise ValueError("provenance: metadata acceptance differs from its provider source") from error
        if node["kind"] == "observation":
            self.legacy_verified.add(node["sha256"])
        return document

    def source(self, node: dict) -> None:
        record = node["record"]
        metadata = record["metadata"].get("source_metadata", {})
        frozen = SourceMetadataSnapshot.from_dict(metadata) if metadata else None
        bindings = metadata.get("observationBindings", [])
        if record["raw_observation_binding"] != ("verified_frozen" if bindings else "unavailable_unfrozen_history"):
            raise ValueError("provenance: raw observation availability differs from frozen evidence")
        observations = [self.nodes[key] for key in node["dependencies"]]
        if any(item["kind"] not in OBSERVATION_KINDS for item in observations):
            raise ValueError("provenance: unexpected source dependency kind")
        recorded = sorted(binding_tuple(binding(self.observation(item))) for item in observations)
        if recorded != sorted(binding_tuple(item) for item in bindings):
            raise ValueError("provenance: source observation dependencies differ from frozen bindings")
        providers = {item["identity"]: item for item in observations if not is_acceptance(item)}
        selected_fields = set()
        for item in observations:
            observation = self.observation(item)
            envelope = observation.envelope
            scope = envelope["scope"]
            matched_video = scope.get("externalVideoId") == metadata["externalVideoId"]
            if not matched_video and envelope["platform"] == "bilibili" and envelope["operation"] == "creator-page":
                payload = json.loads(observation.payload_json)
                listing = payload.get("list") if isinstance(payload, dict) else None
                page = listing.get("vlist") if isinstance(listing, dict) else listing
                matched_video = isinstance(page, list) and any(isinstance(value, dict)
                    and value.get("bvid") == metadata["externalVideoId"] for value in page)
            if (envelope["state"] != "success" or envelope["platform"] != metadata["platform"] or not matched_video
                    or (scope.get("partIndex") is not None and scope["partIndex"] != metadata["partIndex"])):
                raise ValueError("provenance: frozen observation belongs to another source")
            if not is_acceptance(item):
                continue
            document = acceptance_document(observation)
            validate_frozen_acceptance(document, frozen)
            self.acceptance(item, owner_mid=int(frozen.creator_id), legacy_sources=providers)
            for field in document["fields"]:
                if field["field"] in selected_fields:
                    raise ValueError("provenance: ambiguous frozen metadata acceptance field")
                selected_fields.add(field["field"])

    def require_legacy_sources(self) -> None:
        for node in self.nodes.values():
            if node["kind"] == "observation" and is_acceptance(node) and node["sha256"] not in self.legacy_verified:
                raise ValueError("provenance: legacy application acceptance lacks a verified frozen source")
