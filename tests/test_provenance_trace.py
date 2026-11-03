"""Final artifact readers preserve exact producer, model-call and history evidence."""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from copy import deepcopy
from dataclasses import replace

import pytest

from bili_asr.canonical_json import canonical, digest
from bili_asr.editorial import EditorialConfig
from bili_asr.platform_identity import ContentRef
from bili_asr.provenance_contract import read_graph, verify_graph, write_graph
from bili_asr.provenance_report import report_graph
from bili_asr.publication import (
    create_edition,
    edit_edition,
    publish_edition,
    verify_release,
    withdraw_release,
)
from bili_asr.services.archive_migration import initialize_archive
from bili_asr.services.provenance import trace
from bili_asr.storage.database import connect_database
from bili_asr.storage.models import AcquisitionRunRecord, TranscriptSegmentRecord
from bili_asr.storage.sources import SourceRepository, SourceVideoMetadata
from bili_asr.storage.transcripts import TranscriptRepository
from bili_asr.storage.workflow import AsrProfile, WorkflowRepository
from tests.test_ai_editorial import FakeClient, runtime
from tests.test_publication_export import approve
from tests.test_workflow_control_plane import _seed_part

pytestmark = pytest.mark.usefixtures("enable_content_publication_for_tests")


def manuscript(tmp_path, platform="bilibili", *, automatic=False, evidence=True, raw_kind=None, seed_part=None):
    root = tmp_path / "archive"
    if seed_part is None:
        initialize_archive(root, with_source_observations=True)
    else:
        part_id = seed_part(root)
    connection = connect_database(root / "archive.db")
    if seed_part is not None:
        assert platform == "bilibili"
    elif platform == "bilibili":
        part_id = _seed_part(connection)
    else:
        with connection:
            part_id = SourceRepository(connection).upsert_video(SourceVideoMetadata(
                ContentRef("youtube", "dQw4w9WgXcQ"), "English source", 2000, "UCcreator", "Creator", 17, "en", 2))
    if raw_kind is not None:
        from bili_asr.source_observations import PlatformObservation
        from bili_asr.storage.source_observations import ObservationRepository
        part = SourceRepository(connection).part(part_id)
        ref = part["content_ref"]
        payload = {"title": part["video_title"], "description": "Frozen raw detail\n中文", "duration": 2,
                   "timestamp": part["pubdate"], "tags": ["raw-tag"], "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/default.jpg",
                   "private_unknown_field": {"nested": [True, None, 1, "exact 原始值"]}}
        scope = {"externalVideoId": ref.external_video_id, "partIndex": ref.part_index}
        if raw_kind == "creator-page":
            payload = {"list": {"vlist": [{"bvid": ref.external_video_id, **payload}]}}
            scope = {"creatorId": str(part["creator_external_id"]), "pageNumber": 1}
        observation = PlatformObservation.capture(platform=platform, operation=raw_kind, endpoint="fixture/source",
            scope=scope, extractor="provenance-test", extractor_version="1", payload=payload, observed_at=17)
        with connection:
            if platform == "youtube":
                SourceRepository(connection).upsert_video(SourceVideoMetadata(ref, part["video_title"], 2000,
                    "UCcreator", "Creator", part["pubdate"], "en", 17, observations=(observation,)))
            else:
                ObservationRepository(connection).append((observation,))
    content = b"exact private audio fixture"
    audio_sha = hashlib.sha256(content).hexdigest()
    root.joinpath("audio").mkdir()
    root.joinpath("audio/source.m4a").write_bytes(content)
    audio = {"storage_key": "audio/source.m4a", "sha256": audio_sha, "byte_size": len(content)}
    with connection:
        connection.execute("INSERT INTO audio_objects VALUES (1,?,?, 'm4a',2000,'audio/source.m4a',1)", (audio_sha, len(content)))
        connection.execute("INSERT INTO part_audio_objects VALUES (?,1,1,'source')", (part_id,))
    workflow = WorkflowRepository(connection)
    transcripts = TranscriptRepository(connection)
    producers = []
    for index, name in enumerate(("Qwen/model-a", "Qwen/model-b")):
        profile = workflow.register_profile(AsrProfile(name, name, "a" * 40, device="cpu"))
        run_id, job_id, attempt_id = f"run-{index}", f"asr-{index}", f"asr-attempt-{index}"
        transcripts.start_acquisition_run(AcquisitionRunRecord(run_id, "asr", "bvid", "source", 1, False, index + 1))
        configuration = connection.execute("SELECT config_sha256 FROM workflow_asr_profiles WHERE profile_id=?", (profile,)).fetchone()[0]
        recorded = {"schema_version": 1, "profile_id": profile, "config_sha256": configuration,
                    "audio": audio, "provenance": {"model_name": name, "aligner_name": "Qwen/aligner"},
                    "runtime_binding": {"model": {"revision": "a" * 40}},
                    "diagnostics": {"resolved_model_revision": "a" * 40, "resolved_aligner_revision": "b" * 40,
                                    "environment": {"torch": "fixture-runtime"}}}
        stored = transcripts.record_local_transcript(run_id=run_id, video_part_id=part_id, language="en",
            segments=(TranscriptSegmentRecord(0, 1900, "A complete source sentence."),), model_name=name,
            model_revision="a" * 40, started_at=index + 1, finished_at=index + 2, created_at=index + 2,
            coverage={"decoded_s": 2.0, "produced_s": 1.9, "coverage": .95,
                      "coverage_min": .85, "coverage_short": False}, asr_evidence=recorded)
        transcripts.finish_acquisition_run(run_id, index + 2)
        with connection:
            connection.execute("INSERT INTO workflow_jobs VALUES (?,?,?,?,'all',?,?,'succeeded',0,1,NULL,NULL,1,NULL,1,2)",
                (job_id, "asr", part_id, profile, job_id, canonical({})))
            connection.execute("INSERT INTO workflow_attempts VALUES (?,?,?,1,2,'succeeded',NULL,?)",
                (attempt_id, job_id, "fixture-asr", canonical({"run_id": run_id, "transcript_id": stored.transcript_id})))
        producers.append({"run_id": run_id, "job_id": job_id, "attempt_id": attempt_id, "transcript_id": stored.transcript_id})
    assert producers[0]["transcript_id"] == producers[1]["transcript_id"]
    workflow, repository, _handlers, executor = runtime(connection, root, FakeClient())
    config = EditorialConfig()
    if automatic:
        with workflow._write_transaction():
            proof_id, *_ = workflow._editorial_jobs(video_part_id=part_id, payload={"editorial_config": config.to_dict()},
                                                    prerequisite_job_id=producers[-1]["job_id"])
    else:
        if raw_kind is not None and platform == "bilibili":
            from bili_asr.editorial_input_v2 import prepare_input_v2
            from bili_asr.storage.metadata import MetadataRepository

            # Build a new, explicitly receipt-bound input. Historical Bili DTOs
            # remain unbound until the production acceptance recorder exists.
            metadata = replace(MetadataRepository(connection).read_source_metadata(part_id),
                codec_version=2, observation_bindings=((observation.observation_id,
                    observation.envelope["payloadSha256"], digest(observation.envelope)),))
            prepared = prepare_input_v2(repository.read_source(stored.transcript_id), None,
                                       config, source_metadata=metadata)
            with connection:
                prepared = repository.store_input(prepared)
        else:
            prepared = repository.prepare(stored.transcript_id, None, config)
        proof_id, *_ = workflow.request_editorial(video_part_id=part_id, input_id=prepared["input_id"])
    result = executor.run()
    assert result.succeeded == 2 and result.failed == 0
    revision_id = repository.revision_for_job(proof_id)
    edition = create_edition(connection, revision_id=revision_id, artifact_roots=(root,), actor="private-editor")
    approve(connection, edition)
    release = publish_edition(connection, edition_id=edition["edition_id"], artifact_roots=(root,), write_root=root, actor="private-publisher")
    if not evidence:
        # Model a previously released historical archive with missing evidence,
        # after the current execution path has passed real measured admission.
        with connection:
            connection.execute("DELETE FROM transcript_asr_evidence")
    return connection, root, revision_id, edition, release, producers


