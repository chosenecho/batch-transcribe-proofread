"""Versioned request budgets and explainable, frozen chunk boundaries.

New inputs use complete serialized requests in one token unit. Legacy input
planners remain unchanged; their saved boundaries and calls are never rebuilt.
"""
from __future__ import annotations

import math
import re
from copy import deepcopy
from itertools import pairwise

from bili_asr.canonical_json import canonical, digest

POLICY = "token-budget-v2"
ESTIMATOR = "serialized-request-utf8/3-v1"
OUTPUT_GUARD = "conservative-utf8-output-reserve/v1"
_PARAGRAPH_END = re.compile(r"[。！？.!?][\"'”’）)]*$")


class BudgetExceeded(ValueError):
    error_code = "editorial_budget_exceeded"

    def __init__(self, ordinal, limits):
        self.safe_details = {"stage": "editorial_planning", "reason_code": "single_segment_budget",
                             "segment_ordinal": ordinal, "limits": list(limits), "budget_policy": POLICY}
        super().__init__("one segment plus references/context exceeds the configured budget: " + ",".join(limits))


def admit_request(request, config):
    """Check the actual initial/recovery wire before reserving a physical send."""
    if config.budget_policy != POLICY:
        return request
    tokens = math.ceil((len(canonical(request).encode("utf-8")) + 256) / 3)
    limit = min(config.max_input_tokens, config.context_tokens - config.max_output_tokens - config.safety_tokens)
    if tokens > limit:
        from bili_asr.workflow_errors import JobExecutionError
        raise JobExecutionError("editorial_budget_exceeded", {"stage": "editorial_request_budget",
            "reason_code": "serialized_request_tokens_exceeded", "request_tokens": tokens,
            "input_token_limit": limit, "budget_policy": POLICY, "estimator": ESTIMATOR})
    return request


def estimate(chunk, config, system_prompt):
    """Freeze a fixed-point estimate including the estimate's wire overhead.

    The output guard retains the historical byte reserve as a conservative
    token ceiling. Its separate name prevents claiming it is provider usage.
    """
    from bili_asr.editorial_input_contract import estimate_chunk_budget
    from bili_asr.editorial_request import request_body

    original = {name: value for name, value in chunk.items() if name != "budget_estimate"}
    result = estimate_chunk_budget(original, system_prompt=system_prompt, max_output_tokens=config.max_output_tokens)
    result.update(policy_version=POLICY, method=ESTIMATOR, output_guard_method=OUTPUT_GUARD,
                  output_reserve_tokens=result["estimated_output_bytes"])
    for _ in range(12):
        wire = canonical(request_body(dict(original, budget_estimate=result), config, system_prompt=system_prompt))
        # Preserve the existing framing reserve in addition to actual wire JSON.
        byte_size = len(wire.encode("utf-8")) + 256
        tokens = math.ceil(byte_size / 3)
        if result["request_bytes"] == byte_size and result["request_tokens"] == tokens:
            return result
        result.update(request_bytes=byte_size, request_tokens=tokens)
    raise ValueError("serialized request estimate did not converge")


def exceeded_limits(chunk, config):
    value = chunk["budget_estimate"]
    limits = []
    input_limit = min(config.max_input_tokens, config.context_tokens - config.max_output_tokens - config.safety_tokens)
    for name, actual, maximum in (
        ("input_tokens", value["request_tokens"], input_limit),
        ("output_reserve_tokens", value["output_reserve_tokens"], config.max_output_tokens),
        ("editable_segments", len(chunk["editable_segments"]), config.max_chunk_segments),
        ("editable_characters", sum(len(s["text"]) for s in chunk["editable_segments"]), config.max_chunk_chars),
    ):
        if actual > maximum:
            limits.append(name)
    return limits


def _build_chunk(snapshot, config, matches, start, end, *, include_precheck):
    from bili_asr.editorial_input_contract import local_precheck
    body = snapshot["base"]["segments"]
    refs = [] if snapshot["reference"] is None else snapshot["reference"]["segments"]
    selected = {ref["segment_id"] for segment in body[start:end] for ref in matches[segment["segment_id"]]}
    chunk = {"chunk_id": "0" * 64,
             "editable_segments": [dict(segment, allowed_issue_refs=[segment["segment_id"]] +
                [ref["segment_id"] for ref in matches[segment["segment_id"]]]) for segment in body[start:end]],
             "reference_segments": [ref for ref in refs if ref["segment_id"] in selected],
             "readonly_context": body[max(0, start - config.context_segments):start] + body[end:end + config.context_segments]}
    if include_precheck:
        chunk["local_precheck"] = [dict(local_precheck(s["text"]), segment_id=s["segment_id"])
                                   for s in chunk["editable_segments"] if local_precheck(s["text"])["flags"]]
    chunk["budget_estimate"] = estimate(chunk, config, snapshot["system_prompt"])
    return chunk


def _final_chunk(snapshot, config, matches, start, end, *, include_precheck):
    chunk = _build_chunk(snapshot, config, matches, start, end, include_precheck=include_precheck)
    chunk["chunk_id"] = digest({"input_id": digest(snapshot), "start": start, "end": end,
        "chunk": {k: v for k, v in chunk.items() if k not in {"chunk_id", "budget_estimate"}}})
    chunk["budget_estimate"] = estimate(chunk, config, snapshot["system_prompt"])
    if exceeded_limits(chunk, config):
        raise ValueError("final frozen request exceeded its planned budget")
    return chunk


