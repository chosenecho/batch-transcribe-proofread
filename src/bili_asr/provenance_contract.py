"""Portable private provenance: a bounded Merkle closure of recorded evidence."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from bili_asr.canonical_json import digest
from bili_asr.file_ops import checked_path, json_bytes, write_new_file
from bili_asr.provenance_metadata import MetadataEvidence, is_acceptance

FORMAT = "artifact-provenance/v1"
POLICY = "frozen-input-and-recorded-producers/v1"
MAX_BYTES = 64 * 1024 * 1024
MAX_NODES = 25_000
KINDS = frozenset({"release", "edition", "revision", "input", "transcript", "producer",
                   "workflow-attempt", "call", "checkpoint", "artifact", "source", "observation", "metadata-acceptance", "audio"})
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def exact_json(raw: str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("provenance: duplicate JSON field")
            result[key] = value
        return result
    def reject_constant(_value):
        raise ValueError("provenance: nonfinite JSON")
    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)


class EvidenceGraph:
    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.record_bytes = 0

    def add(self, kind: str, identity: str, record: dict, dependencies=()) -> str:
        node = {"kind": kind, "identity": str(identity), "record": record,
                "dependencies": sorted(set(dependencies))}
        identity = digest(node)
        stored = dict(node, sha256=identity)
        if identity not in self.nodes:
            self.record_bytes += len(json_bytes(stored))
            if self.record_bytes > MAX_BYTES:
                raise ValueError("provenance: evidence records exceed byte limit")
        self.nodes[identity] = stored
        if len(self.nodes) > MAX_NODES:
            raise ValueError("provenance: dependency closure exceeds node limit")
        return identity

    def seal(self, selector: dict, roots: list[str]) -> dict:
        value = {"format": FORMAT, "schema_version": 1, "selection_policy": POLICY,
                 "reader_version": 1, "selector": selector, "roots": sorted(set(roots)),
                 "nodes": [self.nodes[key] for key in sorted(self.nodes)]}
        value["graph_sha256"] = digest(value)
        if len(json_bytes(value)) > MAX_BYTES:
            raise ValueError("provenance: portable evidence exceeds byte limit")
        verify_graph(value)
        return value


def _bound(node: dict, nodes: dict, kind: str) -> list[dict]:
    return [nodes[key] for key in node["dependencies"] if nodes[key]["kind"] == kind]


def _one(node: dict, nodes: dict, kind: str) -> dict:
    matches = _bound(node, nodes, kind)
    if len(matches) != 1:
        raise ValueError(f"provenance: {node['kind']} needs exactly one {kind}")
    return matches[0]["record"]


def _verify_domain(node: dict, nodes: dict, metadata_evidence: MetadataEvidence) -> None:
    record, kind = node["record"], node["kind"]
    if kind in {"input", "transcript", "revision", "call"}:
        column = {"input": "input_id", "transcript": "transcript_id", "revision": "revision_id", "call": "call_id"}[kind]
        expected = str(record["row"][column])
    elif kind == "checkpoint":
        expected = f"{record['row']['input_id']}:{record['row']['chunk_id']}"
    elif kind == "producer":
        expected = f"{record['attempt']['run_id']}:{record['attempt']['video_part_id']}"
    elif kind == "workflow-attempt":
        expected = record["attempt"]["attempt_id"]
    elif kind in {"edition", "release"}:
        expected = record[kind][f"{kind}_id"]
    elif kind == "audio":
        expected = record["identity"]["sha256"]
    elif kind == "artifact":
        expected = record["relative_path"]
    elif kind == "source":
        expected = digest(record["metadata"])
    else:
        expected = node["identity"]
    if node["identity"] != expected:
        raise ValueError("provenance: node identity differs from recorded domain identity")
    if kind == "input":
        row = record["row"]
        prepared = exact_json(row["prepared_json"])
        snapshot = prepared["snapshot"]
        if type(record["version"]) is not int or record["version"] not in (1, 2):
            raise ValueError("provenance: unsupported frozen input version")
        if (prepared["input_id"] != row["input_id"] or digest(snapshot) != row["input_id"]
                or snapshot["video_part_id"] != row["video_part_id"]
                or snapshot["base"]["transcript_id"] != row["base_transcript_id"]
                or (None if snapshot["reference"] is None else snapshot["reference"]["transcript_id"]) != row["reference_transcript_id"]
                or digest(snapshot["system_prompt"]) != snapshot["prompt_sha256"]):
            raise ValueError("provenance: frozen input or prompt digest mismatch")
        budget_policy = snapshot["config"].get("budget_policy", "legacy-v1")
        if budget_policy not in {"legacy-v1", "token-budget-v2"}:
            raise ValueError("provenance: unsupported frozen budget policy")
        if budget_policy == "token-budget-v2":
            from bili_asr.editorial_budget import require_frozen_plan
            require_frozen_plan(prepared)
        sources = _bound(node, nodes, "source")
        if len(sources) != 1 or sources[0]["record"]["metadata"] != snapshot.get("metadata", {}):
            raise ValueError("provenance: metadata differs from frozen input")
        transcripts = {item["record"]["row"]["transcript_id"]: item["record"]
                       for item in _bound(node, nodes, "transcript")}
        for frozen in (snapshot["base"], snapshot["reference"]):
            if frozen is None:
                continue
            actual = transcripts.get(frozen["transcript_id"])
            if (actual is None or actual["row"]["content_sha256"] != frozen["content_sha256"]
                    or actual["row"]["video_part_id"] != row["video_part_id"]
                    or [[item["start_ms"], item["end_ms"], item["text"]] for item in actual["segments"]]
                        != [[item["start_ms"], item["end_ms"], item["text"]] for item in frozen["segments"]]):
                raise ValueError("provenance: frozen transcript dependency mismatch")
    elif kind == "transcript":
        from bili_asr.storage.transcripts import _segment_content_sha256
        triples = [[item["start_ms"], item["end_ms"], item["text"]] for item in record["segments"]]
        if _segment_content_sha256(triples) != record["row"]["content_sha256"]:
            raise ValueError("provenance: transcript content digest mismatch")
        for producer in _bound(node, nodes, "producer"):
            attempt = producer["record"]["attempt"]
            if (attempt["transcript_id"], attempt["video_part_id"]) != (record["row"]["transcript_id"], record["row"]["video_part_id"]):
                raise ValueError("provenance: transcript producer identity mismatch")
    elif kind == "revision":
        row = record["row"]
        frozen = _one(node, nodes, "input")["row"]
        blocks = exact_json(row["blocks_json"])
        if (frozen["input_id"] != row["input_id"]
                or digest({"input_id": row["input_id"], "blocks": blocks}) != row["revision_id"]):
            raise ValueError("provenance: AI revision digest mismatch")
        binding = record["job_binding"]
        if binding["job_id"] != row["job_id"] or binding["input_id"] != row["input_id"]:
            raise ValueError("provenance: revision job differs from frozen input binding")
        for workflow in _bound(node, nodes, "workflow-attempt"):
            evidence = workflow["record"]
            if evidence["job"]["kind"] != "proofread" or evidence["job"]["video_part_id"] != frozen["video_part_id"]:
                raise ValueError("provenance: revision workflow differs from frozen input")
            attempt_result = exact_json(evidence["attempt"]["result_json"]) if evidence["attempt"]["result_json"] else {}
            selected = attempt_result.get("source_selection")
            if selected is not None and selected["transcript_id"] != frozen["base_transcript_id"]:
                raise ValueError("provenance: selected ASR result differs from frozen base transcript")
        checkpoints = {item["record"]["row"]["chunk_id"]: item["record"]["row"]
                       for item in _bound(node, nodes, "checkpoint")}
        chunks = exact_json(frozen["prepared_json"])["chunks"]
        chunk_ids = {chunk["chunk_id"] for chunk in chunks}
        for call in _bound(node, nodes, "call"):
            call_row = call["record"]["row"]
            call_job = _one(call, nodes, "workflow-attempt")["job"]
            if call_row["input_id"] != row["input_id"] or call_row["chunk_id"] not in chunk_ids:
                raise ValueError("provenance: call differs from frozen revision input")
            if call_job["video_part_id"] != frozen["video_part_id"]:
                raise ValueError("provenance: call workflow differs from frozen source part")
        if any(checkpoint["input_id"] != row["input_id"] for checkpoint in checkpoints.values()):
            raise ValueError("provenance: checkpoint differs from frozen revision input")
        if checkpoints:
            if set(checkpoints) != chunk_ids:
                raise ValueError("provenance: selected chunks differ from frozen input")
            selected = []
            for chunk in chunks:
                checkpoint = checkpoints.get(chunk["chunk_id"])
                if checkpoint is None:
                    raise ValueError("provenance: partial selected chunk evidence")
                selected.extend(exact_json(checkpoint["blocks_json"]))
            if selected != blocks:
                raise ValueError("provenance: selected checkpoints differ from revision")
    elif kind == "checkpoint":
        call = _one(node, nodes, "call")["row"]
        row = record["row"]
        if (row["call_id"], row["input_id"], row["chunk_id"]) != (call["call_id"], call["input_id"], call["chunk_id"]):
            raise ValueError("provenance: selected call differs from checkpoint")
    elif kind == "call":
        row = record["row"]
        stored = exact_json(row["request_json"])
        if not isinstance(stored, dict):
            raise ValueError("provenance: model request is not an object")
        request = stored.get("request", stored)
        if not isinstance(request, dict):
            raise ValueError("provenance: invocation request is not an object")
        if row["response_json"] is not None and not isinstance(exact_json(row["response_json"]), dict):
            raise ValueError("provenance: model response is not an object")
        invocation = stored.get("invocation_kind", "provider")
        if invocation not in {"provider", "response_replay", "local_precheck"}:
            raise ValueError("provenance: unknown model invocation kind")
        if stored.get("envelope_version", 2) != 2:
            raise ValueError("provenance: unsupported model invocation envelope")
        fingerprint = stored.get("request_fingerprint")
        if fingerprint is not None and fingerprint != digest({"input_id": row["input_id"], "chunk_id": row["chunk_id"], "request": request}):
            raise ValueError("provenance: request fingerprint mismatch")
        workflow = _one(node, nodes, "workflow-attempt")
        attempt, job = workflow["attempt"], workflow["job"]
        if attempt["attempt_id"] != row["attempt_id"] or job["kind"] != "proofread":
            raise ValueError("provenance: call attempt mismatch")
        if workflow["input_binding"]["input_id"] != row["input_id"]:
            raise ValueError("provenance: call differs from workflow input binding")
        replay = stored.get("replay_of_call_id")
        if (invocation == "response_replay") != (replay is not None):
            raise ValueError("provenance: replay invocation and dependency disagree")
        if replay is not None:
            matches = _bound(node, nodes, "call")
            if len(matches) != 1 or matches[0]["record"]["row"]["call_id"] != replay:
                raise ValueError("provenance: replay source mismatch")
            original = matches[0]["record"]["row"]
            original_request = exact_json(original["request_json"])
            if (original["input_id"] != row["input_id"] or original["chunk_id"] != row["chunk_id"]
                    or original_request.get("request", original_request) != request
                    or original["response_json"] != row["response_json"]):
                raise ValueError("provenance: replay differs from physical call")
    elif kind == "edition":
        from bili_asr.provenance_editions import verify_edition
        revision_record = _one(node, nodes, "revision")
        revision = _bound(node, nodes, "revision")[0]
        verify_edition(record, revision_record, _one(revision, nodes, "input"),
                       [item["record"] for item in _bound(node, nodes, "artifact")],
                       [item["record"] for item in _bound(node, nodes, "edition")])
    elif kind == "release":
        from bili_asr.publication_identity import validate_release_identity
        edition = _one(node, nodes, "edition")["edition"]
        validate_release_identity(record["release"], edition, record["events"])
        artifacts = _bound(node, nodes, "artifact")
        if len(artifacts) != 1 or artifacts[0]["record"]["sha256"] != record["release"]["artifact_sha256"]:
            raise ValueError("provenance: release artifact dependency mismatch")
    elif kind == "artifact" and record["text"] is not None:
        if hashlib.sha256(record["text"].encode("utf-8")).hexdigest() != record["sha256"]:
            raise ValueError("provenance: embedded artifact digest mismatch")
    elif kind == "producer":
        attempt = record["attempt"]
        if attempt["run_id"] != record["run"]["run_id"]:
            raise ValueError("provenance: acquisition run mismatch")
        evidence = record["evidence"]
        if evidence is not None:
            if (evidence["schema_version"] != 1 or evidence["transcript_id"] != attempt["transcript_id"]
                    or evidence["run_id"] != attempt["run_id"] or evidence["video_part_id"] != attempt["video_part_id"]):
                raise ValueError("provenance: ASR evidence identity mismatch")
            parsed = exact_json(evidence["evidence_json"])
            if parsed["schema_version"] != 1:
                raise ValueError("provenance: unsupported ASR evidence")
            audio = _bound(node, nodes, "audio")
            if parsed.get("audio") and (len(audio) != 1 or audio[0]["record"]["identity"] != parsed["audio"]):
                raise ValueError("provenance: ASR audio dependency mismatch")
        for workflow in _bound(node, nodes, "workflow-attempt"):
            attempt_row, job = workflow["record"]["attempt"], workflow["record"]["job"]
            result = exact_json(attempt_row["result_json"])
            if (job["kind"] not in {"asr", "subtitle"} or job["video_part_id"] != attempt["video_part_id"]
                    or (result.get("run_id"), result.get("transcript_id")) != (attempt["run_id"], attempt["transcript_id"])):
                raise ValueError("provenance: workflow differs from acquisition producer")
            profile = workflow["record"]["profile"]
            if evidence is not None and profile is not None:
                if (parsed.get("profile_id"), parsed.get("config_sha256")) != (profile["profile_id"], profile["config_sha256"]):
                    raise ValueError("provenance: ASR evidence differs from frozen profile")
    elif kind == "source":
        metadata_evidence.source(node)
    elif kind == "workflow-attempt":
        attempt, job = record["attempt"], record["job"]
        if attempt["job_id"] != job["job_id"]:
            raise ValueError("provenance: workflow attempt job mismatch")
        result = exact_json(attempt["result_json"]) if attempt["result_json"] is not None else {}
        selected = result.get("source_selection")
        if selected is not None:
            if set(selected) != {"format", "asr_job_id", "attempt_id", "run_id", "transcript_id"} or selected["format"] != "editorial-source-selection/v1":
                raise ValueError("provenance: unsupported source selection")
            source = _one(node, nodes, "workflow-attempt")
            source_attempt, source_job = source["attempt"], source["job"]
            result = exact_json(source_attempt["result_json"])
            if (job["kind"] != "proofread" or source_job["kind"] != "asr"
                    or source_attempt["outcome"] != "succeeded" or source_attempt["attempt_id"] != selected["attempt_id"]
                    or source_job["job_id"] != selected["asr_job_id"] or source_job["video_part_id"] != job["video_part_id"]
                    or (result.get("run_id"), result.get("transcript_id")) != (selected["run_id"], selected["transcript_id"])):
                raise ValueError("provenance: source selection differs from ASR attempt")
        profile = record["profile"]
        if profile is not None:
            if profile["profile_id"] != job["profile_id"]:
                raise ValueError("provenance: workflow profile differs from job")
            config = profile["frozen_config"]
            if config is not None:
                from bili_asr.profile_codec import decode_profile
                from bili_asr.workflow_models import AsrProfile
                values = exact_json(config["config_json"])
                actual = AsrProfile(profile["profile_key"], **decode_profile(config["config_json"]))
                if (config["schema_version"] != 2 or config["profile_id"] != profile["profile_id"]
                        or actual.canonical() != config["config_json"]
                        or hashlib.sha256(config["config_json"].encode("utf-8")).hexdigest() != profile["config_sha256"]
                        or any(values[name] != profile[name] for name in
                               ("model_name", "model_revision", "aligner_name", "device", "language"))):
                    raise ValueError("provenance: frozen ASR profile digest mismatch")
    elif kind == "audio":
        identity = record["identity"]
        for retained in record["retained"]:
            if retained["sha256"] != identity["sha256"] or (identity.get("byte_size") is not None and retained["byte_size"] != identity["byte_size"]):
                raise ValueError("provenance: retained audio identity mismatch")
    elif kind in {"observation", "metadata-acceptance"}:
        metadata_evidence.observation(node)
        if kind == "metadata-acceptance" or (is_acceptance(node) and node["dependencies"]):
            metadata_evidence.acceptance(node)


def verify_graph(value: dict) -> dict:
    required = {"format", "schema_version", "selection_policy", "reader_version", "selector", "roots", "nodes", "graph_sha256"}
    if (not isinstance(value, dict) or set(value) != required or value["format"] != FORMAT
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["reader_version"]) is not int or value["reader_version"] != 1
            or value["selection_policy"] != POLICY):
        raise ValueError("provenance: unsupported evidence contract")
    from bili_asr.contracts.json_schema import validate_json
    validate_json(FORMAT, value)
    if digest({key: item for key, item in value.items() if key != "graph_sha256"}) != value["graph_sha256"]:
        raise ValueError("provenance: graph digest mismatch")
    if not isinstance(value["nodes"], list) or not 1 <= len(value["nodes"]) <= MAX_NODES:
        raise ValueError("provenance: invalid node list")
    nodes = {}
    for node in value["nodes"]:
        if (not isinstance(node, dict) or set(node) != {"kind", "identity", "record", "dependencies", "sha256"}
                or node["kind"] not in KINDS or not isinstance(node["record"], dict)
                or not isinstance(node["identity"], str) or not isinstance(node["dependencies"], list)
                or any(not isinstance(key, str) or _HASH.fullmatch(key) is None for key in node["dependencies"])
                or node["dependencies"] != sorted(set(node["dependencies"]))):
            raise ValueError("provenance: invalid node contract")
        identity = digest({key: item for key, item in node.items() if key != "sha256"})
        if identity != node["sha256"] or identity in nodes:
            raise ValueError("provenance: node digest mismatch or duplicate")
        nodes[identity] = node
    if list(nodes) != sorted(nodes) or not isinstance(value["roots"], list) or not value["roots"]:
        raise ValueError("provenance: unsorted nodes or empty roots")
    reachable, active = set(), set()
    def visit(key, depth=0):
        if key not in nodes or key in active or depth > 256:
            raise ValueError("provenance: missing dependency or cycle")
        if key in reachable:
            return
        active.add(key)
        for dependency in nodes[key]["dependencies"]:
            visit(dependency, depth + 1)
        active.remove(key)
        reachable.add(key)
    for key in value["roots"]:
        visit(key)
    if reachable != set(nodes):
        raise ValueError("provenance: unbound evidence outside dependency closure")
    selector = value["selector"]
    if selector["kind"] != "artifact":
        if len(value["roots"]) != 1 or (nodes[value["roots"][0]]["kind"], nodes[value["roots"][0]]["identity"]) != (selector["kind"], selector["value"]):
            raise ValueError("provenance: root differs from exact selector")
    elif not any(node["kind"] == "artifact" and node["record"].get("sha256") == selector["value"] for node in nodes.values()):
        raise ValueError("provenance: final artifact selector is unavailable")
    try:
        metadata_evidence = MetadataEvidence(nodes)
        for node in nodes.values():
            _verify_domain(node, nodes, metadata_evidence)
        metadata_evidence.require_legacy_sources()
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as error:
        raise ValueError("provenance: invalid domain evidence") from error
    return {"valid": True, "graph_sha256": value["graph_sha256"], "node_count": len(nodes),
            "verification_scope": "embedded-records-and-dependency-digests", "audio_bytes_embedded": False}


def read_graph(path: Path) -> dict:
    path = checked_path(path)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("provenance: evidence file is missing or exceeds byte limit")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("provenance: evidence exceeds byte limit")
    value = exact_json(raw.decode("utf-8"))
    verify_graph(value)
    return value


def write_graph(path: Path, value: dict) -> dict:
    report = verify_graph(value)
    content = json_bytes(value)
    if len(content) > MAX_BYTES:
        raise ValueError("provenance: evidence exceeds byte limit")
    write_new_file(checked_path(path), content)
    return dict(report, output=str(path))
