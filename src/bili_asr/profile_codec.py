"""Frozen ASR profile v2 encoding; defaults belong to persisted identity.

Future extensions must have an explicit nested schema version. Runtime tuning
must never change these omission rules or recompute historical profile bytes.
"""
from __future__ import annotations
import json
from typing import Any
from bili_asr.contracts.registry import ASR_PROFILE_VERSION

STRATEGY_DEFAULTS = {
    "asr_attention": "default", "aligner_attention": "default",
    "asr_cache_implementation": "default", "asr_compile": False, "aligner_compile": False,
    "compile_max_buckets": 2, "compile_max_input_tokens": 4096, "compile_max_output_tokens": 2048,
}
# Persisted omission/reconstruction rules are deliberately independent of
# runtime policy defaults. Tuning the supervisor cannot change old v2 bytes.
DEADLINE_DEFAULTS = {
    "inference_timeout_policy": "fixed", "inference_max_rtf": 0.25,
    "inference_startup_seconds": 300.0, "inference_hard_cap_seconds": 14400.0,
    "inference_no_progress_seconds": 600.0,
}

def encode_profile(values: dict[str, Any]) -> str:
    values = dict(values)
    values.pop("profile_key", None)
    deadline = {name: values.pop(name) for name in DEADLINE_DEFAULTS}
    for name in deadline:
        if name != "inference_timeout_policy":
            deadline[name] = float(deadline[name])
    if deadline != DEADLINE_DEFAULTS:
        values["deadline"] = {"schema_version": 1, **deadline}
    # Default profiles retain their exact v2 bytes and digest. Explicit
    # precision is a versioned extension inside the existing v2 envelope.
    precision = {name: values.pop(name) for name in ("model_dtype", "aligner_dtype")}
    if any(value != "bfloat16" for value in precision.values()):
        values["precision"] = {"schema_version": 1, **precision}
    batch_defaults = {"asr_batch_size": 1, "aligner_batch_size": 1, "batch_max_audio_seconds": 360.0,
                      "batch_max_input_bytes": 64 * 1024**2, "batch_max_tokens": 8192}
    batching = {name: values.pop(name) for name in batch_defaults}
    batching["batch_max_audio_seconds"] = float(batching["batch_max_audio_seconds"])
    if batching != batch_defaults:
        values["batching"] = {"schema_version": 1, **batching}
    strategies = {name: values.pop(name) for name in STRATEGY_DEFAULTS}
    if strategies != STRATEGY_DEFAULTS:
        values["runtime_strategies"] = {"schema_version": 1, **strategies}
    for name in ("chunk_seconds", "inference_timeout_seconds", "tokens_per_second"):
        values[name] = float(values[name])
    return json.dumps(
        {"schema_version": ASR_PROFILE_VERSION, **values},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )

def decode_profile(config_json: str) -> dict[str, Any]:
    values: dict[str, Any] = json.loads(config_json)
    if values.pop("schema_version") != 2:
        raise ValueError("unsupported ASR profile schema")
    deadline = values.pop("deadline", None)
    if set(values) & set(DEADLINE_DEFAULTS):
        raise ValueError("ASR deadline fields require the versioned extension")
    values.update(DEADLINE_DEFAULTS)
    if deadline is not None:
        if (not isinstance(deadline, dict) or set(deadline) != set(DEADLINE_DEFAULTS) | {"schema_version"}
                or type(deadline["schema_version"]) is not int or deadline["schema_version"] != 1):
            raise ValueError("unsupported ASR deadline schema")
        values.update({name: deadline[name] for name in DEADLINE_DEFAULTS})
    precision = values.pop("precision", None)
    if precision is not None:
        if (not isinstance(precision, dict)
                or set(precision) != {"schema_version", "model_dtype", "aligner_dtype"}
                or type(precision["schema_version"]) is not int
                or precision["schema_version"] != 1):
            raise ValueError("unsupported ASR precision schema")
        values.update({name: precision[name] for name in ("model_dtype", "aligner_dtype")})
    batching = values.pop("batching", None)
    if batching is not None:
        names = {"asr_batch_size", "aligner_batch_size", "batch_max_audio_seconds",
                 "batch_max_input_bytes", "batch_max_tokens"}
        if (not isinstance(batching, dict) or set(batching) != names | {"schema_version"}
                or type(batching["schema_version"]) is not int or batching["schema_version"] != 1):
            raise ValueError("unsupported ASR batching schema")
        values.update({name: batching[name] for name in names})
    strategies = values.pop("runtime_strategies", None)
    if strategies is not None:
        names = set(STRATEGY_DEFAULTS)
        if (not isinstance(strategies, dict) or set(strategies) != names | {"schema_version"}
                or type(strategies["schema_version"]) is not int or strategies["schema_version"] != 1):
            raise ValueError("unsupported ASR runtime strategies schema")
        values.update({name: strategies[name] for name in names})
    values["hotwords"] = tuple(values["hotwords"])
    return values
