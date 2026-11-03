"""Human-readable projections derived solely from a verified portable graph."""
from __future__ import annotations

from bili_asr.canonical_json import digest
from bili_asr.metadata_acceptance import CONTRACT
from bili_asr.provenance_contract import exact_json, verify_graph
from bili_asr.provenance_metadata import is_acceptance


def _children(node, nodes, kind):
    return [nodes[key] for key in node["dependencies"] if nodes[key]["kind"] == kind]


def _accepted_fields(node) -> dict:
    fields = exact_json(node["record"]["payload_json"])["fields"]
    return {"observation_id": node["identity"], "record_sha256": node["sha256"],
            "owner": "application", "codec": CONTRACT,
            "payload_sha256": node["record"]["payload_sha256"],
            "envelope_sha256": node["record"]["envelope_sha256"],
            "fields": [field["field"] for field in fields],
            "source_observation_ids": sorted({field["source"]["observationId"] for field in fields})}


def _call(node, selected: set[str]) -> dict:
    from bili_asr.deepseek import normalize_usage
    row = node["record"]["row"]
    stored = exact_json(row["request_json"])
    request = stored.get("request", stored)
    response = exact_json(row["response_json"]) if row["response_json"] is not None else None
    kind = stored.get("invocation_kind", "provider")
    if response is not None and response.get("id") == "local-precheck":
        kind = "local_precheck"
    return {"call_id": row["call_id"], "attempt_id": row["attempt_id"], "chunk_id": row["chunk_id"],
            "selected_checkpoint": row["call_id"] in selected, "invocation_kind": kind,
            "physical_provider_send": kind == "provider", "replay_of_call_id": stored.get("replay_of_call_id"),
            "requested_model": request.get("model"), "returned_model": response.get("model") if response else None,
            # A provider's model label does not prove an immutable deployment revision.
            "immutable_model_revision": {"state": "unknown", "value": None},
            "request_sha256": digest(request), "record_sha256": node["sha256"],
            "usage": normalize_usage(response) if response else None,
            "started_at": row["started_at"], "finished_at": row["finished_at"], "error_code": row["error_code"]}


def _producer(node, nodes) -> dict:
    record = node["record"]
    evidence = exact_json(record["evidence"]["evidence_json"]) if record["evidence"] else {}
    provenance = evidence.get("provenance", {})
    diagnostics = evidence.get("diagnostics", {})
    workflow = _children(node, nodes, "workflow-attempt")
    distinct = {digest(item["record"]["profile"]): item["record"]["profile"]
                for item in workflow if item["record"]["profile"] is not None}
    profiles = [distinct[key] for key in sorted(distinct)]
    profile = profiles[0] if len(profiles) == 1 else None
    return {"run_id": record["run"]["run_id"], "kind": record["run"]["kind"],
            "outcome": record["attempt"]["outcome"], "transcript_id": record["attempt"]["transcript_id"],
            "started_at": record["attempt"]["started_at"], "finished_at": record["attempt"]["finished_at"],
            "asr_evidence_state": "recorded" if evidence else "unavailable_legacy",
            "requested_model": profile["model_name"] if profile else provenance.get("model_name"),
            "requested_model_revision": profile["model_revision"] if profile else provenance.get("model_revision"),
            "resolved_model_revision": diagnostics.get("resolved_model_revision"),
            "aligner": profile["aligner_name"] if profile else provenance.get("aligner_name"),
            "resolved_aligner_revision": diagnostics.get("resolved_aligner_revision"),
            "config_sha256": evidence.get("config_sha256"), "runtime_binding": evidence.get("runtime_binding"),
            "runtime": diagnostics.get("environment"), "execution_policy": diagnostics.get("execution_policy"),
            "workflow_profiles": profiles,
            "audio": [{"record_sha256": audio["sha256"], **audio["record"]} for audio in _children(node, nodes, "audio")],
            "workflow_attempt_ids": [item["record"]["attempt"]["attempt_id"] for item in workflow],
            "record_sha256": node["sha256"]}