def prepare_budgeted_input(snapshot, config, matches, *, include_precheck):
    snapshot = deepcopy(snapshot)
    body = snapshot["base"]["segments"]
    if not body:
        raise ValueError("base transcript has no segments")

    def make_chunk(start, end):
        return _build_chunk(snapshot, config, matches, start, end, include_precheck=include_precheck)

    boundaries, reasons, start = [0], [], 0
    while start < len(body):
        low, high, chosen = start + 1, min(len(body), start + config.max_chunk_segments), None
        while low <= high:
            end = (low + high) // 2
            if not exceeded_limits(make_chunk(start, end), config):
                chosen, low = end, end + 1
            else:
                high = end - 1
        if chosen is None:
            raise BudgetExceeded(start, exceeded_limits(make_chunk(start, start + 1), config))
        capacity_end = chosen
        capacity_limits = (["end_of_input"] if chosen == len(body)
                           else exceeded_limits(make_chunk(start, chosen + 1), config))
        logical = False
        if chosen < len(body):
            minimum = start + max(1, math.ceil((chosen - start) * .8))
            for end in range(chosen, minimum - 1, -1):
                if (_PARAGRAPH_END.search(body[end - 1]["text"].rstrip())
                        or body[end]["start_ms"] - body[end - 1]["end_ms"] >= 1200):
                    chosen, logical = end, True
                    break
        boundaries.append(chosen)
        reasons.append({"capacity_end": capacity_end, "capacity_limits": capacity_limits,
                        "logical_boundary": logical})
        start = chosen
    snapshot["budget_plan"] = {
        "policy_version": POLICY, "estimator": ESTIMATOR, "output_guard": OUTPUT_GUARD,
        "preset": config.chunk_preset, "boundaries": boundaries, "cut_reasons": reasons,
        "input_tokens": min(config.max_input_tokens, config.context_tokens - config.max_output_tokens - config.safety_tokens),
        "output_tokens": config.max_output_tokens, "safety_tokens": config.safety_tokens,
        "max_chunk_segments": config.max_chunk_segments, "max_chunk_characters": config.max_chunk_chars,
        "provider_capability": {"status": "config_only", "endpoint": config.base_url, "model": config.model,
                                "context_tokens": config.context_tokens, "max_output_tokens": config.max_output_tokens},
    }
    identity = digest(snapshot)
    chunks = [_final_chunk(snapshot, config, matches, start, end, include_precheck=include_precheck)
              for start, end in pairwise(boundaries)]
    return {"input_id": identity, "snapshot": snapshot, "chunks": chunks}


def replan(prepared, config):
    """Compare scheduling over the same frozen sources, metadata and prompt."""
    snapshot = deepcopy(prepared["snapshot"])
    snapshot.pop("budget_plan", None)
    snapshot["config"] = config.to_dict()
    body = snapshot["base"]["segments"]
    refs = [] if snapshot["reference"] is None else snapshot["reference"]["segments"]
    matches = {s["segment_id"]: [r for r in refs if r["start_ms"] < s["end_ms"] and r["end_ms"] > s["start_ms"]] for s in body}
    return prepare_budgeted_input(snapshot, config, matches, include_precheck="source_rule_version" not in snapshot)


def require_frozen_plan(prepared):
    """New derived chunk caches must equal their complete hashed plan.

    Historical inputs retain their old optional estimates and boundaries.
    """
    from bili_asr.editorial_input_contract import EditorialConfig
    snapshot = prepared["snapshot"]
    if snapshot["config"].get("budget_policy", "legacy-v1") != POLICY:
        return
    if snapshot.get("budget_plan", {}).get("policy_version") != POLICY:
        raise ValueError("frozen editorial budget plan is missing or unsupported")
    config = EditorialConfig.from_snapshot(snapshot["config"])
    body = snapshot["base"]["segments"]
    boundaries = snapshot["budget_plan"]["boundaries"]
    if (config.budget_policy != POLICY or not boundaries or boundaries[0] != 0 or boundaries[-1] != len(body)
            or any(type(value) is not int for value in boundaries) or len(boundaries) != len(prepared["chunks"]) + 1
            or any(start >= end for start, end in pairwise(boundaries))):
        raise ValueError("frozen editorial budget plan or derived chunks changed")
    refs = [] if snapshot["reference"] is None else snapshot["reference"]["segments"]
    matches = {s["segment_id"]: [r for r in refs if r["start_ms"] < s["end_ms"] and r["end_ms"] > s["start_ms"]] for s in body}
    # Reconstruct only frozen intervals, without running boundary search again.
    for chunk, (start, end) in zip(prepared["chunks"], pairwise(boundaries), strict=True):
        expected = _final_chunk(snapshot, config, matches, start, end, include_precheck="source_rule_version" not in snapshot)
        if chunk != expected:
            raise ValueError("frozen editorial budget plan or derived chunks changed")


def plan_report(prepared):
    """Bounded text-free planning diagnostics; estimates do not promise savings."""
    plan = prepared["snapshot"]["budget_plan"]
    chunks = []
    for index, chunk in enumerate(prepared["chunks"]):
        chunks.append({"chunk_id": chunk["chunk_id"], "start_ordinal": plan["boundaries"][index],
                       "end_ordinal": plan["boundaries"][index + 1], "budget": chunk["budget_estimate"],
                       **plan["cut_reasons"][index]})
    return {"input_id": prepared["input_id"], "snapshot_sha256": digest(prepared["snapshot"]),
            "budget_plan": plan, "planned_calls": len(chunks), "chunks": chunks,
            "physical_calls_max": len(chunks) * (1 + prepared["snapshot"]["config"]["max_chunk_regenerations"]),
            "estimated_cost_range": {"status": "unknown", "reason": "no_dated_price_evidence"},
            "provider_usage": "unmeasured", "semantic_quality": "not_reviewed"}