@pytest.mark.parametrize("platform", ["bilibili", "youtube"])
def test_final_hash_trace_preserves_multiple_producers_and_actual_ai_model(tmp_path, platform):
    connection, root, revision, _edition, release, _producers = manuscript(tmp_path, platform)
    with closing(connection):
        before = root.joinpath("archive.db").read_bytes()
        document = verify_release(connection, release["release_id"], (root,))[2]
        graph = trace(connection, artifact_sha256=release["artifact_sha256"], artifact_roots=(root,))
        report = report_graph(graph)
        fact = report["revisions"][0]
        assert report["valid"] and fact["revision_id"] == revision
        assert fact["requested_model"] == "deepseek-flash"
        assert fact["calls"][0]["returned_model"] == "deepseek-flash-offline"
        assert fact["calls"][0]["immutable_model_revision"] == {"state": "unknown", "value": None}
        assert fact["calls"][0]["selected_checkpoint"] and fact["calls"][0]["physical_provider_send"]
        base = fact["transcripts"][0]
        assert base["content_deduplicated"] and len(base["producers"]) == 2
        assert {item["requested_model"] for item in base["producers"]} == {"Qwen/model-a", "Qwen/model-b"}
        assert fact["source_selection"]["state"] == "unavailable_unfrozen_history"
        assert fact["source_metadata"]["raw_observation_binding"] == "unavailable_unfrozen_history"
        assert fact["prompt_sha256"] and fact["config_sha256"]
        assert all(item["audio"][0]["identity"]["sha256"] == hashlib.sha256(b"exact private audio fixture").hexdigest()
                   for item in base["producers"])
        assert all(item["audio"][0]["byte_state_at_capture"] == "local_bytes_verified_at_capture" for item in base["producers"])
        output = tmp_path / "private.json"
        write_graph(output, graph)
        assert report_graph(read_graph(output)) == report
        connection.close()
        root.joinpath("archive.db").rename(root.joinpath("offline.db"))
        assert report_graph(read_graph(output)) == report
        root.joinpath("offline.db").rename(root.joinpath("archive.db"))
        assert root.joinpath("archive.db").read_bytes() == before
        assert root.joinpath(release["relative_path"]).read_bytes() == document