def _selection(attempt_nodes, revision, producers) -> dict:
    candidates, selections = [], []
    for node in attempt_nodes:
        attempt = node["record"]["attempt"]
        result = exact_json(attempt["result_json"]) if attempt["result_json"] is not None else {}
        if (result.get("input_id"), result.get("revision_id")) != (revision["input_id"], revision["revision_id"]):
            continue
        candidates.append(attempt["attempt_id"])
        selected = result.get("source_selection")
        if selected is None:
            continue
        matches = [producer for producer in producers if producer["run_id"] == selected.get("run_id")
                   and producer["transcript_id"] == selected.get("transcript_id")
                   and selected.get("attempt_id") in producer["workflow_attempt_ids"]]
        if selected.get("format") != "editorial-source-selection/v1" or len(matches) != 1:
            raise ValueError("provenance: recorded source selection differs from producer closure")
        selections.append(dict(selected, proofread_attempt_id=attempt["attempt_id"]))
    return {"state": "recorded" if selections else "unavailable_unfrozen_history",
            "selections": selections, "proofread_attempt_ids": candidates}


def report_graph(graph: dict) -> dict:
    verification = verify_graph(graph)
    nodes = {node["sha256"]: node for node in graph["nodes"]}
    revisions = []
    for node in nodes.values():
        if node["kind"] != "revision":
            continue
        revision = node["record"]["row"]
        frozen = _children(node, nodes, "input")[0]
        prepared = exact_json(frozen["record"]["row"]["prepared_json"])
        snapshot = prepared["snapshot"]
        selected = {item["record"]["row"]["call_id"] for item in _children(node, nodes, "checkpoint")}
        transcripts, all_producers = [], []
        for transcript in _children(frozen, nodes, "transcript"):
            row = transcript["record"]["row"]
            producers = [_producer(item, nodes) for item in _children(transcript, nodes, "producer")]
            all_producers.extend(producers)
            transcripts.append({"role": "base" if row["transcript_id"] == snapshot["base"]["transcript_id"] else "reference",
                "transcript_id": row["transcript_id"], "content_sha256": row["content_sha256"],
                "content_deduplicated": len(producers) > 1, "producer_state": "recorded" if producers else "unavailable_legacy",
                "producers": producers, "record_sha256": transcript["sha256"]})
        source = _children(frozen, nodes, "source")[0]
        observations = [nodes[key] for key in source["dependencies"]]
        acceptances = [item for item in observations if is_acceptance(item)]
        selected_fields = sorted(field["field"] for item in acceptances
                                 for field in exact_json(item["record"]["payload_json"])["fields"])
        revisions.append({"revision_id": revision["revision_id"], "created_at": revision["created_at"],
            "input_id": revision["input_id"], "input_created_at": frozen["record"]["row"]["created_at"],
            "requested_model": snapshot["config"].get("model"), "config": snapshot["config"],
            "config_sha256": digest(snapshot["config"]), "prompt_sha256": snapshot["prompt_sha256"],
            "input_version": frozen["record"]["version"], "source_metadata": source["record"],
            "metadata_selection": {"state": "recorded_fields" if acceptances else "unknown", "fields": selected_fields},
            "application_acceptance_digests": [_accepted_fields(item) for item in acceptances],
            "raw_observation_digests": [{"observation_id": item["identity"], "record_sha256": item["sha256"],
                "owner": "provider",
                "payload_sha256": item["record"]["payload_sha256"], "envelope_sha256": item["record"]["envelope_sha256"]}
                for item in observations if not is_acceptance(item)],
            "transcripts": transcripts,
            "source_selection": _selection(_children(node, nodes, "workflow-attempt"), revision, all_producers),
            "calls": sorted((_call(item, selected) for item in _children(node, nodes, "call")),
                            key=lambda item: (item["started_at"], item["call_id"]))})
    return {**verification, "selector": graph["selector"],
            "subjects": [{"kind": nodes[key]["kind"], "identity": nodes[key]["identity"]} for key in graph["roots"]],
            "revisions": sorted(revisions, key=lambda item: item["revision_id"]),
            "editions": [{**node["record"]["edition"], "import_origin": node["record"].get("import_origin"),
                          "source_supplement": node["record"].get("source_supplement")}
                         for node in nodes.values() if node["kind"] == "edition"],
            "releases": [node["record"]["release"] for node in nodes.values() if node["kind"] == "release"],
            "artifacts": [{"record_sha256": node["sha256"], **{key: value for key, value in node["record"].items() if key != "text"}}
                          for node in nodes.values() if node["kind"] == "artifact"]}
