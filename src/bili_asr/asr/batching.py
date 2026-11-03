"""Bounded native HF batches within one task and pass; never a workflow queue."""

from __future__ import annotations

import time
from collections import Counter
from contextlib import contextmanager
from typing import Any

from .alignment import _clean_text
from .constants import SAMPLE_RATE, _MEL_FRAMES_PER_SECOND
from .diagnostics import generation_evidence
from .preparation import estimate_preparation, prepared_input_bytes


def batch_generation_evidence(tokens: Any, model: Any, budget: int) -> dict:
    """Ignore only padding after the first observed EOS, retaining its token."""
    eos = getattr(getattr(model, "generation_config", None), "eos_token_id", None)
    if eos is None:
        eos = getattr(getattr(model, "config", None), "eos_token_id", None)
    if eos is not None:
        values = tokens[0].tolist()
        stop = set(eos if isinstance(eos, (list, tuple)) else (eos,))
        for index, value in enumerate(values):
            if value in stop:
                tokens = tokens[:, :index + 1]
                break
    return generation_evidence(tokens, model, budget)


class BatchExecutor:
    """One prepared/running native batch at a time, with independent stage caps."""

    def __init__(self, runner, models, report: dict, *, minimum_samples: int, bust_cache: bool):
        self.runner, self.models, self.report = runner, models, report
        self.config = runner.config
        self.minimum, self.bust_cache = minimum_samples, bust_cache
        self.fallbacks = Counter()
        self.batch_index = 0
        self.evidence = report["batching"] = {
            "schema_version": 2, "scope": "same_task_same_pass", "pending_windows": 1,
            "running_batches": 1, "build_workers": 1, "max_wait_s": 0,
            "legacy_capacity_fields": ["pending_windows", "running_batches", "build_workers"],
            "limits": {"active_windows": 1, "pending_windows": 1, "running_batches": 1, "build_workers": 1},
            "current": {"active_windows": 0, "pending_windows": 0, "running_batches": 0, "build_workers": 0},
            "peaks": {"active_windows": 0, "pending_windows": 0, "running_batches": 0, "build_workers": 0},
            "activity_scope": "scheduler_windows_batch_stages_and_native_preparation; serial_fallback_preparation_delegated",
            "dispatch": {"order": "original_chunk_fifo", "batch_fill_wait_s": 0,
                         "deadline_owner": "parent_inference_session", "cross_task_fairness": "not_applicable_owned_task"},
            "asr_batch_size": self.config.asr_batch_size, "aligner_batch_size": self.config.aligner_batch_size,
            "max_audio_seconds": self.config.batch_max_audio_seconds,
            "max_input_bytes": self.config.batch_max_input_bytes, "max_output_tokens": self.config.batch_max_tokens,
            "memory_scope": "conservative_waveform_reservation_and_observed_inputs_not_total_rss_or_vram",
            "resource_admission": {"scope": "additional_batch_preparation", "reservation_strategy": "waveform_x64_v1",
                "requested_reservation_peak_bytes": 0, "total_cpu_rss": "not_enforced", "total_gpu_vram": "not_enforced",
                "excluded": ["resident_models", "processor_temporaries", "kv_cache", "compiler_graph_state",
                             "allocator_fragmentation", "other_processes"]},
            "fallback_counts": {}, "batches": [], "observed_input_peak_bytes": 0,
        }
        self.timings = report["batch_timings_s"] = {"decode": 0.0, "align": 0.0}

    @contextmanager
    def _active(self, *names):
        """Observe synchronous ownership, including suspended/closed generators."""
        current, peaks = self.evidence["current"], self.evidence["peaks"]
        for name in names:
            current[name] += 1
            peaks[name] = max(peaks[name], current[name])
        try:
            yield
        finally:
            for name in names:
                current[name] -= 1

    def _trace(self, phase: str, started: float, items: list[dict]) -> None:
        if self.runner._trace is not None:
            self.runner._trace.append({"phase": phase, "start_s": started - self.runner._trace_origin,
                "end_s": time.perf_counter() - self.runner._trace_origin,
                "chunk_index": None, "chunk_indices": [item["chunk_index"] for item in items],
                "clock": "process_perf_counter", "measurement": "wall"})

    def _fallback(self, reason: str) -> None:
        self.fallbacks[reason] += 1
        self.evidence["fallback_counts"] = dict(self.fallbacks)

    def _window_admitted(self, indices: list[int], chunks: list) -> bool:
        lengths = [max(len(chunks[index][0]), self.minimum) for index in indices]
        samples = sum(lengths)
        reservation = estimate_preparation(samples * 4).reserved_bytes
        admission = self.evidence["resource_admission"]
        admission["requested_reservation_peak_bytes"] = max(admission["requested_reservation_peak_bytes"], reservation)
        return (samples / SAMPLE_RATE <= self.config.batch_max_audio_seconds
                and reservation <= self.config.batch_max_input_bytes
                and max(lengths) <= 2 * min(lengths))

    def run(self, chunks: list):
        import numpy as np

        width = max(self.config.asr_batch_size, self.config.aligner_batch_size)
        cursor = 0
        while cursor < len(chunks):
            with self._active("active_windows"):
                with self._active("pending_windows", "build_workers"):
                    indices = []
                    while cursor < len(chunks) and len(indices) < width:
                        if indices and not self._window_admitted(indices + [cursor], chunks):
                            self._fallback("window_capacity_or_length_bucket")
                            break
                        indices.append(cursor)
                        cursor += 1
                    items = []
                    for index in indices:
                        audio = np.asarray(chunks[index][0], dtype=np.float32)
                        if len(audio) < self.minimum:
                            audio = np.pad(audio, (0, self.minimum - len(audio)))
                        items.append({"chunk_index": index, "audio": audio})
                    if len(items) == 1 and width > 1 and not self._window_admitted(indices, chunks):
                        self._fallback("single_chunk_exceeds_batch_reservation")
                for offset in range(0, len(items), self.config.asr_batch_size):
                    with self._active("running_batches"):
                        self._decode(items[offset:offset + self.config.asr_batch_size])
                voiced = [item for item in items if item["text"]]
                for offset in range(0, len(voiced), self.config.aligner_batch_size):
                    with self._active("running_batches"):
                        self._align(voiced[offset:offset + self.config.aligner_batch_size])
                for item in items:
                    item.setdefault("raw_units", [])
                    item.pop("audio")
                    yield item

    def _observe_inputs(self, inputs) -> bool:
        observed = prepared_input_bytes(inputs)
        if observed is None:
            self._fallback("prepared_input_size_unknown")
            return False
        self.evidence["observed_input_peak_bytes"] = max(self.evidence["observed_input_peak_bytes"], observed)
        if observed > self.config.batch_max_input_bytes:
            self._fallback("prepared_input_budget")
            return False
        return True

    def _decode_serial(self, items: list[dict]) -> None:
        from .progress import _progress
        for item in items:
            self.runner._trace_chunk_index = item["chunk_index"]
            text, language = self.runner._transcribe_chunk(self.models, item["audio"], bust_cache=self.bust_cache)
            item.update(text=text, language=language, generation=dict(self.runner._last_generation))
            _progress("decoded")

    def _decode(self, items: list[dict]) -> None:
        started = time.perf_counter()
        try:
            if len(items) == 1 or not self.runner.backend.capabilities.native_asr_batch:
                if len(items) > 1:
                    self._fallback("backend_asr_batch_unsupported")
                self._decode_serial(items)
                return
            self._decode_native(items)
        finally:
            self.timings["decode"] += time.perf_counter() - started
            self._trace("batch_decode", started, items)

    def _decode_native(self, items: list[dict]) -> None:
        import torch
        from .progress import _progress
        _progress("decode")

        # Admission before expensive feature extraction. The exact mask budget
        # is checked again below; this estimate never truncates a singleton.
        estimated = sum(max(self.config.min_new_tokens, int(len(item["audio"]) / SAMPLE_RATE
                        * self.config.tokens_per_second)) for item in items)
        if estimated > self.config.batch_max_tokens:
            self._fallback("output_token_budget")
            self._decode_serial(items)
            return
        hotwords = self.runner._prompt_hotwords()
        prompt = "Vocabulary: " + ", ".join(hotwords) if hotwords else None
        prepared_clock = time.perf_counter()
        with self._active("build_workers"):
            inputs = self.models.processor.apply_transcription_request(audio=[item["audio"] for item in items],
                language=[self.config.language] * len(items), prompt=[prompt] * len(items))
        budgets = [max(self.config.min_new_tokens, int(float(value) / _MEL_FRAMES_PER_SECOND * self.config.tokens_per_second))
                   for value in inputs["input_features_mask"].sum(-1)]
        self._trace("batch_asr_prepare", prepared_clock, items)
        if len(budgets) != len(items):
            raise ValueError("ASR batch processor changed item count")
        if len(set(budgets)) != 1 or sum(budgets) > self.config.batch_max_tokens or not self._observe_inputs(inputs):
            if len(set(budgets)) != 1:
                self._fallback("heterogeneous_token_budgets")
            elif sum(budgets) > self.config.batch_max_tokens:
                self._fallback("output_token_budget")
            del inputs
            self._decode_serial(items)
            return
        transfer_clock = time.perf_counter()
        inputs = inputs.to(self.models.model.device, self.models.model.dtype)
        self._trace("batch_asr_transfer", transfer_clock, items)
        generate_clock = time.perf_counter()
        with torch.inference_mode():
            generated = self.runner.backend.generate(self.models.model, inputs, max_new_tokens=budgets[0],
                disable_cache=self.bust_cache and not self.config.second_pass_use_cache)
        self._trace("batch_asr_generate", generate_clock, items)
        tokens = generated[:, inputs["input_ids"].shape[1]:]
        text = self.models.processor.decode(tokens, return_format="transcription_only")
        parsed = self.models.processor.decode(tokens, return_format="parsed")
        if len(text) != len(items) or len(parsed) != len(items) or tokens.shape[0] != len(items):
            raise ValueError("ASR backend batch result count mismatch")
        batch_id = self.batch_index
        self.batch_index += 1
        self.evidence["batches"].append({"batch_index": batch_id, "stage": "asr",
            "chunk_indices": [item["chunk_index"] for item in items], "max_new_tokens": budgets[0]})
        for index, item in enumerate(items):
            evidence = batch_generation_evidence(tokens[index:index + 1], self.models.model, budgets[index])
            evidence["budget_metadata"] = {"source": "processor_mask_before_transfer"}
            item.update(text=_clean_text(text[index]), language=str(parsed[index].get("language") or ""),
                generation=evidence,
                decode_batch_index=batch_id)
        _progress("decoded")

    def _align_serial(self, items: list[dict]) -> None:
        from .progress import _progress
        for item in items:
            self.runner._trace_chunk_index = item["chunk_index"]
            item["raw_units"] = self.runner._align_chunk(self.models, item["audio"], item["text"], item["language"])
            _progress("aligned")

    def _align(self, items: list[dict]) -> None:
        import torch
        from .progress import _progress
        _progress("align")

        started = time.perf_counter()
        try:
            if len(items) == 1 or not self.runner.backend.capabilities.native_alignment_batch:
                if len(items) > 1:
                    self._fallback("backend_alignment_batch_unsupported")
                self._align_serial(items)
                return
            prepared_clock = time.perf_counter()
            with self._active("build_workers"):
                inputs, word_lists = self.models.aligner_processor.prepare_forced_aligner_inputs(
                    audio=[item["audio"] for item in items], transcript=[item["text"] for item in items],
                    language=[item["language"] or "Chinese" for item in items])
            self._trace("batch_align_prepare", prepared_clock, items)
            if len(word_lists) != len(items):
                raise ValueError("alignment batch processor changed item count")
            if not self._observe_inputs(inputs):
                del inputs
                self._align_serial(items)
                return
            transfer_clock = time.perf_counter()
            inputs = inputs.to(self.models.aligner.device, self.models.aligner.dtype)
            self._trace("batch_align_transfer", transfer_clock, items)
            forward_clock = time.perf_counter()
            with torch.inference_mode():
                logits = self.runner.backend.align(self.models.aligner, inputs)
            self._trace("batch_align_forward", forward_clock, items)
            decoded_clock = time.perf_counter()
            units = self.models.aligner_processor.decode_forced_alignment(logits=logits,
                input_ids=inputs["input_ids"], word_lists=word_lists,
                timestamp_token_id=self.models.aligner.config.timestamp_token_id)
            self._trace("batch_align_postprocess", decoded_clock, items)
            if len(units) != len(items):
                raise ValueError("alignment backend batch result count mismatch")
            batch_id = self.batch_index
            self.batch_index += 1
            self.evidence["batches"].append({"batch_index": batch_id, "stage": "align",
                                           "chunk_indices": [item["chunk_index"] for item in items]})
            for item, value in zip(items, units, strict=True):
                item.update(raw_units=list(value), align_batch_index=batch_id)
            _progress("aligned")
        finally:
            self.timings["align"] += time.perf_counter() - started
            self._trace("batch_align", started, items)
