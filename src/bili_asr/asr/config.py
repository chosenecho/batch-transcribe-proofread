"""Config implementation."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

import bili_asr.asr.constants as _dependency_constants
import bili_asr.asr.hotwords as _dependency_hotwords


def _is_redaction_safe_model_identifier(value: str, *, hub_level: bool = False) -> bool:
    """The redaction rule for model identifiers, in one place.

    An identifier-shaped value with no forbidden marker is safe to serialize — that is the rule
    ``provenance()`` has always applied to ``model_name``.  ``hub_level`` additionally demands a
    slash-qualified ``owner/name`` shape, which is what a *declared* producer identity must look like.

    Note what this rule deliberately does **not** redact: a relative checkpoint path such as
    ``models/Qwen3-ASR-1.7B-hf`` passes through, because that is the documented form of a local
    checkpoint (``README.md``) and provenance is supposed to name it.
    """

    if not isinstance(value, str) or not value.strip():
        return False
    if _dependency_constants._MODEL_IDENTIFIER.fullmatch(value) is None:
        return False
    if _dependency_constants._FORBIDDEN_PROVENANCE.search(value) is not None:
        return False
    return not hub_level or "/" in value


def _is_hub_level_model_name(value: str) -> bool:
    """Whether ``value`` names a hub repository rather than a local path.

    The shape alone cannot tell ``models/Qwen3-ASR-1.7B-hf`` from ``Qwen/Qwen3-ASR-1.7B-hf`` — both
    are two segments once — and the contradiction check must not punish the relative-path form the
    README documents.  Existence answers it on the machine that owns the layout: the checkpoint the
    operator points the knob at is a directory that is *there*, and a hub id is a name that is not.
    The probe resolves against the process working directory, exactly as the loader would.
    """

    if not _is_redaction_safe_model_identifier(value, hub_level=True):
        return False
    return not os.path.isdir(value)


@dataclass(frozen=True)
class ASRConfig:
    """Deterministic, redaction-safe configuration for one ASR run.

    ``language`` is the operator's declaration of what is spoken, passed to both models; ``None``
    leaves the model's own detection in place. The declared language remains in provenance;
    per-chunk detected languages are diagnostic observations.

    ``hotwords`` biases the decoder through the processor's free-form ``prompt``; an empty tuple
    sends no bias, and the terms are recorded in provenance.  Governance ruling 2026-09-28:
    speculative seeding is off, so the shipped default is empty and every token that does reach
    the prompt is admitted by :func:`evidence_guard_hotwords` against the run's own evidence
    (the runner's per-run state, not this config — see :class:`ASRRunner`).

    ``model_name`` / ``aligner_name`` are hub ids or local checkpoint directories.  ``model_id`` is the
    operator's declaration of the hub-level identity behind the ASR checkpoint.
    """

    model_name: str
    aligner_name: str = _dependency_constants.DEFAULT_ALIGNER_MODEL
    model_revision: str | None = None
    aligner_revision: str | None = None
    device: str = "cuda"
    language: str | None = None
    # 2026-09-28 governance ruling (plan 20260928-hotword-injection-governance,
    # residual 20260922-proofread-wave R1): speculative seeding is off.  No term
    # enters the decoder prompt unless evidence-based seeding admits it (see
    # ``evidence_guard_hotwords``); the shipped default is therefore empty and
    # ``BILI_ASR_HOTWORDS`` supplies operator-declared terms that pass the same
    # guard at run time.
    hotwords: tuple[str, ...] = _dependency_constants.DEFAULT_HOTWORDS
    chunk_seconds: float = _dependency_constants.DEFAULT_CHUNK_SECONDS
    inference_timeout_seconds: float = _dependency_constants.DEFAULT_INFERENCE_TIMEOUT_SECONDS
    inference_timeout_policy: str = "fixed"
    inference_max_rtf: float = 0.25
    inference_startup_seconds: float = 300.0
    inference_hard_cap_seconds: float = 14400.0
    inference_no_progress_seconds: float = 600.0
    tokens_per_second: float = _dependency_constants._MAX_NEW_TOKENS_PER_AUDIO_SECOND
    min_new_tokens: int = _dependency_constants._MIN_NEW_TOKENS
    second_pass_use_cache: bool = False
    offline: bool = True
    local_source: str = "configured-local"
    model_id: str | None = None
    model_dtype: str = "bfloat16"
    aligner_dtype: str = "bfloat16"
    asr_batch_size: int = 1
    aligner_batch_size: int = 1
    batch_max_audio_seconds: float = 360.0
    batch_max_input_bytes: int = 64 * 1024**2
    batch_max_tokens: int = 8192
    asr_attention: str = "default"
    aligner_attention: str = "default"
    asr_cache_implementation: str = "default"
    asr_compile: bool = False
    aligner_compile: bool = False
    compile_max_buckets: int = 2
    compile_max_input_tokens: int = 4096
    compile_max_output_tokens: int = 2048

    def __post_init__(self) -> None:
        for name in ("asr_attention", "aligner_attention"):
            if getattr(self, name) not in ("default", "eager", "sdpa", "flash_attention_2"):
                raise ValueError(f"{name} is unsupported")
        if self.asr_cache_implementation not in ("default", "dynamic", "static"):
            raise ValueError("unsupported ASR cache implementation")
        for name in ("asr_compile", "aligner_compile"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a bool")
        if self.asr_compile and self.asr_cache_implementation != "static":
            raise ValueError("ASR compilation requires explicit static cache")
        if type(self.compile_max_buckets) is not int or not 1 <= self.compile_max_buckets <= 8:
            raise ValueError("compile_max_buckets must be an integer within 1..8")
        for name in ("compile_max_input_tokens", "compile_max_output_tokens"):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= 32768:
                raise ValueError(f"{name} must be an integer within 1..32768")
        for name in ("asr_batch_size", "aligner_batch_size"):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= 8:
                raise ValueError(f"{name} must be an integer within 1..8")
        if (isinstance(self.batch_max_audio_seconds, bool)
                or not isinstance(self.batch_max_audio_seconds, (int, float))
                or not math.isfinite(self.batch_max_audio_seconds) or not 0 < self.batch_max_audio_seconds <= 1800):
            raise ValueError("batch_max_audio_seconds must be finite and within 0..1800")
        for name in ("batch_max_input_bytes", "batch_max_tokens"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("model_dtype", "aligner_dtype"):
            if getattr(self, name) not in ("bfloat16", "float16"):
                raise ValueError(f"{name} must be bfloat16 or float16")
        for name, value in (("model_name", self.model_name), ("aligner_name", self.aligner_name)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.model_revision is not None and (
            not isinstance(self.model_revision, str) or not self.model_revision.strip()
        ):
            raise ValueError("model_revision must be a non-empty string or null")
        if self.aligner_revision is not None and (
            not isinstance(self.aligner_revision, str) or not self.aligner_revision.strip()
        ):
            raise ValueError("aligner_revision must be a non-empty string or null")
        if not isinstance(self.device, str) or not self.device.strip():
            raise ValueError("device must be a non-empty string")
        if self.language is not None and (
            not isinstance(self.language, str) or not self.language.strip()
        ):
            raise ValueError("language must be a non-empty string or null")
        if isinstance(self.chunk_seconds, bool) or not isinstance(self.chunk_seconds, (int, float)):
            raise ValueError("chunk_seconds must be a positive number")
        if not math.isfinite(float(self.chunk_seconds)) or self.chunk_seconds <= 0:
            raise ValueError("chunk_seconds must be finite and positive")
        if (
            isinstance(self.tokens_per_second, bool)
            or not isinstance(self.tokens_per_second, (int, float))
            or not math.isfinite(float(self.tokens_per_second))
            or self.tokens_per_second <= 0
        ):
            raise ValueError("tokens_per_second must be finite and positive")
        if isinstance(self.min_new_tokens, bool) or not isinstance(self.min_new_tokens, int) or self.min_new_tokens < 1:
            raise ValueError("min_new_tokens must be a positive integer")
        if not isinstance(self.second_pass_use_cache, bool):
            raise ValueError("second_pass_use_cache must be a bool")  # noqa: TRY004 - configuration validation contract
        if (
            isinstance(self.inference_timeout_seconds, bool)
            or not isinstance(self.inference_timeout_seconds, (int, float))
            or not math.isfinite(float(self.inference_timeout_seconds))
            or self.inference_timeout_seconds <= 0
        ):
            raise ValueError("inference_timeout_seconds must be finite and positive")
        from .deadlines import validate_deadline_config
        validate_deadline_config(self)
        if not isinstance(self.hotwords, tuple) or any(
            not isinstance(term, str) or not term.strip() for term in self.hotwords
        ):
            raise ValueError("hotwords must be a tuple of non-empty strings")
        if not isinstance(self.offline, bool):
            raise ValueError("offline must be a bool")
        if not isinstance(self.local_source, str) or not self.local_source.strip():
            raise ValueError("local_source must be a non-empty identifier")
        if _dependency_constants._FORBIDDEN_LOCAL_SOURCE.search(self.local_source):
            raise ValueError("local_source must be an opaque local identifier")
        if self.model_id is not None:
            if not isinstance(self.model_id, str) or not self.model_id.strip():
                raise ValueError(f"{_dependency_constants.ASR_MODEL_ID_ENV_VAR} must be a hub-level model identifier")
            if not _is_redaction_safe_model_identifier(self.model_id, hub_level=True):
                raise ValueError(f"{_dependency_constants.ASR_MODEL_ID_ENV_VAR} must be a hub-level model identifier")
            if _is_hub_level_model_name(self.model_name) and self.model_name != self.model_id:
                raise ValueError(
                    f"{_dependency_constants.ASR_MODEL_ID_ENV_VAR} contradicts {_dependency_constants.ASR_MODEL_ENV_VAR}: "
                    f"declared {self.model_id!r}, loaded {self.model_name!r}"
                )


def _resolve_chunk_seconds(environment_value: str | None) -> float:
    """Configured chunk cap in seconds; unset keeps the measured default, blank stays unset."""

    if environment_value is None or not environment_value.strip():
        return _dependency_constants.DEFAULT_CHUNK_SECONDS
    try:
        value = float(environment_value.strip().rstrip("sS"))
    except ValueError:
        raise ValueError(
            f"{_dependency_constants.ASR_CHUNK_SECONDS_ENV_VAR} must be a positive number of seconds"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{_dependency_constants.ASR_CHUNK_SECONDS_ENV_VAR} must be a positive number of seconds")
    return value


def _resolve_inference_timeout(environment_value: str | None) -> float:
    """Resolve the hard child-process deadline for GPU/ROCm inference."""

    if environment_value is None or not environment_value.strip():
        return _dependency_constants.DEFAULT_INFERENCE_TIMEOUT_SECONDS
    try:
        value = float(environment_value.strip().rstrip("sS"))
    except ValueError:
        raise ValueError(
            f"{_dependency_constants.ASR_INFERENCE_TIMEOUT_ENV_VAR} must be a finite positive number of seconds"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"{_dependency_constants.ASR_INFERENCE_TIMEOUT_ENV_VAR} must be a finite positive number of seconds"
        )
    return value


def default_config(**overrides) -> ASRConfig:
    """Build the runner configuration from the documented environment knobs.

    ``BILI_ASR_MODEL`` and ``BILI_ASR_ALIGNER_MODEL`` carry hub ids or **local checkpoint
    directories**; the archive's own checkpoints live under ``models/`` so a
    run never touches the network.  ``BILI_ASR_MODEL_ID`` is the operator's declaration of the
    hub-level identity behind the ASR checkpoint: read for provenance only, and refused when it
    contradicts a hub-level ``BILI_ASR_MODEL``.
    """

    values = {
        "model_name": os.environ.get(_dependency_constants.ASR_MODEL_ENV_VAR) or _dependency_constants.DEFAULT_MODEL,
        "aligner_name": os.environ.get(_dependency_constants.ASR_ALIGNER_ENV_VAR) or _dependency_constants.DEFAULT_ALIGNER_MODEL,
        "model_revision": os.environ.get(_dependency_constants.ASR_MODEL_REVISION_ENV_VAR) or None,
        "aligner_revision": os.environ.get("BILI_ASR_ALIGNER_REVISION") or None,
        "device": os.environ.get(_dependency_constants.ASR_DEVICE_ENV_VAR) or "cuda",
        "language": os.environ.get(_dependency_constants.ASR_LANGUAGE_ENV_VAR) or None,
        "hotwords": _dependency_constants.DEFAULT_HOTWORDS + _dependency_hotwords._extra_hotwords(os.environ.get(_dependency_constants.ASR_HOTWORDS_ENV_VAR)),
        "chunk_seconds": overrides.get("chunk_seconds") if "chunk_seconds" in overrides else _resolve_chunk_seconds(os.environ.get(_dependency_constants.ASR_CHUNK_SECONDS_ENV_VAR)),
        "inference_timeout_seconds": overrides.get("inference_timeout_seconds") if "inference_timeout_seconds" in overrides else _resolve_inference_timeout(
            os.environ.get(_dependency_constants.ASR_INFERENCE_TIMEOUT_ENV_VAR)
        ),
        "model_id": (os.environ.get(_dependency_constants.ASR_MODEL_ID_ENV_VAR) or "").strip() or None,
        "model_dtype": os.environ.get("BILI_ASR_MODEL_DTYPE") or "bfloat16",
        "aligner_dtype": os.environ.get("BILI_ASR_ALIGNER_DTYPE") or "bfloat16",
    }
    values.update(overrides)
    return ASRConfig(**values)
