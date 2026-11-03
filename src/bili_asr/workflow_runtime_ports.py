"""Typed construction and inference ports used by concrete workflow handlers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from bili_asr.workflow import JobHandler

if TYPE_CHECKING:
    from bili_asr.asr.config import ASRConfig


class WorkflowAsrRunner(Protocol):
    """The in-process inference behaviors consumed by this application."""

    def set_hotword_evidence(
        self, *, evidence_text: str | None, paired_subtitle_text: str | None
    ) -> None: ...

    def transcribe(
        self, audio_path: str, *, bust_cache: bool = False
    ) -> list[dict[str, Any]]: ...

    def rebuild_hotwords_from_first_pass(self, transcript_text: str) -> list[str]: ...

    def provenance(self) -> dict[str, str]: ...

    def transcribed_coverage(self) -> dict[str, Any] | None: ...

    def release(self) -> None: ...


class RunnerFactory(Protocol):
    """Construct one CPU runner for one frozen workflow profile."""

    def __call__(self, config: ASRConfig) -> WorkflowAsrRunner: ...


class TimeoutTranscriber(Protocol):
    """A bounded GPU inference call, supervised independently of CPU runners."""

    def __call__(
        self,
        config: ASRConfig,
        audio_path: str,
        *,
        paired_subtitle_text: str | None,
        timeout_seconds: float,
        diagnostics_sink: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any] | None]: ...


class ConfigResolver(Protocol):
    """Relocate validated checkpoints without changing a frozen profile."""

    def __call__(self, config: ASRConfig) -> tuple[ASRConfig, Mapping[str, Any]]: ...


class InferenceSession(Protocol):
    def prepare(self, config: ASRConfig, *, request: Any, timeout_seconds: float,
                audio_path: str | None, checkpoint: Callable[[], None]) -> Mapping[str, Any]: ...
    def transcribe(self, config: ASRConfig, audio_path: str, *, request: Any,
                   paired_subtitle_text: str | None, timeout_seconds: float,
                   checkpoint: Callable[[], None],
                   diagnostics_sink: dict[str, Any]) -> dict[str, Any]: ...
    def close(self) -> None: ...


__all__ = [
    "ConfigResolver", "InferenceSession", "JobHandler",
    "RunnerFactory", "TimeoutTranscriber", "WorkflowAsrRunner",
]
