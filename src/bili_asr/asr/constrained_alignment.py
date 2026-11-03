"""Maximum-score timestamp decoding with ordered, positive word spans."""

from __future__ import annotations

import hashlib
import math
from typing import Any

MAX_LATTICE_CELLS = 16_000_000


def decode_timestamp_path(scores: Any, *, duration_seconds: float,
                          step_seconds: float) -> tuple[list[int], dict[str, Any]]:
    """Choose actual model classes, never edit predicted timestamps.

    Starts/ends are nondecreasing and each word has a strictly positive
    duration. Only classes inside the original, unpadded audio are eligible.
    Memory is bounded; infeasible or nonfinite lattices fail closed.
    """
    import numpy as np

    source = np.asarray(scores)
    if source.ndim != 2 or source.shape[0] % 2 or not source.shape[0]:
        raise ValueError("alignment scores require nonempty paired start/end rows")
    for value in (duration_seconds, step_seconds):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("alignment duration and timestamp step must be finite and positive")
    count = min(source.shape[1], math.floor(duration_seconds / step_seconds + 1e-9) + 1)
    if count < source.shape[0] // 2 + 1:
        raise ValueError("audio window has no positive-span alignment path")
    if source.shape[0] * count > MAX_LATTICE_CELLS:
        raise ValueError("alignment lattice exceeds the bounded decode budget")
    values = np.asarray(source[:, :count], dtype=np.float64)
    if not np.isfinite(source).all():
        raise ValueError("alignment classification scores must be finite")
    previous = values[0].copy()
    parents = []
    indices = np.arange(count)
    for row in range(1, len(values)):
        prefix = np.maximum.accumulate(previous)
        improve = np.r_[True, previous[1:] > prefix[:-1]]
        best_indices = np.maximum.accumulate(np.where(improve, indices, 0))
        delta = row % 2
        parent = np.full(count, -1, dtype=np.int32)
        current = np.full(count, -np.inf)
        parent[delta:] = best_indices[:count - delta]
        current[delta:] = values[row, delta:] + prefix[:count - delta]
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
    raw = source.argmax(axis=1)
    penalties = source.max(axis=1) - selected
    digest = hashlib.sha256()
    digest.update(f"{source.dtype}:{source.shape}".encode("ascii"))
    digest.update(np.ascontiguousarray(source).tobytes())
    return path, {"algorithm": "monotone-max-score/v1", "positions": len(path),
                  "timestamp_step_seconds": step_seconds, "valid_classes": count,
                  "score_shape": list(source.shape), "scores_sha256": digest.hexdigest(),
                  "changed_argmax_positions": int(np.count_nonzero(raw != path)),
                  "raw_zero_duration_units": int(np.count_nonzero(raw[::2] == raw[1::2])),
                  "raw_backward_boundaries": int(np.count_nonzero(np.diff(raw) < 0)),
                  "mean_logit_penalty": float(penalties.mean()),
                  "max_logit_penalty": float(penalties.max()), "score": float(selected.sum())}


def decode_alignment(processor: Any, *, logits: Any, input_ids: Any,
                     word_lists: list[list[str]], timestamp_token_id: int,
                     durations: list[float], policy: str) -> tuple[list[list[dict]], list[dict]]:
    """Use one decoding boundary for native serial and batched inference."""
    original = processor.decode_forced_alignment(logits=logits, input_ids=input_ids,
        word_lists=word_lists, timestamp_token_id=timestamp_token_id)
    if len(original) != len(word_lists) or len(durations) != len(word_lists):
        raise ValueError("alignment batch result count mismatch")
    if policy in ("default", "processor"):
        return original, [{} for _ in original]
    if policy not in ("monotone-max-score", "monotone-max-score/v1"):
        raise ValueError("unsupported alignment decoder")
    step = processor.timestamp_segment_time / 1000
    results, evidence = [], []
    for index, words in enumerate(word_lists):
        if len(original[index]) != len(words):
            raise ValueError("alignment processor changed word count")
        scores = logits[index][input_ids[index] == timestamp_token_id]
        if hasattr(scores, "detach"):
            scores = scores.detach().float().cpu().numpy()
        if scores.shape[0] != 2 * len(words):
            raise ValueError("alignment timestamp/word identity mismatch")
        path, report = decode_timestamp_path(scores, duration_seconds=durations[index], step_seconds=step)
        report["original_zero_duration_units"] = sum(
            unit["start_time"] == unit["end_time"] for unit in original[index])
        # Keep the previous prediction, bounded like the existing private
        # threading witness. The score digest/objective covers the full lattice.
        witness, remaining = [], 8192
        for unit in original[index][:512]:
            text = str(unit["text"])[:min(128, remaining)]
            witness.append({"text": text, "start_time": unit["start_time"], "end_time": unit["end_time"]})
            remaining -= len(text)
            if not remaining:
                break
        report["private_original_units"] = witness
        report["original_units_truncated"] = (len(witness) != len(original[index])
            or any(item["text"] != str(unit["text"]) for item, unit in zip(witness, original[index])))
        evidence.append(report)
        results.append([{"text": word, "start_time": path[2 * i] * step,
                         "end_time": path[2 * i + 1] * step} for i, word in enumerate(words)])
    return results, evidence
