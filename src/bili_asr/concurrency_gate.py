"""Pure evidence gate for any separately approved future concurrency work."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_REPORT_SCHEMA_VERSION = "concurrency-gate-report-v1"
_EVIDENCE_SCHEMA_VERSION = "concurrency-gate-evidence-v1"
_WRITE_TARGETS = frozenset({"manifest", "sidecars", "attempts", "index", "artifacts"})
_EVIDENCE_FIELDS = frozenset({
    "schema_version", "campaign_snapshot_id", "campaign_item_count",
    "campaign_denominator", "reconciliation_denominator", "age_seconds",
    "throughput_items_per_hour", "api_risk_rate", "peak_disk_bytes",
    "reclaim_rate", "crash_restart_passed", "duplicate_work_count",
    "max_owner_count", "checkpoint_reconciled", "risk_taxonomy_unchanged",
    "write_isolation",
})
_THRESHOLD_FIELDS = frozenset({
    "min_campaign_item_count", "max_evidence_age_seconds",
    "min_throughput_items_per_hour", "max_api_risk_rate",
    "max_peak_disk_bytes", "min_reclaim_rate", "max_duplicate_work_count",
    "max_owner_count",
})
_SENSITIVE_KEY_TOKENS = frozenset({
    "authorization", "bearer", "cookie", "credential", "password", "secret",
    "sessdata", "token",
})
_SENSITIVE_KEY_PHRASES = frozenset({
    "raw_exception", "signed_url", "traceback", "x_amz_signature",
})
_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"(?i)\b(?:authorization|bearer|token)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(?:cookie|credential|password|secret|sessdata)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bsigned[\s_-]+url\s*[:=]"),
    re.compile(r"(?i)(?:[?&]|\b)(?:x-amz-signature|sig|signature)\s*="),
    re.compile(r"(?i)\braw[\s_-]+exception\s*[:=]"),
    re.compile(r"(?i)\btraceback(?:\s*\(|\s*[:=])"),
)
_MAX_DEPTH = 64
_MAX_NODES = 10_000
_MAX_CONTAINER_ITEMS = 1_000
_MAX_STRING_LENGTH = 4_096


@dataclass(frozen=True)
class GateResult:
    reason_codes: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.reason_codes

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": "go" if self.ok else "no-go",
            "ok": self.ok,
            "operating_mode": "sequential-no-daemon",
            "reason_codes": list(self.reason_codes),
            "schema_version": _REPORT_SCHEMA_VERSION,
        }


class _UnsafeInput(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _copy_mapping(source: Mapping[str, object]) -> dict[str, object]:
    try:
        return dict(source)
    except Exception:
        raise _UnsafeInput("access_error") from None


def _normalize_key(key: str) -> tuple[set[str], str]:
    words = re.findall(r"[a-z0-9]+", key.casefold())
    return set(words), "_".join(words)


def _key_is_sensitive(key: str) -> bool:
    tokens, phrase = _normalize_key(key)
    return bool(tokens & _SENSITIVE_KEY_TOKENS) or any(
        marker in phrase for marker in _SENSITIVE_KEY_PHRASES
    )


def _value_is_sensitive(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SENSITIVE_VALUE_PATTERNS)


def _container_items(value: object) -> list[tuple[object, object]]:
    try:
        if isinstance(value, Mapping):
            items = list(value.items())
        else:
            items = list(enumerate(value))
    except Exception:
        raise _UnsafeInput("access_error") from None
    if len(items) > _MAX_CONTAINER_ITEMS:
        raise _UnsafeInput("complexity_limit")
    return items


def _contains_sensitive_marker(value: object) -> bool:
    """Bounded iterative scan with one shared budget for every popped node.

    Seen identities stay recorded for the entire scan. Cycles and repeated shared
    references therefore conservatively fail closed with one stable reason.
    """
    stack: list[tuple[object, int]] = [(value, 0)]
    seen: set[int] = set()
    popped_nodes = 0
    while stack:
        current, depth = stack.pop()
        popped_nodes += 1
        if popped_nodes > _MAX_NODES or depth > _MAX_DEPTH:
            raise _UnsafeInput("complexity_limit")
        if isinstance(current, str):
            if len(current) > _MAX_STRING_LENGTH:
                raise _UnsafeInput("complexity_limit")
            if _value_is_sensitive(current):
                return True
            continue
        is_mapping = isinstance(current, Mapping)
        is_sequence = isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        )
        if not is_mapping and not is_sequence:
            continue
        identity = id(current)
        if identity in seen:
            raise _UnsafeInput("complexity_or_cycle")
        seen.add(identity)
        for key, nested in _container_items(current):
            if isinstance(key, str):
                if len(key) > _MAX_STRING_LENGTH:
                    raise _UnsafeInput("complexity_limit")
                if _key_is_sensitive(key):
                    return True
            stack.append((nested, depth + 1))
    return False


def _required_value(source: dict[str, object], field: str, prefix: str, reasons: set[str]) -> object | None:
    if field not in source:
        reasons.add(f"{prefix}_missing_{field}")
        return None
    return source[field]


def _validate_nonnegative_integer(source: dict[str, object], field: str, prefix: str, reasons: set[str]) -> int | None:
    value = _required_value(source, field, prefix, reasons)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        reasons.add(f"{prefix}_malformed_{field}")
        return None
    return value


def _validate_positive_integer(source: dict[str, object], field: str, prefix: str, reasons: set[str]) -> int | None:
    value = _validate_nonnegative_integer(source, field, prefix, reasons)
    if value == 0:
        reasons.add(f"{prefix}_zero_{field}")
        return None
    return value


def _validate_nonnegative_number(source: dict[str, object], field: str, prefix: str, reasons: set[str]) -> float | int | None:
    value = _required_value(source, field, prefix, reasons)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        reasons.add(f"{prefix}_malformed_{field}")
        return None
    return value


def _validate_positive_number(source: dict[str, object], field: str, prefix: str, reasons: set[str]) -> float | int | None:
    value = _validate_nonnegative_number(source, field, prefix, reasons)
    if value == 0:
        reasons.add(f"{prefix}_zero_{field}")
        return None
    return value


def _validate_unit_rate(source: dict[str, object], field: str, prefix: str, reasons: set[str]) -> float | int | None:
    value = _validate_nonnegative_number(source, field, prefix, reasons)
    if value is not None and value > 1:
        reasons.add(f"{prefix}_malformed_{field}")
        return None
    return value


def _validate_required_boolean(evidence: dict[str, object], field: str, failure_reason: str, reasons: set[str]) -> None:
    value = _required_value(evidence, field, "evidence", reasons)
    if value is None:
        return
    if not isinstance(value, bool):
        reasons.add(f"evidence_malformed_{field}")
    elif not value:
        reasons.add(failure_reason)


def _safe_source_copy(source: object, prefix: str, reasons: set[str]) -> dict[str, object] | None:
    if not isinstance(source, Mapping):
        reasons.add(f"{prefix}_malformed")
        return None
    try:
        copied = _copy_mapping(source)
        if _contains_sensitive_marker(copied):
            reasons.add(f"sensitive_{prefix}_marker")
        return copied
    except _UnsafeInput as exc:
        reasons.add(f"{prefix}_{exc.reason}")
        return None


class ConcurrencyGate:
    @staticmethod
    def evaluate(evidence: Mapping[str, object], thresholds: Mapping[str, object]) -> GateResult:
        reasons: set[str] = set()
        evidence_values = _safe_source_copy(evidence, "evidence", reasons)
        threshold_values = _safe_source_copy(thresholds, "threshold", reasons)
        if evidence_values is None or threshold_values is None:
            return GateResult(tuple(sorted(reasons)))

        if set(evidence_values) - _EVIDENCE_FIELDS:
            reasons.add("evidence_unknown_fields")
        if set(threshold_values) - _THRESHOLD_FIELDS:
            reasons.add("threshold_unknown_fields")
        if evidence_values.get("schema_version") != _EVIDENCE_SCHEMA_VERSION:
            reasons.add("evidence_schema_invalid")

        if "campaign_snapshot_id" not in evidence_values:
            reasons.add("evidence_missing_campaign_snapshot_id")
        else:
            snapshot_id = evidence_values["campaign_snapshot_id"]
            if isinstance(snapshot_id, str) and not snapshot_id.strip():
                reasons.add("evidence_empty_campaign_snapshot_id")
            elif not isinstance(snapshot_id, str) or len(snapshot_id) > 256 or not snapshot_id.isascii():
                reasons.add("evidence_malformed_campaign_snapshot_id")

        campaign_item_count = _validate_positive_integer(evidence_values, "campaign_item_count", "evidence", reasons)
        campaign_denominator = _validate_positive_integer(evidence_values, "campaign_denominator", "evidence", reasons)
        reconciliation_denominator = _validate_positive_integer(evidence_values, "reconciliation_denominator", "evidence", reasons)
        age_seconds = _validate_nonnegative_integer(evidence_values, "age_seconds", "evidence", reasons)
        throughput = _validate_nonnegative_number(evidence_values, "throughput_items_per_hour", "evidence", reasons)
        api_risk_rate = _validate_unit_rate(evidence_values, "api_risk_rate", "evidence", reasons)
        peak_disk_bytes = _validate_nonnegative_integer(evidence_values, "peak_disk_bytes", "evidence", reasons)
        reclaim_rate = _validate_unit_rate(evidence_values, "reclaim_rate", "evidence", reasons)
        duplicate_work_count = _validate_nonnegative_integer(evidence_values, "duplicate_work_count", "evidence", reasons)
        observed_max_owner_count = _validate_positive_integer(evidence_values, "max_owner_count", "evidence", reasons)

        min_campaign_item_count = _validate_positive_integer(threshold_values, "min_campaign_item_count", "threshold", reasons)
        max_evidence_age_seconds = _validate_nonnegative_integer(threshold_values, "max_evidence_age_seconds", "threshold", reasons)
        min_throughput = _validate_positive_number(threshold_values, "min_throughput_items_per_hour", "threshold", reasons)
        max_api_risk_rate = _validate_unit_rate(threshold_values, "max_api_risk_rate", "threshold", reasons)
        max_peak_disk_bytes = _validate_nonnegative_integer(threshold_values, "max_peak_disk_bytes", "threshold", reasons)
        min_reclaim_rate = _validate_unit_rate(threshold_values, "min_reclaim_rate", "threshold", reasons)
        max_duplicate_work_count = _validate_nonnegative_integer(threshold_values, "max_duplicate_work_count", "threshold", reasons)
        allowed_max_owner_count = _validate_positive_integer(threshold_values, "max_owner_count", "threshold", reasons)

        _validate_required_boolean(evidence_values, "crash_restart_passed", "crash_restart_failed", reasons)
        _validate_required_boolean(evidence_values, "checkpoint_reconciled", "checkpoint_unreconciled", reasons)
        _validate_required_boolean(evidence_values, "risk_taxonomy_unchanged", "risk_taxonomy_changed", reasons)

        if campaign_item_count is not None and campaign_denominator is not None and campaign_item_count > campaign_denominator:
            reasons.add("campaign_item_count_exceeds_denominator")
        if campaign_denominator is not None and reconciliation_denominator is not None and reconciliation_denominator != campaign_denominator:
            reasons.add("reconciliation_denominator_mismatch")
        if campaign_item_count is not None and min_campaign_item_count is not None and campaign_item_count < min_campaign_item_count:
            reasons.add("campaign_item_count_below_minimum")

        isolation_value = evidence_values.get("write_isolation")
        if not isinstance(isolation_value, Mapping):
            reasons.add("evidence_malformed_write_isolation")
            isolation: dict[str, object] = {}
        else:
            try:
                isolation = _copy_mapping(isolation_value)
            except _UnsafeInput:
                reasons.add("evidence_access_error")
                return GateResult(tuple(sorted(reasons)))
        if set(isolation) - _WRITE_TARGETS:
            reasons.add("write_isolation_unknown_fields")
        for target in sorted(_WRITE_TARGETS):
            if isolation.get(target) is not True:
                reasons.add(f"write_isolation_missing_{target}")

        comparisons = (
            (age_seconds, max_evidence_age_seconds, "evidence_stale", lambda left, right: left > right),
            (throughput, min_throughput, "throughput_breach", lambda left, right: left < right),
            (api_risk_rate, max_api_risk_rate, "api_risk_breach", lambda left, right: left > right),
            (peak_disk_bytes, max_peak_disk_bytes, "disk_peak_breach", lambda left, right: left > right),
            (reclaim_rate, min_reclaim_rate, "reclaim_breach", lambda left, right: left < right),
            (duplicate_work_count, max_duplicate_work_count, "duplicate_work_detected", lambda left, right: left > right),
            (observed_max_owner_count, allowed_max_owner_count, "multiple_owners_detected", lambda left, right: left > right),
        )
        for observed, threshold, reason, breached in comparisons:
            if observed is not None and threshold is not None and breached(observed, threshold):
                reasons.add(reason)
        return GateResult(tuple(sorted(reasons)))