@pytest.mark.parametrize("cache", ["budget", "readonly-context", "editable-text"])
def test_resealed_graph_cannot_replace_chunks_derived_from_a_frozen_plan(tmp_path, cache):
    connection, root, _revision, _edition, release, _producers = manuscript(tmp_path, "youtube")
    with closing(connection):
        graph = trace(connection, release_id=release["release_id"], artifact_roots=(root,))
    altered = deepcopy(graph)
    input_node = next(node for node in altered["nodes"] if node["kind"] == "input")
    prepared = json.loads(input_node["record"]["row"]["prepared_json"])
    assert prepared["snapshot"]["config"]["budget_policy"] == "token-budget-v2"
    chunk = prepared["chunks"][0]
    if cache == "budget":
        chunk["budget_estimate"]["request_tokens"] += 1
    elif cache == "readonly-context":
        chunk["readonly_context"].append(deepcopy(chunk["editable_segments"][0]))
    else:
        chunk["editable_segments"][0]["text"] = "Changed cached request text"
    input_node["record"]["row"]["prepared_json"] = json.dumps(prepared, ensure_ascii=False)
    with pytest.raises(ValueError, match="frozen editorial budget plan or derived chunks changed"):
        verify_graph(_rehash(altered))


def test_automatic_proofreading_records_the_selected_attempt_without_changing_input(tmp_path):
    connection, root, _revision, _edition, release, producers = manuscript(tmp_path, automatic=True)
    with closing(connection):
        report = report_graph(trace(connection, release_id=release["release_id"], artifact_roots=(root,)))
        selected = report["revisions"][0]["source_selection"]
        assert selected["state"] == "recorded"
        assert selected["selections"][0]["attempt_id"] == producers[-1]["attempt_id"]
        assert selected["selections"][0]["run_id"] == producers[-1]["run_id"]
        prepared = json.loads(connection.execute("SELECT prepared_json FROM editorial_inputs").fetchone()[0])
        assert "source_selection" not in prepared
        assert prepared["input_id"] == digest(prepared["snapshot"])


