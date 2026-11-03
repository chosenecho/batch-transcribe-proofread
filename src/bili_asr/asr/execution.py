"""Low-cost execution identity and clocks; no model imports or device work."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict
from typing import Any
from uuid import uuid4


def clock_anchor(*, monotonic: float | None = None) -> dict:
    """An explicit local clock domain, plus a wall anchor with sampling uncertainty."""
    before = time.perf_counter()
    epoch_ns = time.time_ns()
    after = time.perf_counter()
    return {"clock": "process_perf_counter", "process_id": os.getpid(),
            "domain_id": uuid4().hex, "origin_s": before if monotonic is None else monotonic,
            "anchor_perf_counter_s": (before + after) / 2,
            "anchor_utc_epoch_ns": epoch_ns, "anchor_uncertainty_s": (after - before) / 2,
            "cross_process_alignment": "wall_anchor_estimate_not_a_synchronized_gpu_clock"}


def _label(value: Any) -> str | None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_. +:/()-]{1,120}", value) is None:
        return None
    return value


def _dtype(model: Any) -> str | None:
    value = str(getattr(model, "dtype", ""))
    return value if value in {"torch.bfloat16", "torch.float16", "torch.float32", "bfloat16", "float16", "float32"} else None


def _attention(model: Any) -> str | None:
    config = getattr(model, "config", None)
    value = getattr(config, "_attn_implementation", None)
    return value if isinstance(value, str) and value in {"eager", "sdpa", "flash_attention_2", "flash_attention_3", "flex_attention"} else None


def _model_cache(model: Any) -> bool | None:
    for config in (getattr(model, "generation_config", None),
                   getattr(getattr(model, "config", None), "text_config", None),
                   getattr(model, "config", None)):
        value = getattr(config, "use_cache", None)
        if isinstance(value, bool):
            return value
    return None


def hardware_evidence(device: Any) -> dict:
    """Query already-loaded Torch properties; never load Torch or synchronize it."""
    result = {"status": "unobserved", "device_type": None, "name": None,
              "memory_bytes": None, "multiprocessor_count": None, "architecture": None,
              "driver": None, "driver_observation": "not_collected"}
    device_type = getattr(device, "type", None)
    if device_type is None and isinstance(device, str):
        device_type = device.split(":", 1)[0]
    if device_type not in ("cpu", "cuda", "rocm", "mps"):
        return result
    result["device_type"] = device_type
    if device_type == "cpu":
        result["status"] = "observed"
        return result
    torch = sys.modules.get("torch")
    cuda = getattr(torch, "cuda", None)
    if device_type not in ("cuda", "rocm") or not callable(getattr(cuda, "get_device_properties", None)):
        return result
    try:
        properties = cuda.get_device_properties(device)
        result.update(status="observed", name=_label(properties.name),
                      memory_bytes=int(properties.total_memory),
                      multiprocessor_count=int(properties.multi_processor_count))
        architecture = getattr(properties, "gcnArchName", None)
        if architecture:
            result["architecture"] = _label(architecture)
        else:
            result["architecture"] = [int(properties.major), int(properties.minor)]
    except (AttributeError, TypeError, ValueError, RuntimeError):
        result["status"] = "unobserved"
    return result


def execution_policy(config: Any, models: Any, *, prefetch: bool, prefetch_bytes: int) -> dict:
    """Stable policy identity excludes clocks, content, paths and process IDs."""
    encoded = json.dumps(asdict(config), sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    return {"schema_version": 1, "backend": {"requested": "huggingface", "resolved": "huggingface"},
            "runtime_config_sha256": hashlib.sha256(encoded).hexdigest(),
            "asr_precision": {"requested": config.model_dtype, "resolved": _dtype(models.model)},
            "aligner_precision": {"requested": config.aligner_dtype, "resolved": _dtype(models.aligner)},
            "asr_attention": {"requested": "checkpoint_default" if config.asr_attention == "default" else config.asr_attention,
                              "resolved": _attention(models.model)},
            "aligner_attention": {"requested": "checkpoint_default" if config.aligner_attention == "default" else config.aligner_attention,
                                  "resolved": _attention(models.aligner)},
            "generation_cache": {"first_pass": "checkpoint_default", "checkpoint_default": _model_cache(models.model),
                                 "second_pass": "checkpoint_default" if config.second_pass_use_cache else "disabled",
                                 "implementation_requested": config.asr_cache_implementation,
                                 "cross_request_past_key_values": False},
            "compile": {"requested": config.asr_compile or config.aligner_compile,
                        "asr_requested": config.asr_compile, "aligner_requested": config.aligner_compile,
                        "resolved_observation": "runtime_strategies"},
            "cuda_graph": {"requested": False, "runner_enabled": False},
            "chunk_seconds": config.chunk_seconds, "asr_batch_size": config.asr_batch_size,
            "aligner_batch_size": config.aligner_batch_size,
            "batch_limits": {"audio_seconds": config.batch_max_audio_seconds,
                             "input_bytes": config.batch_max_input_bytes, "tokens": config.batch_max_tokens},
            "prefetch": {"enabled": prefetch, "depth": 1, "budget_bytes": prefetch_bytes,
                         "resolved_enabled": prefetch and config.asr_batch_size == config.aligner_batch_size == 1,
                         "estimate_strategy": "waveform_x64_v1"}}
