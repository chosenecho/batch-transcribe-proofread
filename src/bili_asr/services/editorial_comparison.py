"""Bounded preset comparisons over one frozen source, with raw provider evidence.

The dedicated experiment ledger owns all reservations. It never mutates the
archive or declares semantic quality from structural validation alone.
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from bili_asr.deepseek import DeepSeekClient, normalize_usage, parse_response
from bili_asr.editorial import EditorialConfig, canonical, digest, validate_revision
from bili_asr.editorial_budget import plan_report, replan, require_frozen_plan
from bili_asr.editorial_lineage import source_lineage
from bili_asr.editorial_quality import check_source_quality
from bili_asr.services.editorial_evaluation import _sum_usage, validation_result
from bili_asr.services.editorial_provider_evaluation import (
    MAX_CAMPAIGN_CALLS,
    _evaluate_variant,
    _initialize_ledger,
    _validate_calls,
    _validate_schema,
    _validate_variant_evidence,
)

SCHEMA = "editorial-preset-comparison/v1"
PRESETS = ("baseline-v1", "large-v1")
RUBRIC = ("completeness", "meaning", "terminology", "cross_chunk_continuity", "structure_failures")


def comparison_manifest(prepared, *, endpoint, model, maximum=8, max_output_tokens=None, pricing=None):
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 2 <= maximum <= MAX_CAMPAIGN_CALLS:
        raise ValueError("comparison physical budget must be between 2 and 20")
    snapshot = prepared["snapshot"]
    if digest(snapshot) != prepared["input_id"]:
        raise ValueError("comparison source input identity changed")
    require_frozen_plan(prepared)
    check_source_quality(prepared)
    source_chars = sum(len(s["text"]) for name in ("base", "reference")
                       for s in (snapshot[name] or {}).get("segments", []))
    if source_chars > 50_000:
        raise ValueError("comparison requires a frozen small sample of at most 50000 source characters")
    historic = EditorialConfig.from_snapshot(snapshot["config"])
    output = historic.max_output_tokens if max_output_tokens is None else max_output_tokens
    if isinstance(output, bool) or not isinstance(output, int) or not 1 <= output <= historic.max_output_tokens:
        raise ValueError("comparison output ceiling must be positive and cannot raise the frozen ceiling")
    common = replace(historic, base_url=endpoint, model=model, max_output_tokens=output,
                     budget_policy="token-budget-v2", request_policy="deepseek-v2",
                     max_chunk_regenerations=min(1, historic.max_chunk_regenerations),
                     timeout_seconds=min(historic.timeout_seconds, 180))
    candidates = {}
    for preset in PRESETS:
        guards = EditorialConfig.preset(preset)
        config = replace(common, chunk_preset=preset, max_chunk_segments=guards.max_chunk_segments,
                         max_chunk_chars=guards.max_chunk_chars)
        candidates[preset] = replan(prepared, config)
    planned = sum(len(candidate["chunks"]) for candidate in candidates.values())
    if planned > maximum:
        raise ValueError("comparison physical budget cannot cover every candidate chunk even once")
    return {"schema": SCHEMA, "source": deepcopy(prepared), "candidates": candidates,
            "max_physical_calls": maximum, "max_calls_per_chunk": 2, "pricing": _pricing(pricing)}


def _pricing(value):
    if value is None:
        return None
    expected = {"currency", "source", "observed_at", "input_cache_hit_per_million",
                "input_cache_miss_per_million", "output_per_million"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("pricing requires dated provenance, currency and all three rates")
    for key in ("currency", "source", "observed_at"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError("pricing provenance must be nonempty")
    _dated(value["observed_at"])
    for key in expected - {"currency", "source", "observed_at"}:
        if isinstance(value[key], bool) or not isinstance(value[key], (int, float)) or not math.isfinite(value[key]) or value[key] < 0:
            raise ValueError("pricing rates must be finite and nonnegative")
    return deepcopy(value)


def _dated(value):
    try:
        date = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("evidence date must be an ISO timestamp with an explicit timezone") from error
    if date.tzinfo is None:
        raise ValueError("evidence date must include an explicit timezone")


def _runs(manifest):
    runs = []
    for preset, prepared in manifest["candidates"].items():
        config = EditorialConfig.from_snapshot(prepared["snapshot"]["config"])
        for chunk in prepared["chunks"]:
            name = preset + ":" + chunk["chunk_id"]
            runs.append(({"prepared": prepared, "chunk": chunk}, name, config, preset))
    return runs


def _check_evidence(connection, manifest):
    # Rebuild from the saved source and compare the entire immutable plan before
    # inspecting any saved request or granting any new physical reservation.
    candidate = manifest["candidates"]["baseline-v1"]["snapshot"]["config"]
    rebuilt = comparison_manifest(manifest["source"], endpoint=candidate["base_url"], model=candidate["model"],
        maximum=manifest["max_physical_calls"], max_output_tokens=candidate["max_output_tokens"], pricing=manifest["pricing"])
    if canonical(rebuilt) != canonical(manifest):
        raise ValueError("comparison manifest or frozen candidate boundaries changed")
    runs = _runs(manifest)
    _validate_schema(connection)
    _validate_calls(connection, [run[1] for run in runs], manifest["max_physical_calls"])
    for sample, name, config, _ in runs:
        count = connection.execute("SELECT COUNT(*) FROM calls WHERE variant=?", (name,)).fetchone()[0]
        if count > 1 + config.max_chunk_regenerations:
            raise ValueError("comparison exceeds the frozen per-chunk regeneration budget")
        _validate_variant_evidence(connection, sample, name, config, None)
    return runs


def compare_presets(prepared, ledger: Path, *, endpoint, model, allow_paid_calls,
                    maximum=8, max_output_tokens=None, pricing=None, client=None):
    if not allow_paid_calls:
        raise ValueError("preset comparison requires --allow-paid-calls")
    manifest = comparison_manifest(prepared, endpoint=endpoint, model=model, maximum=maximum,
                                   max_output_tokens=max_output_tokens, pricing=pricing)
    runs = _runs(manifest)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(ledger)) as connection:
        connection.row_factory = sqlite3.Row
        _initialize_ledger(connection, manifest, [run[1] for run in runs], maximum)
        _check_evidence(connection, manifest)
        owned = client is None
        client = client or DeepSeekClient()
        try:
            for sample, name, config, _ in runs:
                _evaluate_variant(connection, sample, name, config, client, maximum=maximum,
                                  variant_names=[run[1] for run in runs], response_checksums=True)
        finally:
            if owned:
                client.close()
        return _report(connection, manifest)


def _open_ledger(ledger):
    connection = sqlite3.connect(Path(ledger).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _read_manifest(connection):
    _validate_schema(connection)
    rows = connection.execute("SELECT identity,body FROM manifest").fetchall()
    if len(rows) != 1:
        raise ValueError("comparison ledger requires exactly one manifest")
    manifest = json.loads(rows[0]["body"])
    if manifest.get("schema") != SCHEMA or digest(manifest) != rows[0]["identity"]:
        raise ValueError("comparison ledger identity changed")
    _check_evidence(connection, manifest)
    return manifest


def comparison_report(ledger: Path):
    with closing(_open_ledger(ledger)) as connection:
        return _report(connection, _read_manifest(connection))


def _actual_cost(calls, pricing):
    if pricing is None:
        return {"status": "unknown", "reason": "no_dated_price_evidence"}
    required = ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "completion_tokens")
    if any(call.get("usage", {}).get(key) is None for call in calls for key in required):
        return {"status": "unknown", "reason": "missing_provider_usage", "pricing": pricing}
    if any(call["usage"].get("prompt_tokens") is not None and call["usage"]["prompt_tokens"] !=
           call["usage"]["prompt_cache_hit_tokens"] + call["usage"]["prompt_cache_miss_tokens"] for call in calls):
        return {"status": "unknown", "reason": "inconsistent_provider_usage", "pricing": pricing}
    cost = sum(call["usage"]["prompt_cache_hit_tokens"] * pricing["input_cache_hit_per_million"]
        + call["usage"]["prompt_cache_miss_tokens"] * pricing["input_cache_miss_per_million"]
        + call["usage"]["completion_tokens"] * pricing["output_per_million"] for call in calls) / 1_000_000
    return {"status": "measured_usage_with_supplied_prices", "amount": cost, "pricing": pricing}


def _estimated_cost(prepared, pricing):
    if pricing is None:
        return {"status": "unknown", "reason": "no_dated_price_evidence"}
    chunks = prepared["chunks"]
    input_tokens = sum(chunk["budget_estimate"]["request_tokens"] for chunk in chunks)
    output_ceiling = len(chunks) * prepared["snapshot"]["config"]["max_output_tokens"]
    rates = (pricing["input_cache_hit_per_million"], pricing["input_cache_miss_per_million"])
    return {"status": "planning_estimate_not_provider_cap", "minimum": input_tokens * min(rates) / 1_000_000,
            "maximum_including_one_retry_per_chunk": 2 * (input_tokens * max(rates)
                + output_ceiling * pricing["output_per_million"]) / 1_000_000, "pricing": pricing}


def _report(connection, manifest):
    runs = _check_evidence(connection, manifest)
    variants = {}
    for preset, prepared in manifest["candidates"].items():
        calls, accepted, outputs = [], [], []
        for sample, name, config, run_preset in runs:
            if run_preset != preset:
                continue
            for row in connection.execute("SELECT * FROM calls WHERE variant=? ORDER BY ordinal", (name,)):
                response = None if row["response"] is None else json.loads(row["response"])
                item = {"ordinal": row["ordinal"], "chunk_id": sample["chunk"]["chunk_id"],
                        "request_sha256": digest(json.loads(row["request"])),
                        "endpoint": config.base_url, "requested_model": config.model,
                        "outcome": row["outcome"] or "unknown_send_consumed", "latency_ms": row["latency_ms"]}
                if response is not None:
                    item.update(response_sha256=digest(response), usage=normalize_usage(response),
                                validation=validation_result(sample["chunk"], response))
                    item["raw_usage"] = response.get("usage")
                    choices = response.get("choices")
                    item["finish_reason"] = (choices[0].get("finish_reason") if isinstance(choices, list)
                        and choices and isinstance(choices[0], dict) else None)
                    if item["validation"]["accepted"]:
                        accepted.append(sample["chunk"]["chunk_id"])
                        outputs.extend(parse_response(response)["paragraphs"])
                calls.append(item)
        plan = plan_report(prepared)
        counts = len(calls)
        variants[preset] = {"plan": plan, "physical_calls": counts,
            "retry_calls": counts - len({call["chunk_id"] for call in calls}),
            "structure_complete": len(accepted) == len(prepared["chunks"]),
            "accepted_chunks": len(accepted), "structure_failures": sum(
                not call.get("validation", {}).get("accepted", False) for call in calls),
            "observed_latency_ms": sum(call["latency_ms"] or 0 for call in calls),
            "missing_latency_counters": sum(call["latency_ms"] is None for call in calls),
            "usage": _sum_usage(calls), "actual_cost": _actual_cost(calls, manifest["pricing"]),
            "estimated_cost_range": _estimated_cost(prepared, manifest["pricing"]),
            "calls": calls, "accepted_paragraphs": outputs, "semantic_quality": "not_reviewed"}
    return {"schema": SCHEMA, "comparison_id": digest(manifest), "source_input_id": manifest["source"]["input_id"],
            "max_physical_calls": manifest["max_physical_calls"],
            "physical_calls": sum(v["physical_calls"] for v in variants.values()), "variants": variants,
            "provider_capability": "not_verified", "semantic_quality": "not_reviewed", "default_preset": EditorialConfig().chunk_preset}


def write_review_packet(ledger: Path, output: Path):
    with closing(_open_ledger(ledger)) as connection:
        manifest = _read_manifest(connection)
        report = _report(connection, manifest)
    lineage = {}
    for preset, prepared in manifest["candidates"].items():
        variant = report["variants"][preset]
        if variant["structure_complete"]:
            paragraphs = iter(variant["accepted_paragraphs"])
            blocks = []
            for chunk in prepared["chunks"]:
                count = len(chunk["editable_segments"])
                selected, covered = [], 0
                while covered < count:
                    paragraph = next(paragraphs)
                    selected.append(paragraph)
                    covered += len(paragraph["segment_ids"])
                blocks.extend(validate_revision(chunk, {"chunk_id": chunk["chunk_id"], "paragraphs": selected}))
            lineage[preset] = source_lineage(prepared, blocks)
        else:
            lineage[preset] = {"structure_status": "incomplete", "semantic_status": "unverified", "review_required": True}
    packet = {"schema": "editorial-preset-review/v1", "comparison_id": digest(manifest),
              "source_input_id": manifest["source"]["input_id"], "source_snapshot": manifest["source"]["snapshot"],
              "source_lineage": lineage,
              "report": report, "review_template": {"comparison_id": digest(manifest), "reviewer_kind": "human",
                "reviewer": "", "reviewed_at": "", "rubric": {preset: {criterion: {"verdict": "not_reviewed", "notes": ""}
                        for criterion in RUBRIC} for preset in PRESETS}}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(packet, ensure_ascii=False, indent=2) + "\n")
    return {"comparison_id": digest(manifest), "packet": str(output), "semantic_quality": "not_reviewed"}


def record_review(ledger: Path, review: dict, output: Path):
    report = comparison_report(ledger)
    if not isinstance(review, dict) or set(review) != {"comparison_id", "reviewer_kind", "reviewer", "reviewed_at", "rubric"}:
        raise ValueError("review requires identity, named reviewer, date and all rubric entries")
    if review["comparison_id"] != report["comparison_id"] or not isinstance(review["reviewer_kind"], str) or review["reviewer_kind"] not in {"human", "agent"}:
        raise ValueError("review identity or reviewer kind changed")
    if any(not isinstance(review[key], str) or not review[key].strip() for key in ("reviewer", "reviewed_at")):
        raise ValueError("review must name its reviewer and review date")
    _dated(review["reviewed_at"])
    if not isinstance(review["rubric"], dict) or set(review["rubric"]) != set(PRESETS):
        raise ValueError("review must cover both presets")
    for preset in PRESETS:
        if not isinstance(review["rubric"][preset], dict) or set(review["rubric"][preset]) != set(RUBRIC):
            raise ValueError("review must cover every semantic and structural criterion")
        for entry in review["rubric"][preset].values():
            if not isinstance(entry, dict) or set(entry) != {"verdict", "notes"} or not isinstance(entry["verdict"], str) or entry["verdict"] not in {"pass", "fail", "unknown"} or not isinstance(entry["notes"], str) or not entry["notes"].strip():
                raise ValueError("review entries require pass/fail/unknown and supporting notes")
        if not report["variants"][preset]["structure_complete"] and any(
            review["rubric"][preset][criterion]["verdict"] == "pass" for criterion in ("completeness", "structure_failures")):
            raise ValueError("incomplete comparison cannot claim completeness or structure passed")
    recorded = {"schema": "editorial-preset-review-record/v1", "comparison_id": report["comparison_id"],
                "review_sha256": digest(review), "review": deepcopy(review), "report_sha256": digest(report),
                "human_review_status": "recorded" if review["reviewer_kind"] == "human" else "not_reviewed"}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(recorded, ensure_ascii=False, indent=2) + "\n")
    return recorded
