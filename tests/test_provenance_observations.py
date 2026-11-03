"""Only frozen exact raw observation bindings establish manuscript history."""
from __future__ import annotations

import json
from contextlib import closing
from copy import deepcopy

import pytest

from bili_asr.provenance_contract import read_graph, verify_graph, write_graph
from bili_asr.provenance_report import report_graph
from bili_asr.services.provenance import public_summary, trace
from bili_asr.source_observations import PlatformObservation
from bili_asr.storage.source_observations import ObservationRepository
from tests.test_provenance_trace import _rehash, manuscript

pytestmark = pytest.mark.usefixtures("enable_content_publication_for_tests")


@pytest.mark.parametrize(("platform", "operation"), [("youtube", "details"), ("bilibili", "details"), ("bilibili", "creator-page")])
def test_frozen_receipt_preserves_raw_unknown_fields_and_never_uses_later_observations(tmp_path, platform, operation):
    connection, root, _revision, _edition, release, _producers = manuscript(tmp_path, platform, raw_kind=operation)
    with closing(connection):
        graph = trace(connection, release_id=release["release_id"], artifact_roots=(root,))
        report = report_graph(graph)
        facts = report["revisions"][0]
        assert facts["source_metadata"]["raw_observation_binding"] == "verified_frozen"
        assert facts["metadata_selection"] == {"state": "unknown", "fields": []}
        assert facts["application_acceptance_digests"] == []
        assert len(facts["raw_observation_digests"]) == 1
        source = facts["source_metadata"]["metadata"]["source_metadata"]
        assert source["version"] == "source-metadata-v2" and source["observationBindings"]
        receipt = next(node for node in graph["nodes"] if node["kind"] == "observation")
        payload = json.loads(receipt["record"]["payload_json"])
        original = payload["list"]["vlist"][0] if operation == "creator-page" else payload
        assert original["private_unknown_field"] == {"nested": [True, None, 1, "exact 原始值"]}
        summary = public_summary(connection, release["release_id"], (root,))
        assert "private_unknown_field" not in str(summary) and "exact 原始值" not in str(summary)
        frozen = connection.execute("SELECT prepared_json FROM editorial_inputs").fetchone()[0]
        newer = PlatformObservation.capture(platform=platform, operation="details", endpoint="fixture/newer",
            scope={"externalVideoId": source["externalVideoId"], "partIndex": source["partIndex"]},
            extractor="test", extractor_version="2", payload={"title": "Later mutable title", "unknown": "new"}, observed_at=99)
        with connection:
            ObservationRepository(connection).append((newer,))
            connection.execute("UPDATE source_videos SET title='Later mutable title' WHERE platform=? AND external_id=?",
                               (platform, source["externalVideoId"]))
            if platform == "bilibili":
                connection.execute("UPDATE videos SET title='Later mutable title' WHERE bvid=?", (source["externalVideoId"],))
        assert connection.execute("SELECT prepared_json FROM editorial_inputs").fetchone()[0] == frozen
        assert trace(connection, release_id=release["release_id"], artifact_roots=(root,)) == graph
        assert newer.observation_id not in str(graph)
        path = tmp_path / "raw-chain.json"
        write_graph(path, graph)
    assert report_graph(read_graph(path)) == report


def test_resealed_raw_receipt_still_requires_its_original_payload_digest(tmp_path):
    connection, root, _revision, _edition, release, _producers = manuscript(tmp_path, "youtube", raw_kind="details")
    with closing(connection):
        graph = trace(connection, release_id=release["release_id"], artifact_roots=(root,))
    altered = deepcopy(graph)
    observation = next(node for node in altered["nodes"] if node["kind"] == "observation")
    observation["record"]["payload_json"] = '{}'
    with pytest.raises(ValueError, match="digest|observation|provenance"):
        verify_graph(_rehash(altered))