def test_cold_audio_and_historical_missing_evidence_are_explicit(tmp_path):
    connection, root, _revision, _edition, release, _producers = manuscript(tmp_path, evidence=False)
    with closing(connection):
        root.joinpath("audio/source.m4a").unlink()
        report = report_graph(trace(connection, release_id=release["release_id"], artifact_roots=(root,)))
        producers = report["revisions"][0]["transcripts"][0]["producers"]
        assert all(item["asr_evidence_state"] == "unavailable_legacy" and item["audio"] == [] for item in producers)
        assert not root.joinpath("audio/source.m4a").exists()


def test_cold_audio_keeps_identity_without_implicit_restore(tmp_path):
    connection, root, _revision, _edition, release, _producers = manuscript(tmp_path)
    with closing(connection):
        root.joinpath("audio/source.m4a").unlink()
        report = report_graph(trace(connection, release_id=release["release_id"], artifact_roots=(root,)))
        producers = report["revisions"][0]["transcripts"][0]["producers"]
        assert all(item["audio"][0]["byte_state_at_capture"] == "unavailable_local_bytes" for item in producers)
        assert all(item["audio"][0]["identity"]["sha256"] for item in producers)
        assert not root.joinpath("audio/source.m4a").exists()






@pytest.mark.parametrize("field", ["graph_sha256", "node", "dependency", "format"])
def test_offline_graph_rejects_corruption_and_unknown_contracts(tmp_path, field):
    connection, root, _revision, _edition, release, _producers = manuscript(tmp_path)
    with closing(connection):
        graph = trace(connection, release_id=release["release_id"], artifact_roots=(root,))
    damaged = deepcopy(graph)
    if field == "graph_sha256":
        damaged["graph_sha256"] = "a" * 64
    elif field == "node":
        damaged["nodes"][0]["record"]["tampered"] = True
    elif field == "dependency":
        damaged["nodes"][0]["dependencies"].append("b" * 64)
    else:
        damaged["format"] = "artifact-provenance/v99"
    with pytest.raises(ValueError, match="provenance"):
        verify_graph(damaged)


def test_withdrawn_and_new_release_keep_their_exact_edition_lineages(tmp_path):
    connection, root, _revision, parent, release, _producers = manuscript(tmp_path)
    with closing(connection):
        withdraw_release(connection, release_id=release["release_id"], actor="withdrawer", note="withdraw")
        child = edit_edition(connection, edition_id=parent["edition_id"], markdown_text="New reviewed release body.", actor="editor", note="new")
        approve(connection, child)
        newer = publish_edition(connection, edition_id=child["edition_id"], artifact_roots=(root,), write_root=root,
                                actor="publisher", expected_release_id=None)
        old_report = report_graph(trace(connection, release_id=release["release_id"], artifact_roots=(root,)))
        new_report = report_graph(trace(connection, release_id=newer["release_id"], artifact_roots=(root,)))
        assert old_report["releases"][0]["status"] == "withdrawn"
        assert new_report["releases"][0]["edition_id"] == child["edition_id"]
        assert old_report["revisions"] == new_report["revisions"]


