"""Versioned structural ASR admission, independent of semantic accuracy.

The original cue order and values are evidence. This module only measures
them: overlap is valid, and no cue is sorted, dropped or clamped.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from bili_asr.storage.models import MAX_TIMELINE_MS

POLICY_VERSION = "asr-admission/v1"
TIMESTAMP_TOLERANCE_MS = 50


def _number(value: Any) -> bool:
    return type(value) is int or type(value) is float and math.isfinite(value)


def assess_output(segments: Sequence[Mapping[str, Any]], *, duration_ms: int | None = None,
                  coverage: Mapping[str, Any] | None = None,
                  diagnostics: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate raw second timestamps and measured coverage without mutation.

    Coverage is a measured output span, not a union of speech intervals. Quiet
    gaps and ordinary overlaps cannot by themselves prove a defective ASR.
    Missing measurements remain unknown; they do not establish silence.
    """
    issues: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    def issue(code: str, ordinal: int | None = None) -> None:
        counts[code] = counts.get(code, 0) + 1
        if len(issues) < 32:
            item: dict[str, Any] = {"code": code}
            if ordinal is not None:
                item["segment_ordinal"] = ordinal
            issues.append(item)

    coverage_valid = False
    decoded_ms = None
    coverage_short = False
    if coverage is not None:
        names = ("decoded_s", "produced_s", "coverage", "coverage_min")
        if (not all(_number(coverage.get(name)) and abs(coverage[name]) <= MAX_TIMELINE_MS for name in names)
                or type(coverage.get("coverage_short")) is not bool):
            issue("asr_coverage_invalid")
        else:
            decoded, produced, ratio, minimum = (float(coverage[name]) for name in names)
            short = coverage["coverage_short"]
            if (decoded <= 0 or produced < 0 or produced > decoded or not 0 <= ratio <= 1
                    or not 0 < minimum <= 1 or abs(ratio - produced / decoded) > 1e-6
                    or short != (ratio < minimum)):
                issue("asr_coverage_invalid")
            else:
                coverage_valid = True
                decoded_ms = round(decoded * 1000)
                coverage_short = short
                if short:
                    issue("asr_coverage_short")
    bound = decoded_ms if decoded_ms is not None else duration_ms
    if type(bound) is not int or bound <= 0:
        bound = None
    previous_start = None
    representable = bool(segments)
    starts, ends = [], []
    if not segments:
        issue("asr_empty_output")
    for ordinal, segment in enumerate(segments):
        if not isinstance(segment, Mapping) or any(type(segment.get(name)) not in (int, float) for name in ("start", "end")):
            issue("asr_timestamp_invalid", ordinal)
            representable = False
            continue
        if not _number(segment["start"]) or not _number(segment["end"]):
            issue("asr_timestamp_nonfinite", ordinal)
            representable = False
            continue
        if any(abs(segment[name]) > MAX_TIMELINE_MS / 1000 for name in ("start", "end")):
            issue("asr_timestamp_out_of_range", ordinal)
            representable = False
            continue
        start, end = float(segment["start"]), float(segment["end"])
        start_ms, end_ms = round(start * 1000), round(end * 1000)
        if start < 0:
            issue("asr_timestamp_negative", ordinal)
            representable = False
        if end <= start or end_ms <= start_ms:
            issue("asr_timestamp_nonpositive", ordinal)
            representable = False
        if previous_start is not None and start_ms < previous_start:
            issue("asr_timestamp_backward", ordinal)
        if bound is not None and end * 1000 > bound + TIMESTAMP_TOLERANCE_MS:
            issue("asr_timestamp_out_of_bounds", ordinal)
        text = segment.get("text")
        if not isinstance(text, str) or not text.strip():
            issue("asr_text_empty", ordinal)
            representable = False
        elif "\x00" in text or any(not line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").strip().split("\n")):
            issue("asr_text_unrepresentable", ordinal)
        previous_start = start_ms
        starts.append(start)
        ends.append(end)
    if coverage_valid and starts:
        # Compare the existing span claim, including its documented decoded
        # window cap. No timestamp is changed to make that claim pass.
        measured_span = min(max(ends) - min(starts), float(coverage["decoded_s"]))
        if abs(measured_span - float(coverage["produced_s"])) > TIMESTAMP_TOLERANCE_MS / 1000:
            issue("asr_coverage_inconsistent")
    review_required = False
    if isinstance(diagnostics, Mapping):
        quality = diagnostics.get("quality")
        if isinstance(quality, Mapping) and quality.get("status") == "needs-review":
            review_required = True
            issue("asr_diagnostics_review_required")
        passes = diagnostics.get("passes")
        final = passes[-1] if isinstance(passes, list) and passes else None
        if isinstance(final, Mapping) and isinstance(final.get("chunks"), list):
            for chunk in final["chunks"]:
                alignment = chunk.get("alignment") if isinstance(chunk, Mapping) else None
                if isinstance(alignment, Mapping) and any(
                    type(alignment.get(name)) is int and alignment[name] > 0
                    for name in ("unmatched_text_characters", "extra_alignment_units", "backward_alignment_units",
                                 "zero_duration_alignment_units")
                ):
                    issue("asr_alignment_unresolved")
    hard = set(counts) - {"asr_coverage_short", "asr_diagnostics_review_required"}
    status = ("unusable" if hard else "needs-review" if coverage_short or review_required
              else "unknown" if bound is None or not coverage_valid else "usable")
    return {"policy_version": POLICY_VERSION, "status": status,
            "inference_completed": True, "confirmed_silence": None,
            "representable": representable, "segment_count": len(segments),
            "timeline": "invalid" if any(code.startswith("asr_timestamp_") for code in counts) else "valid",
            "coverage": "invalid" if "asr_coverage_invalid" in counts or "asr_coverage_inconsistent" in counts
            else "short" if coverage_short else "measured" if coverage_valid else "unknown",
            "duration_ms": bound, "timestamp_tolerance_ms": TIMESTAMP_TOLERANCE_MS,
            "issues": issues, "issue_counts": counts,
            "issues_truncated": sum(counts.values()) > len(issues)}


def evidence_value(value: Any) -> Any:
    """Keep nonfinite raw values in strict JSON with explicit numeric tags."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"nonfinite_number": "nan" if math.isnan(value) else "+inf" if value > 0 else "-inf"}
    if isinstance(value, Mapping):
        return {str(key): evidence_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [evidence_value(item) for item in value]
    return value
