"""Read-only ASR throughput accounting; historical gaps remain unknown."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from typing import Any

from bili_asr.storage.asr_performance import acquisition_counts, performance_attempts

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _identity(value: Any) -> str:
    if not value:
        return "unknown"
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _policy_identity(value: Any) -> str:
    policy = dict(_mapping(value))
    # Early runtime-strategy records placed model-lifetime counters and cold
    # call measurements inside the stable policy. Preserve the record itself,
    # but project these observations out of configuration grouping.
    policy.pop("runtime_strategies", None)
    return _identity(policy)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


class _Totals:
    def __init__(self):
        self.outcomes = Counter()
        self.jobs = {}
        self.audio = {}
        self.audio_attempts = Counter()
        self.conflicting_audio = set()
        self.latencies = []
        self.retry_attempts = 0
        self.attempt_wall_s = 0.0
        self.failure_wall_s = 0.0
        self.retry_wall_s = 0.0
        self.boundary_successes = 0
        self.unknown_success_audio = 0
        self.invalid_evidence = 0
        self.missing_evidence = 0
        self.single_pass = self.multi_pass = self.unknown_passes = 0
        self.prefetch = Counter()
        self.fallback = Counter()
        self.legacy_prefetch_passes = 0

    def add(self, row, start: int, end: int, evidence: dict, invalid: bool):
        outcome = row["outcome"]
        self.outcomes[outcome] += 1
        self.jobs[row["job_id"]] = row["job_status"]
        finish = row["finished_at"]
        overlap = max(0, min(end, finish if finish is not None else end) - max(start, row["started_at"]))
        self.attempt_wall_s += overlap
        self.failure_wall_s += overlap if outcome in {"failed", "cancelled"} else 0
        if row["is_retry"]:
            self.retry_attempts += 1
            self.retry_wall_s += overlap
        self.invalid_evidence += int(invalid)
        self.missing_evidence += int(row["evidence_json"] is None)
        if outcome != "succeeded":
            return
        if finish is None or row["started_at"] < start or finish > end:
            self.boundary_successes += 1
            return
        self.latencies.append(float(finish - row["started_at"]))
        audio = _mapping(evidence.get("audio"))
        duration_ms = _number(audio.get("duration_ms"))
        digest = audio.get("sha256")
        if duration_ms is not None and duration_ms > 0 and isinstance(digest, str) and _SHA256.fullmatch(digest):
            self.audio_attempts[digest] += 1
            held = self.audio.get(digest)
            if held is not None and held != duration_ms / 1000:
                self.conflicting_audio.add(digest)
            # A conflict invalidates the identity permanently for this window;
            # a third matching observation cannot restore its earlier credit.
            if digest not in self.conflicting_audio:
                self.audio[digest] = duration_ms / 1000
            else:
                self.audio.pop(digest, None)
        else:
            self.unknown_success_audio += 1
        passes = _mapping(evidence.get("diagnostics")).get("passes")
        if not isinstance(passes, list) or not passes:
            self.unknown_passes += 1
            return
        self.single_pass += int(len(passes) == 1)
        self.multi_pass += int(len(passes) > 1)
        for item in passes:
            prefetch = _mapping(_mapping(item).get("prefetch"))
            if not prefetch:
                continue
            for key in ("submitted", "consumed", "discarded", "input_wait_s"):
                value = _number(prefetch.get(key))
                if value is not None:
                    self.prefetch[key] += value
            reasons = prefetch.get("fallback_counts")
            if isinstance(reasons, dict):
                for key in ("input_budget", "prepared_input_budget", "prepared_input_size_unknown", "processor_not_cloneable"):
                    value = _number(reasons.get(key))
                    if value is not None:
                        self.fallback[key] += value
            else:
                # v1's last reason cannot reconstruct per-chunk counts.
                self.legacy_prefetch_passes += 1

    def report(self, window_s: int, conflicting_audio: set[str]) -> dict:
        terminal = sum(self.outcomes[k] for k in ("succeeded", "failed", "cancelled"))
        jobs = Counter(self.jobs.values())
        terminal_jobs = sum(jobs[k] for k in ("succeeded", "failed", "cancelled"))
        conflicts = conflicting_audio.intersection(self.audio_attempts)
        known_audio = {digest: duration for digest, duration in self.audio.items()
                       if digest not in conflicting_audio}
        audio_s = math.fsum(known_audio[digest] for digest in sorted(known_audio))
        conflicting_attempts = sum(self.audio_attempts[digest] for digest in conflicts)
        unknown_audio = self.unknown_success_audio + conflicting_attempts
        return {
            "attempt_outcomes": {k: self.outcomes[k] for k in ("running", "succeeded", "failed", "cancelled")},
            "terminal_attempt_success_rate": self.outcomes["succeeded"] / terminal if terminal else None,
            "unique_asr_job_status_at_snapshot": {k: jobs[k] for k in ("queued", "running", "succeeded", "failed", "cancelled")},
            "terminal_asr_job_success_rate_at_snapshot": jobs["succeeded"] / terminal_jobs if terminal_jobs else None,
            "retry_attempts": self.retry_attempts,
            "overlapping_attempt_wall_s": self.attempt_wall_s,
            "failure_cancelled_attempt_wall_s": self.failure_wall_s,
            "retry_attempt_wall_s": self.retry_wall_s,
            "unique_success_audio_s": audio_s,
            "known_unique_audio_count": len(known_audio),
            "audio_s_per_wall_s": audio_s / window_s if unknown_audio == 0 else None,
            "known_audio_s_per_wall_s_lower_bound": audio_s / window_s,
            "audio_credit_complete": unknown_audio == 0,
            "unknown_success_audio_attempts": unknown_audio,
            "conflicting_audio_identity_count": len(conflicts),
            "conflicting_success_audio_attempts": conflicting_attempts,
            "excluded_boundary_success_attempts": self.boundary_successes,
            "successful_attempt_latency_s": {"samples": len(self.latencies),
                "p50": _percentile(self.latencies, 0.5), "p95": _percentile(self.latencies, 0.95)},
            "pass_counts": {"single": self.single_pass, "multiple": self.multi_pass, "unknown": self.unknown_passes},
            "evidence_counts": {"missing": self.missing_evidence, "invalid_or_unsupported": self.invalid_evidence},
            "prefetch_observed_totals": dict(self.prefetch),
            "prefetch_fallback_counts": dict(self.fallback),
            "prefetch_legacy_passes_without_reason_counts": self.legacy_prefetch_passes,
        }


def asr_performance_report(connection: sqlite3.Connection, *, start: int, end: int,
                           max_attempts: int = 10_000) -> dict:
    if type(start) is not int or type(end) is not int or start < 0 or end <= start:
        raise ValueError("report window requires nonnegative epoch seconds with end > start")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 100_000:
        raise ValueError("max_attempts must be 1..100000")
    # One snapshot for attempts, evidence and acquisition counts. Never commit
    # or roll back a transaction belonging to the caller.
    owns_snapshot = not connection.in_transaction
    if owns_snapshot:
        connection.execute("BEGIN")
    try:
        return _read_report(connection, start, end, max_attempts)
    finally:
        if owns_snapshot:
            connection.rollback()


def _read_report(connection, start: int, end: int, max_attempts: int) -> dict:
    rows = performance_attempts(connection, start, end, max_attempts)
    totals = _Totals()
    groups = {}
    for row in rows:
        invalid = False
        try:
            evidence = json.loads(row["evidence_json"]) if row["evidence_json"] is not None else {}
            if not isinstance(evidence, dict) or type(evidence.get("schema_version")) is not int or evidence["schema_version"] != 1:
                evidence, invalid = {}, True
            # Non-finite legacy JSON cannot be a configuration identity.
            json.dumps(evidence, allow_nan=False)
        except (ValueError, TypeError):
            evidence, invalid = {}, True
        diagnostics = _mapping(evidence.get("diagnostics"))
        binding = _identity(_mapping(evidence.get("runtime_binding")))
        policy = _policy_identity(diagnostics.get("execution_policy"))
        profile = row["config_sha256"]
        profile = profile if isinstance(profile, str) and _SHA256.fullmatch(profile) else "unknown"
        key = (profile, binding, policy)
        group = groups.setdefault(key, _Totals())
        totals.add(row, start, end, evidence, invalid)
        group.add(row, start, end, evidence, invalid)
    return {
        "schema_version": 1,
        "window": {"start_epoch_s": start, "end_epoch_s": end, "wall_s": end - start,
                   "clock": "stored_utc_epoch", "timestamp_resolution_s": 1, "interval": "[start,end)"},
        "accounting": {
            "audio_credit": "successful_attempt_fully_inside_window_unique_audio_sha256",
            "audio_conflicts": "duration_conflicts_in_report_window_invalidate_identity_in_totals_and_all_groups",
            "group_audio_credit": "unique_within_group; identities_shared_by_groups_are_not_additive; conflicts_use_report_window_domain",
            "attempt_cost": "all_overlapping_attempts_clipped_to_window_sum_not_throughput_denominator",
            "scope": "workflow_asr_attempts; standalone_acquisition_runs_are_counts_only",
            "latency": "successful_fully_contained_attempts_seconds_resolution",
            "outcomes": "stored_outcomes_at_report_time_for_overlapping_attempts_not_as_of_window_end",
            "jobs": "unique_asr_jobs_with_overlapping_attempts; current_snapshot_status_not_as_of_window_end",
            "group_costs": "missing_binding_or_policy_stays_in_unknown_group_not_assigned_to_successful_configuration",
            "retry_order": "started_at_then_insertion_order_for_same_second",
            "prefetch_scope": "evidence_for_successful_fully_contained_attempts_only",
            "resource_peaks": "unknown", "published_throughput": "unknown",
            "effective_policy": "unknown_unless_explicitly_recorded; unknown_groups_are_not_configuration_rankings",
            "max_attempts": max_attempts,
        },
        "totals": totals.report(end - start, totals.conflicting_audio),
        "groups": [{"profile_sha256": key[0], "runtime_binding_sha256": key[1],
                    "effective_policy_sha256": key[2], **value.report(end - start, totals.conflicting_audio)}
                   for key, value in sorted(groups.items())],
        "acquisition_run_outcomes": acquisition_counts(connection, start, end),
    }
