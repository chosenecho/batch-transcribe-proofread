"""Recomputed container hashes cannot legitimize foreign or missing evidence."""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from copy import deepcopy

import pytest

from bili_asr.canonical_json import canonical, digest
from bili_asr.provenance_contract import EvidenceGraph, verify_graph
from bili_asr.services.provenance import trace
from tests.test_provenance_contract import artifact_graph
from tests.test_provenance_trace import _rehash, manuscript


@pytest.fixture(scope="module")
def recorded_graph(tmp_path_factory):
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("BILI_COMPLIANCE_ENABLED", "1")
        environment.setenv("BILI_PUBLICATION_ENABLED", "1")
        connection, root, _revision, _edition, release, _producers = manuscript(
            tmp_path_factory.mktemp("recorded-evidence"), automatic=True)
        with closing(connection):
            return trace(connection, release_id=release["release_id"], artifact_roots=(root,))


def node_of(graph, kind):
    return next(node for node in graph["nodes"] if node["kind"] == kind)


def proof_workflow(graph):
    return next(node for node in graph["nodes"] if node["kind"] == "workflow-attempt"
                and node["record"]["job"]["kind"] == "proofread")


def isolate(graph, selected):
    """Keep the real dependency closure, with a neutral artifact selector."""
    by_hash = {node["sha256"]: node for node in graph["nodes"]}
    closure = {}
    def visit(key):
        if key not in closure:
            closure[key] = by_hash[key]
            for dependency in by_hash[key]["dependencies"]:
                visit(dependency)
    visit(selected["sha256"])
    evidence = EvidenceGraph()
    evidence.nodes = closure
    text = "Exact isolated evidence closure\n"
    checksum = hashlib.sha256(text.encode()).hexdigest()
    root = evidence.add("artifact", "documents/evidence-fixture.md", {
        "name": "evidence-fixture.md", "relative_path": "documents/evidence-fixture.md",
        "sha256": checksum, "text": text, "availability": "embedded_verified"}, (selected["sha256"],))
    return evidence.seal({"kind": "artifact", "value": checksum}, [root])


@pytest.mark.parametrize("damage,kind,message", [
    ("input-version", "input", "unsupported frozen input version"),
    ("input-row", "input", "frozen input or prompt digest"),
    ("source-metadata", "input", "metadata differs from frozen input"),
    ("transcript-body", "transcript", "transcript content digest"),
    ("producer-run", "producer", "acquisition run mismatch"),
    ("evidence-wrapper", "producer", "ASR evidence identity"),
    ("evidence-version", "producer", "unsupported ASR evidence"),
    ("request-array", "call", "model request is not an object"),
    ("request-wrapper", "call", "invocation request is not an object"),
    ("response-array", "call", "model response is not an object"),
    ("invocation-kind", "call", "unknown model invocation kind"),
    ("envelope-version", "call", "unsupported model invocation envelope"),
    ("call-attempt", "call", "call attempt mismatch"),
    ("call-input-binding", "call", "call differs from workflow input binding"),
    ("replay-missing-id", "call", "replay invocation and dependency disagree"),
    ("replay-missing-source", "call", "replay source mismatch"),
    ("checkpoint-missing-call", "checkpoint", "needs exactly one call"),
    ("revision-job-binding", "revision", "revision job differs from frozen input"),
    ("workflow-job", "workflow-attempt", "workflow attempt job mismatch"),
    ("workflow-profile", "workflow-attempt", "workflow profile differs from job"),
    ("retained-audio", "audio", "retained audio identity mismatch"),
])
def test_resealed_record_rejects_foreign_or_missing_domain_evidence(recorded_graph, damage, kind, message):
    original = deepcopy(recorded_graph)
    selected = (next(node for node in original["nodes"] if node["kind"] == kind
                    and node["record"].get("profile") is not None)
                if damage == "workflow-profile" else node_of(original, kind))
    graph = isolate(original, selected)
    record = selected["record"]
    if damage == "input-version":
        record["version"] = 3
    elif damage == "input-row":
        record["row"]["base_transcript_id"] += 1
    elif damage == "source-metadata":
        source = node_of(graph, "source")
        source["record"]["metadata"] = {"bvid": "BV1ANOTHER00", "page_index": 0}
        source["identity"] = digest(source["record"]["metadata"])
    elif damage == "transcript-body":
        record["segments"][0]["text"] = "Unrelated source words."
    elif damage == "producer-run":
        record["run"]["run_id"] = "foreign-run"
    elif damage == "evidence-wrapper":
        record["evidence"]["schema_version"] = 2
    elif damage == "evidence-version":
        value = json.loads(record["evidence"]["evidence_json"])
        record["evidence"]["evidence_json"] = canonical(value | {"schema_version": 2})
    elif damage in {"request-array", "request-wrapper"}:
        record["row"]["request_json"] = canonical([] if damage == "request-array" else {"request": []})
    elif damage == "response-array":
        record["row"]["response_json"] = "[]"
    elif damage in {"invocation-kind", "envelope-version", "replay-missing-id", "replay-missing-source"}:
        value = json.loads(record["row"]["request_json"])
        if damage == "envelope-version":
            value["envelope_version"] = 3
        else:
            value["invocation_kind"] = "unknown" if damage == "invocation-kind" else "response_replay"
            if damage == "replay-missing-source":
                value["replay_of_call_id"] = "unrecorded-physical-call"
        record["row"]["request_json"] = canonical(value)
    elif damage == "call-attempt":
        record["row"]["attempt_id"] = "foreign-attempt"
    elif damage == "call-input-binding":
        proof_workflow(graph)["record"]["input_binding"]["input_id"] = "a" * 64
    elif damage == "checkpoint-missing-call":
        selected["dependencies"] = []
    elif damage == "revision-job-binding":
        record["job_binding"]["job_id"] = "foreign-job"
    elif damage == "workflow-job":
        record["attempt"]["job_id"] = "foreign-job"
    elif damage == "workflow-profile":
        record["job"]["profile_id"] = "foreign-profile"
    else:
        record["retained"] = [{"sha256": "a" * 64, "byte_size": 1}]
    with pytest.raises(ValueError, match=message):
        verify_graph(_rehash(graph))
    assert verify_graph(recorded_graph)["valid"]


