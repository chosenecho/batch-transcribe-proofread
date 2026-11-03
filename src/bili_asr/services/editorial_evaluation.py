"""Read-only replay of exact saved editorial calls; never repairs provider text."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

from bili_asr.deepseek import (
    DeepSeekError,
    normalize_usage,
    parse_response,
    request_body,
)
from bili_asr.editorial import (
    EditorialConfig,
    RevisionValidationError,
    digest,
    estimate_chunk_budget,
    local_precheck,
    validate_revision,
)
from bili_asr.editorial_recovery import diagnostic_for_error, recovery_request
from bili_asr.storage.editorial import EditorialRepository


def load_sample(database: Path, call_id: str) -> dict[str, Any]:
    """Load immutable evidence without opening a writable archive connection."""
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT m.call_id,m.input_id,m.chunk_id,m.request_json,m.response_json,i.prepared_json "
            "FROM editorial_model_calls m JOIN editorial_inputs i USING(input_id) WHERE m.call_id=?",
            (call_id,),
        ).fetchone()
    if row is None or row["response_json"] is None:
        raise ValueError("saved call with a response is required")
    prepared = json.loads(row["prepared_json"])
    if prepared["input_id"] != row["input_id"] or digest(prepared["snapshot"]) != row["input_id"]:
        raise ValueError("saved input identity mismatch")
    chunk = next((item for item in prepared["chunks"] if item["chunk_id"] == row["chunk_id"]), None)
    if chunk is None:
        raise ValueError("saved call chunk is absent from frozen input")
    _validate_frozen_chunk(prepared, chunk)
    stored = json.loads(row["request_json"])
    kind = stored.get("invocation_kind", "provider")
    if kind != "provider" or stored.get("replay_of_call_id") is not None:
        raise ValueError("select an original physical provider call")
    request = stored.get("request", stored)
    response = json.loads(row["response_json"])
    if response.get("id") == "local-precheck":
        raise ValueError("select an original physical provider call")
    fingerprint = stored.get("request_fingerprint")
    if fingerprint is not None and fingerprint != EditorialRepository.request_fingerprint(
            row["input_id"], row["chunk_id"], request):
        raise ValueError("stored request fingerprint mismatch")
    if json.loads(request["messages"][1]["content"]) != chunk:
        raise ValueError("saved request does not match the frozen chunk")
    if request["messages"][0]["content"] != prepared["snapshot"]["system_prompt"]:
        raise ValueError("saved system prompt does not match frozen input")
    config = EditorialConfig.from_snapshot(prepared["snapshot"]["config"])
    original = request_body(chunk, config, system_prompt=prepared["snapshot"]["system_prompt"])
    # Recovery calls legitimately add feedback messages, but cannot silently
    # change the frozen provider controls or original two-message prefix.
    controls = dict(request, messages=request["messages"][:2])
    if controls != original:
        raise ValueError("saved request controls do not match frozen config")
    return {"call_id": row["call_id"], "prepared": prepared, "chunk": chunk,
            "request": request, "response": response,
            "request_fingerprint_evidence": "verified" if fingerprint is not None else "unavailable_legacy"}


def _validate_frozen_chunk(prepared: dict[str, Any], chunk: dict[str, Any]) -> None:
    """Verify mutable prepared chunk caches against the hashed source snapshot.

    Historical chunk planners had different splitting limits and optional
    estimate fields. Preserve their exact boundaries while deriving all source
    text, permissions, reference selection and context from the frozen input.
    """
    if set(chunk) - {"chunk_id", "editable_segments", "reference_segments", "readonly_context",
                     "local_precheck", "budget_estimate"}:
        raise ValueError("unknown frozen chunk fields")
    snapshot = prepared["snapshot"]
    body = snapshot["base"]["segments"]
    references = [] if snapshot["reference"] is None else snapshot["reference"]["segments"]
    editable = chunk["editable_segments"]
    if not editable:
        raise ValueError("empty frozen chunk")
    starts = [i for i, segment in enumerate(body) if segment["segment_id"] == editable[0]["segment_id"]]
    if len(starts) != 1:
        raise ValueError("frozen chunk source identity mismatch")
    start, end = starts[0], starts[0] + len(editable)
    expected = []
    selected = set()
    for segment in body[start:end]:
        matching = [ref["segment_id"] for ref in references
                    if ref["start_ms"] < segment["end_ms"] and ref["end_ms"] > segment["start_ms"]]
        selected.update(matching)
        expected.append(dict(segment, allowed_issue_refs=[segment["segment_id"]] + matching))
    context_size = EditorialConfig.from_snapshot(snapshot["config"]).context_segments
    context = body[max(0, start - context_size):start] + body[end:end + context_size]
    if (editable != expected or chunk["reference_segments"] != [r for r in references if r["segment_id"] in selected]
            or chunk["readonly_context"] != context):
        raise ValueError("frozen chunk content differs from hashed snapshot")
    if "local_precheck" in chunk:
        checks = [dict(local_precheck(s["text"]), segment_id=s["segment_id"]) for s in editable
                  if local_precheck(s["text"])["flags"]]
        if chunk["local_precheck"] != checks:
            raise ValueError("frozen chunk precheck differs from source")
    if "budget_estimate" in chunk:
        original = dict(chunk)
        original.pop("budget_estimate")
        config = EditorialConfig.from_snapshot(snapshot["config"])
        if config.budget_policy == "token-budget-v2":
            from bili_asr.editorial_budget import estimate
            expected_budget = estimate(original, config, snapshot["system_prompt"])
        else:
            expected_budget = estimate_chunk_budget(original, max_output_tokens=config.max_output_tokens)
        if chunk["budget_estimate"] != expected_budget:
            raise ValueError("frozen chunk estimate changed")
    expected_id = f"{prepared['input_id'][:16]}:{start}-{end}"
    if chunk["chunk_id"] != expected_id:
        ignored = {"chunk_id"}
        if snapshot.get("budget_plan", {}).get("policy_version") == "token-budget-v2":
            ignored.add("budget_estimate")
        v2_body = {k: v for k, v in chunk.items() if k not in ignored}
        if chunk["chunk_id"] != digest({"input_id": prepared["input_id"], "start": start, "end": end, "chunk": v2_body}):
            raise ValueError("frozen chunk boundary identity mismatch")


def validation_result(chunk: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    try:
        blocks = validate_revision(chunk, parse_response(response))
    except (DeepSeekError, RevisionValidationError) as error:
        # Provider messages and transcript prose never enter the public report.
        result = {"accepted": False, "error_type": type(error).__name__}
        diagnostic = diagnostic_for_error(error)
        if diagnostic is not None:
            result["diagnostic"] = diagnostic
        return result
    return {"accepted": True, "paragraphs": len(blocks)}


def replay_sample(sample: dict[str, Any]) -> dict[str, Any]:
    prepared, chunk = sample["prepared"], sample["chunk"]
    validation = validation_result(chunk, sample["response"])
    historic = EditorialConfig.from_snapshot(prepared["snapshot"]["config"])
    report = {"source_call_id": sample["call_id"], "source_input_id": prepared["input_id"],
              "source_chunk_id": chunk["chunk_id"], "source_request_sha256": digest(sample["request"]),
              "source_response_sha256": digest(sample["response"]),
              "source_request_policy": historic.request_policy,
              "request_fingerprint_evidence": sample["request_fingerprint_evidence"],
              "editable_segments": len(chunk["editable_segments"]),
              "validation": validation, "usage": normalize_usage(sample["response"]),
              "physical_calls": 0, "semantic_quality": "not_reviewed"}
    if validation.get("diagnostic"):
        # This is an explicitly identified new request preview, never a rewrite
        # of the historic input, prompt or provider response.
        config = replace(historic, request_policy="deepseek-v2", max_chunk_regenerations=1)
        original = request_body(chunk, config, system_prompt=prepared["snapshot"]["system_prompt"])
        request, recovery = recovery_request(original, {"call_id": sample["call_id"],
            "stored_request": {"request": sample["request"],
                               "validation_diagnostic": validation["diagnostic"]}}, config)
        report["feedback_preview"] = {"request_sha256": digest(request),
            "config_sha256": digest(config.to_dict()), "policy": recovery["policy"],
            "regeneration": recovery["regeneration"], "provider_called": False}
    return report


def historical_recovery_report(database: Path, call_ids: list[str], *, maximum_additional_calls: int = 2) -> dict[str, Any]:
    """Observe genuine later physical calls for the same immutable input/chunk.

    This retrospective cohort measures natural retries, not a counterfactual
    improvement from feedback. Missing follow-up is censored, never success.
    Only exact original controls are pooled; changed settings are excluded.
    """
    if not call_ids or len(set(call_ids)) != len(call_ids) or maximum_additional_calls != 2:
        raise ValueError("select distinct real failures with the fixed two-call observation window")
    reports = []
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        for call_id in call_ids:
            sample = load_sample(database, call_id)
            validation = validation_result(sample["chunk"], sample["response"])
            if not validation.get("diagnostic"):
                raise ValueError("historical recovery cohort requires recoverable original failures")
            first = connection.execute("SELECT rowid,* FROM editorial_model_calls WHERE call_id=?", (call_id,)).fetchone()
            rows = connection.execute("SELECT rowid,* FROM editorial_model_calls WHERE input_id=? AND chunk_id=? "
                "AND (started_at>? OR (started_at=? AND rowid>?)) ORDER BY started_at,rowid",
                (first["input_id"], first["chunk_id"], first["started_at"], first["started_at"], first["rowid"])).fetchall()
            calls, skipped, recovered = [], 0, False
            original_controls = dict(sample["request"], messages=sample["request"]["messages"][:2])
            for row in rows:
                stored = json.loads(row["request_json"])
                if stored.get("invocation_kind", "provider") != "provider" or stored.get("replay_of_call_id") is not None:
                    skipped += 1
                    continue
                request = stored.get("request", stored)
                fingerprint = stored.get("request_fingerprint")
                if fingerprint is not None and fingerprint != EditorialRepository.request_fingerprint(
                        first["input_id"], first["chunk_id"], request):
                    raise ValueError("historical follow-up request fingerprint mismatch")
                if dict(request, messages=request["messages"][:2]) != original_controls:
                    skipped += 1
                    continue
                if len(calls) == maximum_additional_calls:
                    break
                entry = {"call_id": row["call_id"], "request_sha256": digest(request),
                         "feedback_present": len(request["messages"]) > 2,
                         "outcome": "unknown_send_consumed", "validation": {"accepted": False}}
                if row["response_json"] is not None:
                    followup = load_sample(database, row["call_id"])
                    if followup["prepared"]["input_id"] != sample["prepared"]["input_id"] or followup["chunk"] != sample["chunk"]:
                        raise ValueError("historical follow-up identity changed")
                    entry.update(response_sha256=digest(followup["response"]), outcome="response_saved",
                                 validation=validation_result(sample["chunk"], followup["response"]),
                                 usage=normalize_usage(followup["response"]))
                calls.append(entry)
                if entry["validation"]["accepted"]:
                    recovered = True
                    break
            reports.append({"source_call_id": call_id, "source_input_id": first["input_id"],
                "source_chunk_id": first["chunk_id"], "editable_segments": len(sample["chunk"]["editable_segments"]),
                "source_validation": validation, "source_usage": normalize_usage(sample["response"]),
                "additional_physical_calls": len(calls), "recovered": recovered,
                "observation": "recovered" if recovered else ("budget_exhausted" if len(calls) == maximum_additional_calls else "censored_no_more_observed_calls"),
                "excluded_nonphysical_or_changed_controls": skipped, "calls": calls,
                "additional_usage": _sum_usage(calls)})
    observed = [r for r in reports if r["additional_physical_calls"]]
    return {"schema": "editorial-historical-recovery-v1", "mode": "retrospective_natural_retry",
        "source_failures": len(reports), "failures_with_followup": len(observed),
        "recoveries": sum(r["recovered"] for r in observed),
        "recovery_rate_among_observed": None if not observed else sum(r["recovered"] for r in observed) / len(observed),
        "new_provider_calls": 0, "maximum_additional_calls_per_failure": maximum_additional_calls,
        "additional_usage": _sum_usage([c for r in reports for c in r["calls"]]),
        "samples": reports, "causal_feedback_improvement": "not_established", "semantic_quality": "not_reviewed"}


def _sum_usage(calls: list[dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        values = [call.get("usage", {}).get(key) for call in calls]
        result[key] = {"known_total": sum(v for v in values if v is not None),
                       "missing_call_counters": sum(v is None for v in values)}
    return result