def test_resealed_source_cannot_claim_frozen_raw_history_without_bindings(tmp_path):
    connection, root, _revision, _edition, release, _producers = manuscript(tmp_path, "youtube")
    with closing(connection):
        graph = trace(connection, release_id=release["release_id"], artifact_roots=(root,))
    altered = deepcopy(graph)
    source = next(node for node in altered["nodes"] if node["kind"] == "source")
    source["record"]["raw_observation_binding"] = "verified_frozen"
    with pytest.raises(ValueError, match="availability differs"):
        verify_graph(_rehash(altered))


@pytest.mark.parametrize("fault", ["denied", "list-non-object", "page-non-array", "another-video"])
def test_reader_rejects_invalid_newly_frozen_raw_receipt(tmp_path, monkeypatch, fault):
    capture = PlatformObservation.capture

    def faulted_receipt(**kwargs):
        if kwargs["extractor"] == "provenance-test":
            if fault == "denied":
                kwargs["state"] = "denied"
            elif fault == "list-non-object":
                kwargs["payload"] = {"list": []}
            elif fault == "page-non-array":
                kwargs["payload"] = {"list": {"vlist": {"bvid": "BV1234567890"}}}
            else:
                kwargs["payload"] = {"list": {"vlist": [{"bvid": "BV9876543210"}]}}
        return capture(**kwargs)

    monkeypatch.setattr(PlatformObservation, "capture", faulted_receipt)
    connection, root, _revision, _edition, release, _producers = manuscript(tmp_path, "bilibili", raw_kind="creator-page")
    with closing(connection), pytest.raises(ValueError, match="belongs to another source"):
        trace(connection, release_id=release["release_id"], artifact_roots=(root,))


def test_frozen_receipt_is_required_even_when_other_current_evidence_exists(tmp_path):
    connection, root, _revision, _edition, release, _producers = manuscript(tmp_path, "youtube", raw_kind="details")
    with closing(connection):
        graph = trace(connection, release_id=release["release_id"], artifact_roots=(root,))
        identity = next(node["identity"] for node in graph["nodes"] if node["kind"] == "observation")
        other = PlatformObservation.capture(platform="youtube", operation="details", endpoint="fixture/later",
            scope={"externalVideoId": "dQw4w9WgXcQ"}, extractor="test", extractor_version="1",
            payload={"description": "Current description"}, observed_at=100)
        with connection:
            ObservationRepository(connection).append((other,))
            guards = tuple(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                                              "AND tbl_name='platform_observations'"))
            for name, _sql in guards:
                connection.execute('DROP TRIGGER "' + name + '"')
            connection.execute("DELETE FROM platform_observations WHERE observation_id=?", (identity,))
            for _name, sql in guards:
                connection.execute(sql)
        with pytest.raises(ValueError, match="raw platform observation ID is unavailable"):
            trace(connection, release_id=release["release_id"], artifact_roots=(root,))


def test_unbound_historical_input_does_not_adopt_matching_current_receipts(tmp_path):
    connection, root, _revision, _edition, release, _producers = manuscript(tmp_path, "youtube")
    with closing(connection):
        initial = trace(connection, release_id=release["release_id"], artifact_roots=(root,))
        observation = PlatformObservation.capture(platform="youtube", operation="details", endpoint="fixture/late",
            scope={"externalVideoId": "dQw4w9WgXcQ"}, extractor="test", extractor_version="1",
            payload={"title": "Later observation"}, observed_at=17)
        with connection:
            ObservationRepository(connection).append((observation,))
        later = trace(connection, release_id=release["release_id"], artifact_roots=(root,))
        assert later == initial and all(node["kind"] != "observation" for node in later["nodes"])
        assert report_graph(later)["revisions"][0]["source_metadata"]["raw_observation_binding"] == "unavailable_unfrozen_history"
        assert report_graph(later)["revisions"][0]["metadata_selection"] == {"state": "unknown", "fields": []}