def test_reproofreading_keeps_both_configs_and_old_ai_bytes(tmp_path):
    connection, root, revision, parent, release, producers = manuscript(tmp_path)
    with closing(connection):
        before = root.joinpath(release["relative_path"]).read_bytes()
        workflow, repository, _handlers, executor = runtime(connection, root, FakeClient())
        prepared = repository.prepare(producers[0]["transcript_id"], None, EditorialConfig(model="deepseek-v4.1-flash"))
        proof_id, *_ = workflow.request_editorial(video_part_id=parent["video_part_id"], input_id=prepared["input_id"])
        assert executor.run().succeeded == 2
        revised = repository.revision_for_job(proof_id)
        assert revised != revision
        edition = create_edition(connection, revision_id=revised, artifact_roots=(root,), actor="editor",
                                 expected_edition_id=parent["edition_id"])
        report = report_graph(trace(connection, edition_id=edition["edition_id"], artifact_roots=(root,)))
        assert {item["requested_model"] for item in report["revisions"]} == {"deepseek-flash", "deepseek-v4.1-flash"}
        assert root.joinpath(release["relative_path"]).read_bytes() == before
        assert len(report["editions"]) == 2


def test_failed_calls_and_response_replays_remain_distinct_from_paid_sends(tmp_path):
    connection, root, _revision, _edition, release, _producers = manuscript(tmp_path)
    with closing(connection):
        call = dict(connection.execute("SELECT * FROM editorial_model_calls").fetchone())
        request = json.loads(call["request_json"])
        replay = dict(request, invocation_kind="response_replay", replay_of_call_id=call["call_id"])
        with connection:
            for identity, body, response, error in (("failed-send", request, None, "Timeout"),
                                                      ("accepted-replay", replay, call["response_json"], None)):
                connection.execute("INSERT INTO editorial_model_calls VALUES (?,?,?,?,?,?,?,?,?)",
                    (identity, call["attempt_id"], call["input_id"], call["chunk_id"], canonical(body), response,
                     error, call["started_at"], call["finished_at"]))
            connection.execute("UPDATE editorial_chunk_results SET call_id='accepted-replay'")
        graph = trace(connection, release_id=release["release_id"], artifact_roots=(root,))
        calls = report_graph(graph)["revisions"][0]["calls"]
        assert len(calls) == 3 and sum(item["physical_provider_send"] for item in calls) == 2
        assert next(item for item in calls if item["call_id"] == "failed-send")["error_code"] == "Timeout"
        selected = next(item for item in calls if item["selected_checkpoint"])
        assert selected["call_id"] == "accepted-replay" and selected["replay_of_call_id"] == call["call_id"]
        assert not selected["physical_provider_send"]


def _rehash(graph):
    """Reseal container hashes only, leaving intrinsic domain identities unchanged."""
    old = {node["sha256"]: node for node in graph["nodes"]}
    rewritten = {}
    def visit(key):
        if key not in rewritten:
            node = {name: value for name, value in old[key].items() if name != "sha256"}
            node["dependencies"] = sorted(visit(dependency)["sha256"] for dependency in node["dependencies"])
            rewritten[key] = dict(node, sha256=digest(node))
        return rewritten[key]
    roots = [visit(key)["sha256"] for key in graph["roots"]]
    result = {key: value for key, value in graph.items() if key not in {"nodes", "roots", "graph_sha256"}}
    result.update(roots=sorted(roots), nodes=sorted(rewritten.values(), key=lambda item: item["sha256"]))
    return dict(result, graph_sha256=digest(result))


def test_resealed_checkpoint_cannot_change_the_selected_revision(tmp_path):
    connection, root, _revision, _edition, release, _producers = manuscript(tmp_path)
    with closing(connection):
        graph = trace(connection, release_id=release["release_id"], artifact_roots=(root,))
    checkpoint = next(node for node in graph["nodes"] if node["kind"] == "checkpoint")
    blocks = json.loads(checkpoint["record"]["row"]["blocks_json"])
    blocks[0]["text"] = "Different selected block"
    checkpoint["record"]["row"]["blocks_json"] = canonical(blocks)
    with pytest.raises(ValueError, match="selected checkpoints differ"):
        verify_graph(_rehash(graph))


