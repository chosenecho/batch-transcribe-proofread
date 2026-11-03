"""Depth-one CPU preparation and its conservative admission evidence."""

from __future__ import annotations

import copy
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class DecodeInputs:
    """CPU processor output and its generation budget, before device transfer."""

    inputs: Any
    max_new_tokens: int
    feature_seconds: float


@dataclass(frozen=True)
class PreparationEstimate:
    waveform_bytes: int
    reserved_bytes: int
    strategy: str = "waveform_x64_v1"


def estimate_preparation(waveform_bytes: int) -> PreparationEstimate:
    if type(waveform_bytes) is not int or waveform_bytes < 0:
        raise ValueError("waveform_bytes must be a nonnegative integer")
    return PreparationEstimate(waveform_bytes, waveform_bytes * 64)


def prepared_input_bytes(inputs: Any) -> int | None:
    if isinstance(inputs, DecodeInputs):
        inputs = inputs.inputs
    total = 0
    for item in inputs.values():
        if callable(getattr(item, "numel", None)) and callable(getattr(item, "element_size", None)):
            size = int(item.numel()) * int(item.element_size())
        else:
            size = getattr(item, "nbytes", None)
        if size is None:
            return None
        total += int(size)
    return total


@dataclass(frozen=True)
class PreparedInput:
    inputs: Any
    observed_bytes: int | None
    started: float
    finished: float


class ProcessorReuse:
    """Hold one independent processor only inside a two-pass task scope."""

    def __init__(self):
        self.source = None
        self.processor = None

    def get(self, source: Any) -> Any:
        return self.processor if self.source is source else None

    def remember(self, source: Any, processor: Any) -> None:
        self.source, self.processor = source, processor

    def close(self) -> None:
        self.source = self.processor = None


class InputPrefetch:
    """The preparation thread returns data; only the caller updates evidence."""

    def __init__(self, processor: Any, prepare: Callable, *, enabled: bool, budget_bytes: int,
                 processor_reuse: ProcessorReuse | None = None):
        self.prepare = prepare
        self.budget = budget_bytes
        self.pool = None
        self.processor = None
        self._source_processor = processor if enabled else None
        self._processor_reuse = processor_reuse
        self._started = False
        self.pending = None
        self.entry = None
        self.peak_bytes = 0
        self.report = {
            "schema_version": 2, "enabled": enabled, "depth": 1,
            "budget_bytes": budget_bytes, "estimate_strategy": "waveform_x64_v1",
            "submitted": 0, "consumed": 0, "discarded": 0,
            "first_chunk_serial": True,
            "fallback": None, "fallback_counts": {}, "input_wait_s": 0.0,
            "processor_clone_attempts": 0, "processor_clone_s": 0.0,
            "processor_reused": False,
            "chunks": [],
            "memory_scope": "extra_prepared_input_not_total_rss_or_processor_temporaries",
        }

    def _start(self) -> None:
        if self._started:
            return
        self._started = True
        started = time.perf_counter()
        try:
            if self._processor_reuse is not None:
                self.processor = self._processor_reuse.get(self._source_processor)
            self.report["processor_reused"] = self.processor is not None
            if self.processor is None:
                self.report["processor_clone_attempts"] += 1
                self.processor = copy.deepcopy(self._source_processor)
            self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr-cpu-input")
            if self._processor_reuse is not None:
                self._processor_reuse.remember(self._source_processor, self.processor)
        except Exception:  # noqa: BLE001 - unsupported processors retain serial execution.
            self.processor = None
            self.report["fallback"] = "processor_not_cloneable"
        finally:
            self._source_processor = None
            if self.report["processor_clone_attempts"]:
                self.report["processor_clone_s"] = time.perf_counter() - started

    def _fallback(self, entry: dict, reason: str) -> None:
        entry["fallback"] = reason
        entry["status"] = "serial_fallback"
        self.report["fallback"] = reason
        counts = self.report["fallback_counts"]
        counts[reason] = counts.get(reason, 0) + 1

    def submit(self, index: int, audio: Any, *, minimum_samples: int = 0) -> None:
        waveform_bytes = max(len(audio), minimum_samples) * int(audio.dtype.itemsize)
        estimate = estimate_preparation(waveform_bytes)
        entry = {"chunk_index": index, "waveform_bytes": estimate.waveform_bytes,
                 "reserved_bytes": estimate.reserved_bytes, "observed_bytes": None,
                 "status": "disabled", "fallback": None, "wait_s": 0.0}
        self.report["chunks"].append(entry)
        if not self.report["enabled"]:
            return
        if estimate.reserved_bytes > self.budget:
            self._fallback(entry, "input_budget")
            return
        if self.pending is not None:
            raise RuntimeError("prefetch depth exceeded")
        # Clone only after a real candidate passes admission. A single chunk or
        # all-over-budget input should pay no processor or thread-pool setup.
        if self.pool is None:
            self._start()
        if self.pool is None:
            self._fallback(entry, "processor_not_cloneable")
            return
        entry["status"] = "submitted"
        self.entry = entry
        self.pending = self.pool.submit(self._prepare, audio, minimum_samples)
        self.report["submitted"] += 1

    def _prepare(self, audio: Any, minimum_samples: int) -> PreparedInput:
        started = time.perf_counter()
        if len(audio) < minimum_samples:
            import numpy as np
            audio = np.pad(audio, (0, minimum_samples - len(audio)))
        inputs = self.prepare(self.processor, audio)
        return PreparedInput(inputs, prepared_input_bytes(inputs), started, time.perf_counter())

    def _observe(self, result: PreparedInput) -> None:
        self.entry["observed_bytes"] = result.observed_bytes
        if result.observed_bytes is not None:
            self.peak_bytes = max(self.peak_bytes, result.observed_bytes)

    def consume(self) -> tuple[Any, PreparedInput | None]:
        if self.pending is None:
            return None, None
        started = time.perf_counter()
        try:
            result = self.pending.result()
        except BaseException:
            self.entry["status"] = "failed"
            raise
        finally:
            self.pending = None
            waited = time.perf_counter() - started
            self.entry["wait_s"] = waited
            self.report["input_wait_s"] += waited
        self._observe(result)
        metadata = PreparedInput(None, result.observed_bytes, result.started, result.finished)
        if result.observed_bytes is None or result.observed_bytes > self.budget:
            reason = "prepared_input_size_unknown" if result.observed_bytes is None else "prepared_input_budget"
            self._fallback(self.entry, reason)
            self.report["discarded"] += 1
            return None, metadata
        self.entry["status"] = "consumed"
        self.report["consumed"] += 1
        return result.inputs, metadata

    def close(self) -> None:
        self._source_processor = None
        self._processor_reuse = None
        if self.pool is None:
            return
        self.pool.shutdown(wait=True, cancel_futures=True)
        if self.pending is not None:
            self.report["discarded"] += 1
            if self.pending.cancelled():
                self.entry["status"] = "cancelled"
            else:
                # Cleanup must not replace the active decoding exception.
                try:
                    self._observe(self.pending.result())
                    self.entry["status"] = "discarded"
                except BaseException:  # noqa: BLE001
                    self.entry["status"] = "failed"
            self.pending = None
        self.pool = None
        self.processor = None
