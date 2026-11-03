"""Opt-in HF runtime strategies with finite compile admission and eager fallback."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import time


STRATEGY_DEFAULTS = {
    "asr_attention": "default", "aligner_attention": "default",
    "asr_cache_implementation": "default", "asr_compile": False, "aligner_compile": False,
    "compile_max_buckets": 2, "compile_max_input_tokens": 4096, "compile_max_output_tokens": 2048,
}


def _compiler_failure(exc: Exception) -> bool:
    return (type(exc).__module__.startswith(("torch._dynamo", "torch._inductor"))
            and type(exc).__name__ in {"BackendCompilerFailed", "Unsupported", "InductorError", "InvalidBackend"})


class RuntimeStrategies:
    """Retain compiled code/shape metadata, never request tensors or KV content."""

    def __init__(self, config):
        self.config = config
        self.buckets = {"asr": set(), "align": set()}
        self.failed = {"asr": set(), "align": set()}
        self.aligner_call = None
        self.seen = {"asr": set(), "align": set()}
        self.fallbacks = Counter()
        self.report = {"schema_version": 1, "scope": "model_lifetime_cumulative",
            "requested": {key: getattr(config, key) for key in STRATEGY_DEFAULTS},
            "attention": {}, "fallback_counts": {}, "compile_api_calls": {"asr": 0, "align": 0},
            "compiled_kernel_execution": "requires_profiler_verification",
            "first_bucket_call_wall_s": [], "admitted_shapes": {"asr": 0, "align": 0},
            "cuda_graph": "disabled", "cross_request_kv_reuse": False, "cache_cleanup_calls": 0,
            "shape_limit_scope": "external_input_signatures_not_internal_compiler_graph_count"}

    def evidence(self):
        return deepcopy(self.report)

    def fallback(self, reason):
        self.fallbacks[reason] += 1
        self.report["fallback_counts"] = dict(self.fallbacks)

    def prepare_models(self, models):
        for name, model in (("asr", models.model), ("aligner", models.aligner)):
            requested = getattr(self.config, name + "_attention")
            if requested == "default":
                self.report["attention"][name] = "checkpoint_default"
                continue
            setter = getattr(model, "set_attn_implementation", None)
            if not callable(setter):
                self.fallback(name + "_attention_api_unavailable")
                self.report["attention"][name] = "checkpoint_default"
                continue
            previous = getattr(getattr(model, "config", None), "_attn_implementation", None)
            try:
                setter(requested)
            except (ValueError, ImportError) as exc:
                # Optional kernels/capabilities are not installed automatically.
                self.fallback(name + "_attention_" + type(exc).__name__)
                current = getattr(getattr(model, "config", None), "_attn_implementation", None)
                if current != previous:
                    # A setter that partially mutated must restore successfully;
                    # otherwise model construction fails instead of claiming fallback.
                    setter(previous)
                self.report["attention"][name] = "checkpoint_default"
            else:
                self.report["attention"][name] = requested

    def _signature(self, stage, model, inputs, output_tokens=0, *, require_cuda=True):
        device = str(getattr(model, "device", ""))
        if require_cuda and not device.startswith("cuda"):
            self.fallback(stage + "_compile_requires_cuda")
            return None
        ids = inputs.get("input_ids")
        if ids is None or len(ids.shape) != 2 or ids.shape[1] > self.config.compile_max_input_tokens:
            self.fallback(stage + "_compile_input_limit")
            return None
        if output_tokens > self.config.compile_max_output_tokens:
            self.fallback(stage + "_compile_output_limit")
            return None
        # Exact observed shapes avoid silently padding/truncating audio or text.
        signature = (output_tokens, tuple((name, tuple(value.shape), str(getattr(value, "dtype", "")))
                     for name, value in sorted(inputs.items())))
        if signature in self.failed[stage]:
            self.fallback(stage + "_compile_previously_failed")
            return None
        if signature not in self.buckets[stage]:
            if len(self.buckets[stage]) >= self.config.compile_max_buckets:
                self.fallback(stage + "_compile_bucket_limit")
                return None
            self.buckets[stage].add(signature)
            self.report["admitted_shapes"][stage] = len(self.buckets[stage])
        return signature

    def _clean_cache(self, model):
        # Current HF creates fresh static caches per generate. Clear the legacy
        # cache carrier too, so older supported installations cannot retain KV.
        cached = getattr(model, "_cache", None)
        if cached is not None:
            reset = getattr(cached, "reset", None)
            if callable(reset):
                reset()
            delattr(model, "_cache")
        self.report["cache_cleanup_calls"] += 1

    def generate(self, model, inputs, *, max_new_tokens, disable_cache):
        kwargs = {"max_new_tokens": max_new_tokens}
        if disable_cache:
            kwargs["use_cache"] = False
        requested = self.config.asr_cache_implementation
        signature = None
        static_signature = None
        if not disable_cache and requested != "default":
            if requested == "static":
                static_signature = self._signature("asr", model, inputs, max_new_tokens,
                                                    require_cuda=self.config.asr_compile)
                if static_signature is None:
                    self.fallback("asr_static_cache_capacity_or_device")
                else:
                    kwargs["cache_implementation"] = requested
            else:
                kwargs["cache_implementation"] = requested
        if self.config.asr_compile:
            if disable_cache:
                self.fallback("asr_compile_cache_disabled")
            else:
                signature = static_signature
                if signature is not None:
                    try:
                        from transformers import CompileConfig
                        kwargs["compile_config"] = CompileConfig(dynamic=False, fullgraph=False,
                            mode=None, options={"triton.cudagraphs": False})
                    except (ImportError, TypeError):
                        self.fallback("asr_compile_api_unavailable")
                        self.failed["asr"].add(signature)
                        signature = None
        # HF static cache otherwise opts into compilation implicitly.
        if requested == "static" or self.config.asr_compile:
            kwargs["disable_compile"] = signature is None
        started = time.perf_counter()
        cold = signature is not None and signature not in self.seen["asr"]
        try:
            try:
                result = model.generate(**inputs, **kwargs)
            except Exception as exc:
                if signature is None or not _compiler_failure(exc):
                    raise
                self.fallback("asr_compile_" + type(exc).__name__)
                self.failed["asr"].add(signature)
                self._clean_cache(model)
                kwargs.pop("compile_config", None)
                kwargs["disable_compile"] = True
                result = model.generate(**inputs, **kwargs)
            else:
                if signature is not None:
                    self.report["compile_api_calls"]["asr"] += 1
            return result
        finally:
            if requested != "default" or self.config.asr_compile:
                self._clean_cache(model)
            if cold:
                self.report["first_bucket_call_wall_s"].append({"stage": "asr",
                    "wall_s": time.perf_counter() - started, "includes_execution_and_possible_fallback": True})
                self.seen["asr"].add(signature)

    def align(self, model, inputs):
        if not self.config.aligner_compile:
            return model(**inputs).logits
        signature = self._signature("align", model, inputs)
        if signature is None:
            return model(**inputs).logits
        started = time.perf_counter()
        cold = signature not in self.seen["align"]
        try:
            if self.aligner_call is None:
                import torch
                compiler = getattr(torch, "compile", None)
                if not callable(compiler):
                    self.fallback("align_compile_api_unavailable")
                    self.failed["align"].add(signature)
                    return model(**inputs).logits
                try:
                    self.aligner_call = compiler(model, dynamic=False, fullgraph=False,
                        options={"triton.cudagraphs": False})
                except Exception as exc:
                    if not _compiler_failure(exc) and not isinstance(exc, TypeError):
                        raise
                    self.fallback("align_compile_api_" + type(exc).__name__)
                    self.failed["align"].add(signature)
                    return model(**inputs).logits
            try:
                result = self.aligner_call(**inputs).logits
            except Exception as exc:
                if not _compiler_failure(exc):
                    raise
                self.fallback("align_compile_" + type(exc).__name__)
                self.failed["align"].add(signature)
                return model(**inputs).logits
            self.report["compile_api_calls"]["align"] += 1
            return result
        finally:
            if cold:
                self.report["first_bucket_call_wall_s"].append({"stage": "align",
                    "wall_s": time.perf_counter() - started, "includes_execution_and_possible_fallback": True})
                self.seen["align"].add(signature)

    def release(self):
        self.aligner_call = None
