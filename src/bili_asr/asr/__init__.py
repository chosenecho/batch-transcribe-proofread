"""Lazy ASR facade; configuration imports do not load runners."""
from __future__ import annotations
import subprocess as subprocess
from importlib import import_module
from collections.abc import Callable
from typing import Any, Protocol
from .config import ASRConfig
from . import constants as _constants
from .constants import _INSTALL_HINT as _INSTALL_HINT
# Preload this pure namesake before defining the legacy provenance function.
from .provenance import apply_provenance_evidence, provenance_language
class RunnerPort(Protocol):
    """Public one-shot facade only needs transcription and provenance."""

    def transcribe(self, audio_path: str) -> list[dict[str, Any]]: ...
    def provenance(self) -> dict[str, str]: ...
_EXPORTS = {
    '_fmt_srt_time': ('bili_asr.cues', '_fmt_srt_time'),
    'segments_to_srt': ('bili_asr.cues', 'segments_to_srt'),
    'segments_to_txt': ('bili_asr.cues', 'segments_to_txt'),
    '_aligned_cues': ('bili_asr.asr.alignment', '_aligned_cues'),
    '_characters_from_pieces': ('bili_asr.asr.alignment', '_characters_from_pieces'),
    '_clean_text': ('bili_asr.asr.alignment', '_clean_text'),
    '_thread_text': ('bili_asr.asr.alignment', '_thread_text'),
    '_decode_with_ffmpeg': ('bili_asr.asr.audio', '_decode_with_ffmpeg'),
    '_materialize_input': ('bili_asr.asr.audio', '_materialize_input'),
    '_read_audio': ('bili_asr.asr.audio', '_read_audio'),
    '_split_audio': ('bili_asr.asr.audio', '_split_audio'),
    'ASRConfig': ('bili_asr.asr.config', 'ASRConfig'),
    'default_config': ('bili_asr.asr.config', 'default_config'),
    'apply_coverage_evidence': ('bili_asr.asr.coverage', 'apply_coverage_evidence'),
    'characters_of': ('bili_asr.asr.coverage', 'characters_of'),
    'coverage_verdict': ('bili_asr.asr.coverage', 'coverage_verdict'),
    'transcribed_coverage': ('bili_asr.asr.coverage', 'transcribed_coverage'),
    'ASRDependencyError': ('bili_asr.asr.errors', 'ASRDependencyError'),
    'ASRRuntimeCompatibilityError': ('bili_asr.asr.errors', 'ASRRuntimeCompatibilityError'),
    'ASRModelError': ('bili_asr.asr.errors', 'ASRModelError'),
    'AudioDecodeError': ('bili_asr.asr.errors', 'AudioDecodeError'),
    'evidence_guard_hotwords': ('bili_asr.asr.hotwords', 'evidence_guard_hotwords'),
    'filter_hotwords': ('bili_asr.asr.hotwords', 'filter_hotwords'),
    'ASRInferenceTimeoutError': ('bili_asr.asr.runner', 'ASRInferenceTimeoutError'),
    'ASRRunner': ('bili_asr.asr.runner', 'ASRRunner'),
    'transcribe_with_timeout': ('bili_asr.asr.session', 'transcribe_with_timeout'),
    'two_pass_transcribe': ('bili_asr.asr.runner', 'two_pass_transcribe'),
}


def __getattr__(name):
    if not name.startswith("_") and hasattr(_constants, name):
        return getattr(_constants, name)
    try:
        module, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))


def transcribe(
    audio_path: str,
    model_name: str | None = None,
    *,
    runner_factory: Callable[[ASRConfig | None], RunnerPort] | None = None,
) -> list[dict[str, Any]]:
    """Transcribe through the runner configured at this public boundary."""

    factory = (globals()["ASRRunner"] if "ASRRunner" in globals() else __getattr__("ASRRunner")) if runner_factory is None else runner_factory
    return import_module("bili_asr.asr.runner").transcribe(audio_path, model_name, runner_factory=factory)


def provenance(
    *, runner_factory: Callable[[ASRConfig | None], RunnerPort] | None = None
) -> dict[str, str]:
    """Read default provenance without loading models or changing module state."""

    factory = (globals()["ASRRunner"] if "ASRRunner" in globals() else __getattr__("ASRRunner")) if runner_factory is None else runner_factory
    return import_module("bili_asr.asr.runner").provenance(runner_factory=factory)


__all__ = [name for name in _EXPORTS if not name.startswith("_")] + [
    "apply_provenance_evidence", "provenance_language", "provenance", "transcribe",
]