@pytest.mark.parametrize("fault", ["profile_digest", "producer_audio", "call_input", "source_selection"])
def test_resealed_container_cannot_replace_frozen_domain_relationships(tmp_path, fault):
    connection, root, _revision, _edition, release, _producers = manuscript(tmp_path, automatic=True)
    with closing(connection):
        graph = trace(connection, release_id=release["release_id"], artifact_roots=(root,))
    if fault == "profile_digest":
        record = next(node["record"] for node in graph["nodes"]
                      if node["kind"] == "workflow-attempt" and node["record"]["profile"] is not None)
        config = json.loads(record["profile"]["frozen_config"]["config_json"])
        config["model_name"] = "Qwen/model-tampered"
        record["profile"]["frozen_config"]["config_json"] = canonical(config)
    elif fault == "producer_audio":
        record = next(node["record"] for node in graph["nodes"] if node["kind"] == "producer")
        evidence = json.loads(record["evidence"]["evidence_json"])
        evidence["audio"]["sha256"] = "a" * 64
        record["evidence"]["evidence_json"] = canonical(evidence)
    elif fault == "call_input":
        node = next(node for node in graph["nodes"] if node["kind"] == "call")
        node["record"]["row"]["input_id"] = "b" * 64
    else:
        node = next(node for node in graph["nodes"]
                    if node["kind"] == "workflow-attempt" and node["record"]["job"]["kind"] == "proofread")
        result = json.loads(node["record"]["attempt"]["result_json"])
        result["source_selection"]["run_id"] = "different-producer"
        node["record"]["attempt"]["result_json"] = canonical(result)
    with pytest.raises(ValueError, match="provenance"):
        verify_graph(_rehash(graph))


def test_reader_rejects_modified_audio_and_bounds_text_before_embedding(tmp_path, monkeypatch):
    connection, root, _revision, _edition, release, _producers = manuscript(tmp_path)
    with closing(connection):
        root.joinpath("audio/source.m4a").write_bytes(b"modified")
        with pytest.raises(ValueError, match="audio digest mismatch"):
            trace(connection, release_id=release["release_id"], artifact_roots=(root,))
        root.joinpath("audio/source.m4a").write_bytes(b"exact private audio fixture")
        monkeypatch.setattr("bili_asr.services.provenance.MAX_BYTES", 4)
        with pytest.raises(ValueError, match="reader byte limit"):
            trace(connection, release_id=release["release_id"], artifact_roots=(root,))


def test_preserved_body_import_and_supplement_remain_bound_offline(tmp_path, preserved_archive_template):
    from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
    from bili_asr.services.source_supplement import (
        apply_source_supplement,
        plan_source_supplement,
    )
    from tests.fixtures.migrated_archive import copy_archive
    template, identities = preserved_archive_template
    root = copy_archive(template, tmp_path / "historical")
    with ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session:
        connection = session.connection
        edition_id = identities[0]
        plan = plan_source_supplement(connection, edition_ids=[edition_id], artifact_roots=(root,))
        supplemented = apply_source_supplement(connection, plan=plan, artifact_roots=(root,), actor="operator")
        child_id = supplemented["editions"][0]["editionId"]
        graph = trace(connection, edition_id=child_id, artifact_roots=(root,))
        report = report_graph(graph)
        selected = next(item for item in report["editions"] if item["edition_id"] == child_id)
        assert selected["import_origin"]["relation"] == "preserved" and selected["source_supplement"] is not None
        assert any(item["name"] == "preserved-body.md" for item in report["artifacts"])
        private = tmp_path / "historical-chain.json"
        write_graph(private, graph)
    assert report_graph(read_graph(private)) == report
