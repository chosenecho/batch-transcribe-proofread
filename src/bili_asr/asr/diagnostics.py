"""Measured ASR evidence. These signals do not attest semantic accuracy."""

from __future__ import annotations

import math
import platform
import sys
from copy import deepcopy
from importlib.metadata import PackageNotFoundError, version
from typing import Any


def runtime_environment() -> dict[str, Any]:
    packages = {}
    for name in ("torch", "transformers", "accelerate", "numpy", "soundfile", "soxr"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    torch = sys.modules.get("torch")
    runtime = getattr(torch, "version", None)
    return {
        "python": platform.python_version(),
        "platform": platform.system(),
        "packages": packages,
        "cuda": getattr(runtime, "cuda", None),
        "hip": getattr(runtime, "hip", None),
    }


def generation_evidence(tokens: Any, model: Any, budget: int) -> dict[str, Any]:
    count = int(tokens.shape[1])
    eos = getattr(getattr(model, "generation_config", None), "eos_token_id", None)
    if eos is None:
        eos = getattr(getattr(model, "config", None), "eos_token_id", None)
    ended_with_eos = None
    if count and eos is not None:
        try:
            last = int(tokens[0, -1].item())
            ended_with_eos = last in (eos if isinstance(eos, (list, tuple)) else [eos])
        except (AttributeError, TypeError, IndexError):
            # Tensor doubles/backends may not expose scalar indexing. Unknown
            # stays unknown rather than inventing an EOS observation.
            pass
    return {
        "max_new_tokens": budget,
        "generated_tokens": count,
        "ended_with_eos": ended_with_eos,
        "token_limit_reached": count >= budget,
    }


def alignment_evidence(units: list[dict[str, Any]], duration_s: float) -> dict[str, Any]:
    """Union and gaps of raw aligner units, before subtitle cue merging."""
    intervals = []
    invalid = 0
    for unit in units:
        start, end = float(unit["start_time"]), float(unit["end_time"])
        if (not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or end < start or end > duration_s + 0.05):
            invalid += 1
            continue
        start, end = max(0.0, start), min(duration_s, end)
        if end > start:
            intervals.append((start, end))
    cursor = covered = longest_gap = 0.0
    for start, end in sorted(intervals):
        longest_gap = max(longest_gap, start - cursor)
        covered += max(0.0, end - max(cursor, start))
        cursor = max(cursor, end)
    longest_gap = max(longest_gap, duration_s - cursor)
    return {
        "aligned_unit_union_s": covered,
        "longest_unaligned_gap_s": longest_gap,
        "invalid_alignment_units": invalid,
        "timestamp_tolerance_s": 0.05,
    }


def assemble_diagnostics(passes: list[dict[str, Any]]) -> dict[str, Any]:
    """Final-pass quality; timings cover every actual pass, including load."""
    passes = deepcopy(passes)
    final = passes[-1] if passes else {}
    flags = sorted({flag for chunk in final.get("chunks", []) for flag in chunk.get("flags", [])})
    if final.get("span_coverage_short"):
        flags.append("span-coverage-short")
    complete = bool(final.get("completed"))
    if not complete or not final.get("decoded_s"):
        status = "not-evaluable"
    else:
        status = "needs-review" if flags else "no-known-risk"
    timings: dict[str, float] = {}
    for report in passes:
        for key, value in report.get("timings_s", {}).items():
            timings[key] = timings.get(key, 0.0) + value
    return {
        "schema_version": 1,
        "execution_policy": final.get("execution_policy"),
        "passes": passes,
        "quality": {"status": status, "flags": flags, "human_reviewed": False},
        "timings_s": timings,
    }
