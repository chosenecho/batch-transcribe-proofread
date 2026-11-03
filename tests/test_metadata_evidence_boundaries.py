"""Private metadata evidence rejects invalid owners, shapes and cache claims."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest

from bili_asr.canonical_json import canonical
from bili_asr.metadata_acceptance import (
    CONTRACT,
    SourceFieldReceipt,
    acceptance_document,
    binding,
    binding_tuple,
    unpack_cache,
    validate_frozen_acceptance,
)
from bili_asr.platform_identity import ContentRef
from bili_asr.source_metadata import SourceMetadataSnapshot, public_cover_url
from bili_asr.source_observations import (
    PlatformObservation,
    project_superset,
    validate_observation,
)
from bili_asr.sources.models import TagRead, VideoPart, VideoSummary
from bili_asr.storage.observation_rows import (
    append_observations,
    install_observations_in_staged_copy,
    require_observations,
)
from bili_asr.storage.schema_contract import SchemaContractError

BVID = "BV1DETAILSD0"


def observation(**overrides):
    options = {"platform": "bilibili", "operation": "details", "endpoint": "fixture/source",
        "scope": {"externalVideoId": BVID}, "extractor": "fixture", "extractor_version": "1",
        "payload": {"title": "Original"}, "observed_at": 17}
    return PlatformObservation.capture(**(options | overrides))


def receipt(raw):
    return SourceFieldReceipt.create(field="title", value="Original", observation=raw,
        pointer="/title", normalizer="bilibili-details/v1", context={"ownerMid": 42})


@pytest.mark.parametrize("overrides", [
    {"platform": "unsupported"}, {"state": "unknown"}, {"error_code": "transport_error"},
    {"operation": " "}, {"observed_at": True}, {"scope": {}}, {"scope": {"partIndex": 0}},
    {"scope": {"externalVideoId": " "}}, {"scope": {"externalVideoId": BVID, "pageNumber": 0}},
    {"scope": {"externalVideoId": BVID, "arbitrary": 1}},
    {"payload": {1: "non-string key"}}, {"payload": {"title": object()}},
])
def test_capture_refuses_untrustworthy_acquisition_identity_and_non_json_payload(overrides):
    with pytest.raises(ValueError):
        observation(**overrides)


def test_capture_limits_bytes_and_retains_failed_response_without_transport_secrets(monkeypatch):
    monkeypatch.setattr("bili_asr.source_observations.MAX_PAYLOAD_BYTES", 30)
    with pytest.raises(ValueError, match="payload budget"):
        observation(payload={"title": "x" * 31})
    raw = observation(state="failed", error_code="response_error", payload={"cookie": "SECRET", "code": -1})
    validate_observation(raw)
    assert raw.to_dict()["payload"] == {"code": -1}
    assert raw.envelope["excludedTransportPaths"] == ["/cookie"]
    assert raw.envelope["state"] == "failed"


@pytest.mark.parametrize("damage", ["id", "envelope", "payload_digest", "transport", "pretty_json"])
def test_external_receipt_rejects_tampered_or_ambiguous_original_bytes(damage):
    raw = observation()
    if damage == "id":
        raw = replace(raw, observation_id="g" * 32)
    elif damage == "envelope":
        raw = replace(raw, envelope_json=canonical(raw.envelope | {"captureCoverage": "complete"}))
    elif damage == "payload_digest":
        raw = replace(raw, envelope_json=canonical(raw.envelope | {"payloadSha256": "a" * 64}))
    else:
        payload = '{"cookie":"SECRET","title":"Original"}' if damage == "transport" else '{"title": "Original"}'
        envelope = raw.envelope | {"payloadSha256": hashlib.sha256(payload.encode()).hexdigest()}
        raw = replace(raw, payload_json=payload, envelope_json=canonical(envelope))
    with pytest.raises(ValueError):
        validate_observation(raw)


@pytest.mark.parametrize("damage", ["mapping", "identity", "platform", "video", "absent_payload"])
def test_superset_refuses_cross_source_or_unavailable_success_receipts(damage):
    raw = observation()
    options = {"platform": "bilibili", "external_video_id": BVID}
    if damage == "mapping":
        options["mapping_version"] = "unknown/v9"
    elif damage == "identity":
        options["platform"] = "unsupported"
    elif damage == "platform":
        raw = observation(platform="youtube")
    elif damage == "video":
        raw = observation(scope={"externalVideoId": "BV1ANOTHER00"})
    else:
        raw = replace(raw, payload_json=None)
    with pytest.raises(ValueError):
        project_superset((raw,), **options)


def test_offline_tag_and_creator_page_projections_keep_original_namespaces():
    tags = observation(operation="tags", payload=[{"tag_name": "one"}, {"tag_name": 2}, None])
    unrelated = observation(operation="creator-page", scope={"creatorId": "42", "pageNumber": 1},
                            payload={"list": {"vlist": [{"bvid": "BV1ANOTHER00", "title": "Other"}]}})
    report = project_superset((unrelated, tags), platform="bilibili", external_video_id=BVID)
    assert report["fields"] == {"tags": ["one"]}
    assert report["provenance"]["tags"]["observationId"] == tags.observation_id
    assert report["platformFields"]["bilibili"][unrelated.observation_id]["list"]["vlist"][0]["title"] == "Other"


@pytest.mark.parametrize("damage", ["missing-key", "bad-id", "bad-digest"])
def test_acceptance_binding_has_a_closed_immutable_identity(damage):
    value = binding(observation())
    if damage == "missing-key":
        value.pop("envelopeSha256")
    else:
        value["observationId" if damage == "bad-id" else "payloadSha256"] = "BAD"
    with pytest.raises(ValueError, match="binding|digest or ID"):
        binding_tuple(value)


@pytest.mark.parametrize("damage", ["unknown-field", "pointer-limit", "context-limit", "noncanonical", "cache-value", "cache-version"])
def test_receipt_and_cache_refuse_resealed_invalid_selection_claims(damage):
    raw = observation()
    selected = receipt(raw).document
    if damage.startswith("cache"):
        cache = {"version": CONTRACT, "value": "Original", "receipt": selected, "acceptance": binding(raw)}
        cache["value" if damage == "cache-value" else "version"] = "different"
        with pytest.raises(ValueError, match="acceptance cache"):
            unpack_cache(canonical(cache))
        return
    if damage == "unknown-field":
        selected["field"] = "unknown"
    elif damage == "pointer-limit":
        selected["jsonPointer"] = "/" + "x" * 512
    elif damage == "context-limit":
        selected["context"] = {"unbounded": "x" * 4096}
    serialized = json.dumps(selected) if damage == "noncanonical" else canonical(selected)
    with pytest.raises(ValueError, match="source field receipt"):
        SourceFieldReceipt(serialized)


def test_acceptance_owner_scope_and_frozen_source_binding_are_required():
    raw = observation()
    selected = receipt(raw)
    document = {"version": CONTRACT, "externalVideoId": BVID, "fields": [selected.document]}
    with pytest.raises(ValueError, match="application metadata acceptance"):
        acceptance_document(observation(payload=document))
    source = SourceMetadataSnapshot(ContentRef("bilibili", BVID), "Original", "42", "Creator", 17, 17, codec_version=2)
    with pytest.raises(ValueError, match="missing original source binding"):
        validate_frozen_acceptance(document, source)
    source = replace(source, observation_bindings=(binding_tuple(binding(raw)),))
    validate_frozen_acceptance(document, source)
    with pytest.raises(ValueError, match="payload"):
        validate_frozen_acceptance(document | {"extra": "claim"}, source)
    with pytest.raises(ValueError, match="owner differs"):
        validate_frozen_acceptance(document, replace(source, creator_id="43"))


@pytest.mark.parametrize("factory", [
    lambda: VideoSummary(BVID, 1, "Title", 17, 42, desc="bad\x00body"),
    lambda: VideoSummary(BVID, 1, "Title", 17, 42, source_receipts=(object(),)),
    lambda: VideoPart(BVID, 0, 1, "Part", 1000, source_receipt=object()),
    lambda: TagRead((), source_receipt=object()),
    lambda: TagRead(None, "transport_error", receipt(observation())),
])
def test_dto_cannot_advertise_untyped_or_failed_selection_evidence(factory):
    with pytest.raises((TypeError, ValueError)):
        factory()


def test_optional_receipts_preserve_legacy_unknown_and_do_not_change_dto_equality():
    summary = VideoSummary(BVID, 1, "Title", 17, 42)
    assert summary == replace(summary, source_receipts=(receipt(observation()),))
    assert VideoPart(BVID, 0, 1, "Part", 1000).source_receipt is None
    assert TagRead(()).source_receipt is None
    assert unpack_cache('"legacy value"') == ("legacy value", None)


def test_observation_installation_is_explicit_and_rejects_transaction_or_duplicate_contract():
    with sqlite3.connect(":memory:") as connection:
        assert require_observations(connection) is False
        with pytest.raises(SchemaContractError, match="explicit observation upgrade"):
            require_observations(connection, required=True)
        connection.execute("BEGIN")
        with pytest.raises(ValueError, match="stopped staged copy"):
            install_observations_in_staged_copy(connection)
        connection.rollback()
        install_observations_in_staged_copy(connection)
        with pytest.raises(ValueError, match="already installed"):
            install_observations_in_staged_copy(connection)


def test_schema_marker_corruption_is_detected_even_when_guard_sql_is_restored():
    with sqlite3.connect(":memory:") as connection:
        install_observations_in_staged_copy(connection)
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='platform_observation_contract_no_delete'").fetchone()[0]
        connection.execute("DROP TRIGGER platform_observation_contract_no_delete")
        connection.execute("DELETE FROM platform_observation_contract")
        connection.execute(sql)
        with pytest.raises(SchemaContractError, match="altered contract marker"):
            require_observations(connection)


def test_raw_identity_collision_keeps_the_first_immutable_payload():
    with sqlite3.connect(":memory:") as connection:
        install_observations_in_staged_copy(connection)
        original = observation()
        append_observations(connection, (original, original))
        collision = replace(observation(payload={"title": "Different"}), observation_id=original.observation_id)
        with pytest.raises(ValueError, match="ID already has different evidence"):
            append_observations(connection, (collision,))
        assert connection.execute("SELECT payload_json FROM platform_observations").fetchone()[0] == original.payload_json


def test_source_snapshot_rejects_invalid_shape_identity_and_oversized_binding_closure():
    source = SourceMetadataSnapshot(ContentRef("bilibili", BVID), "Title", "42", "Creator", 17, 17, codec_version=2)
    with pytest.raises(ValueError, match="ContentRef"):
        replace(source, ref="untyped")
    with pytest.raises(ValueError, match="codec version"):
        replace(source, codec_version=True)
    with pytest.raises(ValueError, match="bindings"):
        replace(source, observation_bindings=tuple((f"{index:032x}", "a" * 64, "b" * 64) for index in range(257)))
    value = source.to_dict()
    with pytest.raises(ValueError, match="bindings"):
        SourceMetadataSnapshot.from_dict(value | {"observationBindings": [{"observationId": "a" * 32}]})
    with pytest.raises(ValueError, match="unknown source metadata fields"):
        SourceMetadataSnapshot.from_dict(value | {"extra": "undeclared"})
    with pytest.raises(ValueError, match="shape"):
        SourceMetadataSnapshot.from_dict(value | {"partIndex": -1})
    assert public_cover_url("https://i.ytimg.com:invalid/image.jpg", platform="youtube", codec_version=2) is None