@pytest.mark.parametrize("damage,message", [
    ("digest", "edition digest mismatch"), ("parent", "edition parent dependency"),
    ("version", "unsupported publication content version"), ("normalization", "invalid publication content"),
    ("source", "edition source differs from frozen input"), ("origin", "unsupported edition origin"),
    ("audit-missing", "edition review audit is missing"), ("audit-actor", "edition review differs from audit"),
])
def test_resealed_edition_cannot_claim_unrecorded_source_parent_or_review(recorded_graph, damage, message):
    original = deepcopy(recorded_graph)
    selected = node_of(original, "edition")
    graph = isolate(original, selected)
    record, edition = selected["record"], selected["record"]["edition"]
    if damage == "digest":
        edition["review_content_sha256"] = "a" * 64
    elif damage == "parent":
        edition["parent_edition_id"] = "unrecorded-parent"
    elif damage == "version":
        edition["content_version"] = 3
    elif damage in {"normalization", "source"}:
        content = edition["content"]
        if damage == "normalization":
            content["title"] = "  " + content["title"]
        else:
            content["source"]["videoPartId"] += 1
        edition.update(content_sha256=digest(content), content_json=canonical(content), review_content_sha256=digest(content))
    elif damage == "origin":
        record["import_origin"] = {"unrecorded": True}
    elif damage == "audit-missing":
        record["events"] = []
    else:
        edition["review_actor"] = "unrecorded-approver"
    with pytest.raises(ValueError, match=message):
        verify_graph(_rehash(graph))


def test_evidence_builder_bounds_node_count_and_sealed_container_overhead(monkeypatch):
    monkeypatch.setattr("bili_asr.provenance_contract.MAX_NODES", 1)
    builder = EvidenceGraph()
    builder.add("artifact", "first", {})
    with pytest.raises(ValueError, match="node limit"):
        builder.add("artifact", "second", {})
    monkeypatch.setattr("bili_asr.provenance_contract.MAX_BYTES", 16)
    with pytest.raises(ValueError, match="portable evidence exceeds byte limit"):
        EvidenceGraph().seal({"kind": "artifact", "value": "a" * 64}, [])


def test_graph_integrity_and_selector_checks_remain_independent_of_embedded_domain_checks():
    graph = artifact_graph()
    graph["graph_sha256"] = "a" * 64
    with pytest.raises(ValueError, match="graph digest mismatch"):
        verify_graph(graph)
    graph = artifact_graph()
    graph["nodes"][0]["sha256"] = "a" * 64
    graph["graph_sha256"] = digest({key: value for key, value in graph.items() if key != "graph_sha256"})
    with pytest.raises(ValueError, match="node digest mismatch"):
        verify_graph(graph)
    graph = artifact_graph()
    graph["selector"]["value"] = "a" * 64
    graph["graph_sha256"] = digest({key: value for key, value in graph.items() if key != "graph_sha256"})
    with pytest.raises(ValueError, match="final artifact selector is unavailable"):
        verify_graph(graph)
