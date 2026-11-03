"""Explicitly opted-in experiments with durable, bounded physical reservations."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

from bili_asr.deepseek import (
    DeepSeekClient,
    DeepSeekError,
    normalize_usage,
    parse_response,
    request_body,
)
from bili_asr.editorial import EditorialConfig, canonical, digest
from bili_asr.editorial_recovery import recovery_request
from bili_asr.services.editorial_evaluation import _sum_usage, validation_result

MAX_CALLS = 4
MAX_OUTPUT_TOKENS = 8192
MAX_CAMPAIGN_CALLS = 20
VARIANT_NAMES = ("disabled-temperature-0.2", "enabled-low")
LEDGER_TABLES = {
    "manifest": "CREATE TABLE manifest(identity TEXT PRIMARY KEY, body TEXT NOT NULL)",
    "calls": "CREATE TABLE calls(ordinal INTEGER PRIMARY KEY, variant TEXT NOT NULL, "
             "request TEXT NOT NULL, response TEXT, outcome TEXT, latency_ms INTEGER)",
}


def _validate_schema(connection):
    for name, expected in LEDGER_TABLES.items():
        row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        if row is None or " ".join(row[0].upper().split()) != " ".join(expected.upper().split()):
            raise ValueError("experiment ledger schema changed")
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view')").fetchone():
        raise ValueError("experiment ledger schema changed")


def _validate_calls(connection, variant_names, maximum=MAX_CALLS):
    rows = connection.execute("SELECT ordinal,variant FROM calls ORDER BY ordinal").fetchall()
    if len(rows) > maximum or [row["ordinal"] for row in rows] != list(range(1, len(rows) + 1)):
        raise ValueError("experiment ledger ordinals or budget changed")
    counts = {name: 0 for name in variant_names}
    for row in rows:
        if row["variant"] not in counts:
            raise ValueError("experiment ledger has unknown variant")
        counts[row["variant"]] += 1
    if any(count > 2 for count in counts.values()):
        raise ValueError("experiment ledger variant budget changed")


def _initialize_ledger(connection, manifest, variant_names, maximum):
    # Schema inspection, both tables and the immutable manifest form one
    # transaction. Another process must never observe a half-created ledger.
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and tables != {"manifest", "calls"}:
            raise ValueError("ledger is not a dedicated provider experiment database")
        for statement in LEDGER_TABLES.values():
            connection.execute(statement.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
        _validate_schema(connection)
        _validate_calls(connection, variant_names, maximum)
        existing = connection.execute("SELECT identity,body FROM manifest").fetchall()
        if existing and (len(existing) != 1 or existing[0][0] != digest(manifest)
                         or existing[0][1] != canonical(manifest)):
            raise ValueError("experiment identity or budget changed; existing ledger cannot be reset")
        if not existing and connection.execute("SELECT 1 FROM calls LIMIT 1").fetchone():
            raise ValueError("experiment ledger calls have no manifest")
        connection.execute("INSERT OR IGNORE INTO manifest VALUES (?,?)", (digest(manifest), canonical(manifest)))


def variants(sample: dict[str, Any], endpoint: str, model: str) -> list[tuple[str, EditorialConfig]]:
    historic = EditorialConfig.from_snapshot(sample["prepared"]["snapshot"]["config"])
    common = replace(historic, base_url=endpoint, model=model, request_policy="deepseek-v2",
                     max_chunk_regenerations=1, max_output_tokens=MAX_OUTPUT_TOKENS,
                     timeout_seconds=180, temperature=None)
    return [(VARIANT_NAMES[0], replace(common, thinking_mode="disabled", temperature=.2)),
            (VARIANT_NAMES[1], replace(common, thinking_mode="enabled", reasoning_effort="low"))]


def evaluate_provider(sample: dict[str, Any], ledger: Path, *, endpoint: str, model: str,
                      allow_paid_calls: bool, client: DeepSeekClient | None = None) -> dict[str, Any]:
    if not allow_paid_calls:
        raise ValueError("provider experiments require --allow-paid-calls")
    configs = variants(sample, endpoint, model)
    manifest = {"schema": "editorial-provider-evaluation-v1", "source_call": sample["call_id"],
                "source_request_sha256": digest(sample["request"]), "source_response_sha256": digest(sample["response"]),
                "source_chunk_sha256": digest(sample["chunk"]), "max_physical_calls": MAX_CALLS,
                "variants": {name: config.to_dict() for name, config in configs}}
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(ledger)) as connection:
        connection.row_factory = sqlite3.Row
        _initialize_ledger(connection, manifest, VARIANT_NAMES, MAX_CALLS)
        owned = client is None
        client = client or DeepSeekClient()
        try:
            for name, config in configs:
                _evaluate_variant(connection, sample, name, config, client)
        finally:
            if owned:
                client.close()
        reports = []
        for row in connection.execute("SELECT * FROM calls ORDER BY ordinal"):
            response = None if row["response"] is None else json.loads(row["response"])
            entry = {"ordinal": row["ordinal"], "variant": row["variant"],
                     "request_sha256": digest(json.loads(row["request"])),
                     "outcome": row["outcome"] or "unknown_send_consumed", "latency_ms": row["latency_ms"],
                     "http_response_accepted": response is not None}
            if response is not None:
                entry.update(response_sha256=digest(response), validation=validation_result(sample["chunk"], response),
                             usage=normalize_usage(response))
            reports.append(entry)
    return {"schema": manifest["schema"], "experiment_id": digest(manifest), "source_call_id": sample["call_id"],
            "endpoint": endpoint, "model": model, "max_physical_calls": MAX_CALLS,
            "max_output_tokens_per_call": MAX_OUTPUT_TOKENS, "physical_calls": len(reports),
            "calls": reports, "provider_parameter_enforcement": "not_verified", "semantic_quality": "not_reviewed"}


def evaluate_campaign(samples: list[dict[str, Any]], ledger: Path, *, endpoint: str, model: str,
                      allow_paid_calls: bool, client: DeepSeekClient | None = None) -> dict[str, Any]:
    """One immutable, global budget for up to five original historical chunks.

    Failed historical chunks start with their saved validator feedback. They
    are explicitly new configurations, never claimed as exact request replay.
    Each source/mode has at most two new calls; all reservations share one DB.
    """
    if not allow_paid_calls:
        raise ValueError("provider experiments require --allow-paid-calls")
    if not 1 <= len(samples) <= 5 or len({s["call_id"] for s in samples}) != len(samples):
        raise ValueError("campaign requires one to five distinct historical samples")
    names, runs, sources = [], [], []
    for sample in samples:
        diagnostic = validation_result(sample["chunk"], sample["response"]).get("diagnostic")
        configurations = variants(sample, endpoint, model)
        if diagnostic:
            configurations = [(n, replace(c, max_chunk_regenerations=2)) for n, c in configurations]
        sources.append({"source_call_id": sample["call_id"],
            "source_input_id": sample["prepared"]["input_id"],
            "request_sha256": digest(sample["request"]), "response_sha256": digest(sample["response"]),
            "chunk_sha256": digest(sample["chunk"]), "historical_feedback": diagnostic,
            "variants": {n: c.to_dict() for n, c in configurations}})
        for name, config in configurations:
            key = sample["call_id"] + ":" + name
            names.append(key)
            runs.append((sample, key, config, diagnostic))
    manifest = {"schema": "editorial-provider-campaign-v1", "sources": sources,
                "max_physical_calls": MAX_CAMPAIGN_CALLS,
                "max_calls_per_source_variant": 2, "max_output_tokens_per_call": MAX_OUTPUT_TOKENS,
                "timeout_seconds": 180}
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(ledger)) as connection:
        connection.row_factory = sqlite3.Row
        _initialize_ledger(connection, manifest, names, MAX_CAMPAIGN_CALLS)
        # Check every persisted request and completed response before any new
        # sends. A changed later sample cannot consume earlier unused slots.
        for sample, name, config, diagnostic in runs:
            _validate_variant_evidence(connection, sample, name, config, diagnostic)
        owned = client is None
        client = client or DeepSeekClient()
        try:
            for sample, name, config, diagnostic in runs:
                _evaluate_variant(connection, sample, name, config, client,
                    maximum=MAX_CAMPAIGN_CALLS, variant_names=names, initial_diagnostic=diagnostic,
                    response_checksums=True)
        finally:
            if owned:
                client.close()
        reports = []
        for sample in samples:
            calls = []
            for row in connection.execute("SELECT * FROM calls WHERE variant LIKE ? ORDER BY ordinal",
                                           (sample["call_id"] + ":%",)):
                response = None if row["response"] is None else json.loads(row["response"])
                item = {"ordinal": row["ordinal"], "variant": row["variant"].split(":", 1)[1],
                        "request_sha256": digest(json.loads(row["request"])),
                        "outcome": "response_saved" if response is not None else (row["outcome"] or "unknown_send_consumed"), "latency_ms": row["latency_ms"],
                        "http_response_accepted": response is not None}
                if response is not None:
                    item.update(response_sha256=digest(response), validation=validation_result(sample["chunk"], response),
                                usage=normalize_usage(response))
                    choices = response.get("choices")
                    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
                    item["finish_reason"] = choice.get("finish_reason")
                    message = choice.get("message", {})
                    message = message if isinstance(message, dict) else {}
                    reasoning = message.get("reasoning_content")
                    item["reasoning_content_observed"] = isinstance(reasoning, str) and bool(reasoning)
                    item["reasoning_content_chars"] = len(reasoning) if isinstance(reasoning, str) else None
                calls.append(item)
            reports.append({"source_call_id": sample["call_id"],
                "source_input_id": sample["prepared"]["input_id"], "source_chunk_id": sample["chunk"]["chunk_id"],
                "editable_segments": len(sample["chunk"]["editable_segments"]),
                "historical_validation": validation_result(sample["chunk"], sample["response"]),
                "historical_feedback_seeded": bool(validation_result(sample["chunk"], sample["response"]).get("diagnostic")),
                "calls": calls})
    return {"schema": manifest["schema"], "experiment_id": digest(manifest),
            "endpoint": endpoint, "model": model, "max_physical_calls": MAX_CAMPAIGN_CALLS,
            "max_output_tokens_per_call": MAX_OUTPUT_TOKENS, "timeout_seconds": 180,
            "physical_calls": sum(len(s["calls"]) for s in reports), "samples": reports,
            "variant_summaries": _campaign_summary(reports),
            "provider_parameter_enforcement": "not_verified", "semantic_quality": "not_reviewed",
            "comparison": "endpoint_mode_smoke_and_historical_feedback_not_causal_quality_estimate"}


def _campaign_summary(samples):
    summaries = {}
    for variant in VARIANT_NAMES:
        calls = [c for sample in samples for c in sample["calls"] if c["variant"] == variant]
        failed = [s for s in samples if s["historical_feedback_seeded"]]
        recovered = sum(any(c.get("validation", {}).get("accepted") for c in s["calls"]
                            if c["variant"] == variant) for s in failed)
        summaries[variant] = {"physical_calls": len(calls),
            "http_responses_accepted": sum(c["http_response_accepted"] for c in calls),
            "strictly_accepted_samples": sum(any(c.get("validation", {}).get("accepted")
                for c in s["calls"] if c["variant"] == variant) for s in samples),
            "historically_failed_samples": len(failed), "historical_feedback_recoveries": recovered,
            "historical_feedback_recovery_rate": None if not failed else recovered / len(failed),
            "usage": _sum_usage(calls),
            "observed_latency_ms": sum(c["latency_ms"] for c in calls if c["latency_ms"] is not None),
            "missing_latency_counters": sum(c["latency_ms"] is None for c in calls)}
    return summaries


def write_review_packet(samples: list[dict[str, Any]], ledger: Path, output: Path) -> None:
    """Export private source/output pairs for named semantic review.

    Produces no quality claims. Reviewers must record provenance, rubric and
    concrete findings, and distinguish agent assessment from human review.
    """
    packets = []
    with closing(sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        _validate_schema(connection)
        for sample in samples:
            comparisons = []
            for row in connection.execute("SELECT * FROM calls WHERE variant LIKE ? ORDER BY ordinal",
                                           (sample["call_id"] + ":%",)):
                if row["response"] is None:
                    continue
                response = json.loads(row["response"])
                if row["outcome"] != "response_saved:" + digest(response):
                    raise ValueError("persisted experiment response changed")
                validation = validation_result(sample["chunk"], response)
                if validation["accepted"]:
                    comparisons.append({"ordinal": row["ordinal"], "variant": row["variant"].split(":", 1)[1],
                        "response_sha256": digest(response), "paragraphs": parse_response(response)["paragraphs"]})
            packets.append({"source_call_id": sample["call_id"], "source_input_id": sample["prepared"]["input_id"],
                "metadata": sample["prepared"]["snapshot"].get("metadata"), "source_chunk": sample["chunk"],
                "outputs": comparisons})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"schema": "editorial-private-semantic-review-v1",
        "semantic_quality": "not_reviewed", "reviewer_kind": None,
        "rubric": ["proper_name_miscorrection", "missed_correction", "meaning_change", "unsupported_addition"],
        "samples": packets}, ensure_ascii=False, indent=2), encoding="utf-8")


def _initial_previous(sample, diagnostic):
    if diagnostic is None:
        return None
    return {"call_id": sample["call_id"], "stored_request": {"request": sample["request"],
            "validation_diagnostic": diagnostic, "recovery": {"regeneration": 0}}}


def _validate_variant_evidence(connection, sample, name, config, diagnostic):
    original = request_body(sample["chunk"], config, system_prompt=sample["prepared"]["snapshot"]["system_prompt"])
    previous = _initial_previous(sample, diagnostic)
    for generation, row in enumerate(connection.execute("SELECT * FROM calls WHERE variant=? ORDER BY ordinal", (name,))):
        request, recovery = recovery_request(original, previous, config)
        if json.loads(row["request"]) != request:
            raise ValueError("persisted experiment request changed")
        if row["response"] is None:
            if row["outcome"] not in {None, "DeepSeekError", "DeepSeekTransportError"}:
                raise ValueError("persisted experiment outcome changed")
            if connection.execute("SELECT COUNT(*) FROM calls WHERE variant=?", (name,)).fetchone()[0] != generation + 1:
                raise ValueError("experiment has calls after unknown or failed send")
            return
        response = json.loads(row["response"])
        if row["outcome"] != "response_saved:" + digest(response):
            raise ValueError("persisted experiment outcome changed")
        validation = validation_result(sample["chunk"], response)
        if validation["accepted"] or not validation.get("diagnostic"):
            if connection.execute("SELECT COUNT(*) FROM calls WHERE variant=?", (name,)).fetchone()[0] != generation + 1:
                raise ValueError("experiment has calls after terminal validation")
            return
        previous = {"call_id": f"experiment:{row['ordinal']}", "stored_request": {
            "request": request, "recovery": recovery, "validation_diagnostic": validation["diagnostic"]}}


def _evaluate_variant(connection, sample, name, config, client, *,
                      maximum=MAX_CALLS, variant_names=VARIANT_NAMES, initial_diagnostic=None,
                      response_checksums=False):
    original = request_body(sample["chunk"], config, system_prompt=sample["prepared"]["snapshot"]["system_prompt"])
    previous = _initial_previous(sample, initial_diagnostic)
    # Two persisted reservations per variant, four across the experiment. A
    # crash after reservation leaves an unknown send; it is never sent again.
    for generation in range(min(2, 1 + config.max_chunk_regenerations)):
        request, recovery = recovery_request(original, previous, config)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _validate_schema(connection)
            _validate_calls(connection, variant_names, maximum)
            rows = connection.execute("SELECT * FROM calls WHERE variant=? ORDER BY ordinal", (name,)).fetchall()
            if len(rows) > generation:
                row = rows[generation]
                if row["response"] is None:
                    return
                response = json.loads(row["response"])
                ordinal = row["ordinal"]
                if json.loads(row["request"]) != request:
                    raise ValueError("persisted experiment request changed")
                reserved = False
            else:
                consumed = connection.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
                if consumed >= maximum:
                    return
                ordinal = consumed + 1
                connection.execute("INSERT INTO calls(ordinal,variant,request) VALUES (?,?,?)",
                                   (ordinal, name, canonical(request)))
                response = None
                reserved = True
        if reserved:
            started = time.monotonic()
            try:
                response = client.complete(request, config)
            except DeepSeekError as error:
                # Preserve safe type only, no server body or credential-bearing
                # exception text. Reservation remains consumed on every error.
                with connection:
                    connection.execute("UPDATE calls SET outcome=?,latency_ms=? WHERE ordinal=?",
                        (type(error).__name__, int((time.monotonic() - started) * 1000), ordinal))
                return
            with connection:
                connection.execute("UPDATE calls SET response=?,outcome=?,latency_ms=? WHERE ordinal=?",
                    (canonical(response), "response_saved:" + digest(response) if response_checksums else "response_saved",
                     int((time.monotonic() - started) * 1000), ordinal))
        validation = validation_result(sample["chunk"], response)
        if validation["accepted"] or not validation.get("diagnostic"):
            return
        previous = {"call_id": f"experiment:{ordinal}", "stored_request": {"request": request,
                    "recovery": recovery, "validation_diagnostic": validation["diagnostic"]}}
