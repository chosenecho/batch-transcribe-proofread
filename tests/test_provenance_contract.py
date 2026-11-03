"""Portable evidence is bounded, closed and independently verifiable."""
from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from bili_asr.canonical_json import digest
from bili_asr.provenance_contract import (
    EvidenceGraph,
    exact_json,
    read_graph,
    verify_graph,
    write_graph,
)


def artifact_graph():
    content = "原始 UTF-8 文稿\n"
    sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    graph = EvidenceGraph()
    root = graph.add("artifact", "documents/original.md", {"name": "original.md",
        "relative_path": "documents/original.md", "sha256": sha256, "text": content, "availability": "embedded_verified"})
    return graph.seal({"kind": "artifact", "value": sha256}, [root])


def test_private_evidence_roundtrips_offline_and_refuses_overwrite(tmp_path):
    path = tmp_path / "evidence.json"
    graph = artifact_graph()
    assert write_graph(path, graph)["graph_sha256"] == graph["graph_sha256"]
    assert read_graph(path) == graph
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_graph(path, graph)
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["unknown_version", "node_identity", "unbound", "dependency", "bytes"])
def test_closed_evidence_rejects_resealed_invalid_relationships(damage):
    graph = deepcopy(artifact_graph())
    node = graph["nodes"][0]
    if damage == "unknown_version":
        graph["reader_version"] = 2
    elif damage == "node_identity":
        node["identity"] = "documents/another.md"
    elif damage == "unbound":
        extra = {**node, "identity": "unbound"}
        extra["sha256"] = digest({key: value for key, value in extra.items() if key != "sha256"})
        graph["nodes"].append(extra)
    elif damage == "dependency":
        node["dependencies"] = ["a" * 64]
    else:
        node["record"]["text"] = "Different body"
    root_before = node["sha256"]
    node["sha256"] = digest({key: value for key, value in node.items() if key != "sha256"})
    graph["roots"] = [node["sha256"] if key == root_before else key for key in graph["roots"]]
    graph["nodes"].sort(key=lambda item: item["sha256"])
    graph["graph_sha256"] = digest({key: value for key, value in graph.items() if key != "graph_sha256"})
    with pytest.raises(ValueError, match="provenance"):
        verify_graph(graph)


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'])
def test_private_evidence_json_rejects_ambiguous_numbers_and_duplicate_fields(raw):
    with pytest.raises(ValueError, match="provenance"):
        exact_json(raw)


def test_private_evidence_byte_limits_apply_to_records_and_reads(tmp_path, monkeypatch):
    graph = artifact_graph()
    path = tmp_path / "evidence.json"
    write_graph(path, graph)
    monkeypatch.setattr("bili_asr.provenance_contract.MAX_BYTES", 16)
    with pytest.raises(ValueError, match="byte limit"):
        EvidenceGraph().add("artifact", "large", {"text": "large recorded evidence"})
    with pytest.raises(ValueError, match="byte limit"):
        read_graph(path)


def test_failed_private_evidence_write_removes_only_its_partial_file(tmp_path, monkeypatch):
    path = tmp_path / "evidence.json"
    def fail_sync(_descriptor):
        raise OSError("injected evidence sync failure")
    monkeypatch.setattr("bili_asr.file_ops.os.fsync", fail_sync)
    with pytest.raises(OSError, match="injected"):
        write_graph(path, artifact_graph())
    assert not path.exists()
