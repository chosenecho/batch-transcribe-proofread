"""Decode an ordered positive-span alignment from timestamp classification scores.

This is a constrained maximum-score model decode, not a repair of predicted
timestamps. The caller must retain its input score evidence and compare the
result against the usual admission policy.
"""
from __future__ import annotations

import math
from typing import Any


def decode_timestamp_path(scores: Any, *, duration_seconds: float,
                          step_seconds: float) -> tuple[list[int], dict[str, Any]]:
    """Maximize the score over nonoverlapping start/end classification pairs.

    Each end must be at least one model class after its start. Adjacent words
    may touch. All positions must be actual timestamp classes inside the
    measured audio window. No timestamp sorting, clipping or interpolation is
    involved. Infeasible lattices fail closed.
    """
    import numpy as np

    values = np.asarray(scores, dtype=np.float64)
    if values.ndim != 2 or values.shape[0] % 2:
        raise ValueError("alignment scores require paired start/end rows")
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("alignment duration must be finite and positive")
    if not math.isfinite(step_seconds) or step_seconds <= 0:
        raise ValueError("timestamp class step must be finite and positive")
    if not values.shape[0]:
        return [], {"algorithm": "monotone-max-score/v1", "positions": 0}
    count = min(values.shape[1], math.floor(duration_seconds / step_seconds + 1e-9) + 1)
    if count < values.shape[0] // 2 + 1:
        raise ValueError("audio window has no positive-span alignment path")
    values = values[:, :count]
    if not np.isfinite(values).all():
        raise ValueError("alignment classification scores must be finite")
    previous = values[0].copy()
    parents = []
    for row in range(1, len(values)):
        # Accumulated prefix maxima give the best predecessor in O(classes).
        prefix = np.maximum.accumulate(previous)
        indices = np.arange(count)
        improve = np.r_[True, previous[1:] > prefix[:-1]]
        best_indices = np.maximum.accumulate(np.where(improve, indices, 0))
        delta = row % 2
        parent = np.full(count, -1, dtype=np.int32)
        current = np.full(count, -np.inf)
        parent[delta:] = best_indices[:count-delta]
        current[delta:] = values[row, delta:] + prefix[:count-delta]
        parents.append(parent)
        previous = current
    last = int(previous.argmax())
    if not np.isfinite(previous[last]):
        raise ValueError("no finite timestamp alignment path")
    path = [last]
    for parent in reversed(parents):
        last = int(parent[last])
        if last < 0:
            raise ValueError("invalid timestamp path predecessor")
        path.append(last)
    path.reverse()
    selected = values[np.arange(len(values)), path]
    penalties = values.max(axis=1) - selected
    raw = values.argmax(axis=1)
    return path, {"algorithm": "monotone-max-score/v1", "positions": len(path),
                  "timestamp_step_seconds": step_seconds, "valid_classes": count,
                  "changed_argmax_positions": int(np.count_nonzero(raw != path)),
                  "mean_logit_penalty": float(penalties.mean()),
                  "max_logit_penalty": float(penalties.max()),
                  "score": float(selected.sum())}
