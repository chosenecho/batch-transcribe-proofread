"""Conservative, opt-in evidence for nearly silent audio boundaries, not VAD."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

METHOD = "quiet-edges/v1"
PEAK_DBFS = -80


def measure_edges(samples: Any, sample_rate: int) -> dict[str, Any]:
    """Find boundary samples above -80 dBFS without trimming the waveform.

    Scan bounded one-second blocks. Ordinary quiet speech,
    music and interior gaps are not excluded. An entirely quiet file is not
    evidence of a successfully transcribed or confirmed-silent source.
    """
    import numpy as np

    values = np.asarray(samples)
    base = {"method": METHOD, "peak_dbfs": PEAK_DBFS, "sample_rate": sample_rate,
            "sample_count": int(values.size), "status": "unavailable"}
    if (values.ndim != 1 or not values.size or type(sample_rate) is not int
            or sample_rate < 1):
        return base
    threshold = 10 ** (PEAK_DBFS / 20)

    first = last = None
    for offset in range(0, len(values), sample_rate):
        block = values[offset:offset + sample_rate]
        if not np.isfinite(block).all():
            return base
        indices = np.flatnonzero(np.abs(block) > threshold)
        if len(indices):
            if first is None:
                first = offset + int(indices[0])
            last = offset + int(indices[-1]) + 1
    if first is None or last is None:
        return {**base, "status": "no_signal_above_threshold"}
    return {**base, "status": "measured", "first_signal_sample": first,
            "last_signal_sample_exclusive": last}


def covered_edges(evidence: Any, *, decoded_s: float, cue_start: float,
                  cue_end: float, minimum: float) -> dict[str, Any] | None:
    """Validate an exact decoded-window witness and recompute its span ratio.

    No caller-supplied ratio or success flag is trusted. The original full
    duration/coverage attestation and cue timestamps are never rewritten.
    """
    if not isinstance(evidence, Mapping):
        return None
    required = {"method", "peak_dbfs", "sample_rate", "sample_count", "status",
                "first_signal_sample", "last_signal_sample_exclusive"}
    if (set(evidence) != required or evidence["method"] != METHOD
            or evidence["status"] != "measured"
            or type(evidence["peak_dbfs"]) is not int or evidence["peak_dbfs"] != PEAK_DBFS):
        return None
    names = ("sample_rate", "sample_count", "first_signal_sample", "last_signal_sample_exclusive")
    if any(type(evidence[name]) is not int for name in names):
        return None
    rate, count, first, last = (evidence[name] for name in names)
    if rate < 1 or not 0 <= first < last <= count:
        return None
    numbers = (decoded_s, cue_start, cue_end, minimum)
    try:
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in numbers):
            return None
        duration = count / rate
    except OverflowError:
        return None
    if (abs(duration - decoded_s) > 1 / rate or not 0 < minimum <= 1
            or not 0 <= cue_start < cue_end <= decoded_s + .05):
        return None
    active_start, active_end = first / rate, last / rate
    if not active_start < active_end:
        return None
    produced = max(0.0, min(cue_end, active_end) - max(cue_start, active_start))
    ratio = produced / (active_end - active_start)
    return {"method": METHOD, "head_quiet_s": active_start,
            "tail_quiet_s": decoded_s - active_end, "coverage": ratio,
            "coverage_short": ratio < minimum}


def diagnostic_edge_coverage(diagnostics: Any, segments: Sequence[Mapping[str, Any]], *,
                             decoded_s: float, minimum: float) -> dict[str, Any] | None:
    """Require the producing run's explicit policy and its final-pass witness."""
    if not isinstance(diagnostics, Mapping) or not segments:
        return None
    execution = diagnostics.get("execution_policy")
    if not isinstance(execution, Mapping):
        return None
    policy = execution.get("output_policy")
    if (not isinstance(policy, Mapping)
            or set(policy) != {"schema_version", "coverage_policy", "alignment_decoder"}
            or type(policy.get("schema_version")) is not int or policy["schema_version"] != 1
            or policy["coverage_policy"] != "quiet-edges"
            or policy["alignment_decoder"] not in ("default", "processor", "monotone-max-score", "monotone-max-score/v1")):
        return None
    passes = diagnostics.get("passes")
    final = passes[-1] if isinstance(passes, list) and passes else None
    if not isinstance(final, Mapping):
        return None
    final_execution = final.get("execution_policy")
    if not isinstance(final_execution, Mapping) or final_execution.get("output_policy") != policy:
        return None
    try:
        return covered_edges(final.get("quiet_edges"), decoded_s=decoded_s,
                             cue_start=min(s["start"] for s in segments),
                             cue_end=max(s["end"] for s in segments), minimum=minimum)
    except (KeyError, TypeError, ValueError):
        return None
