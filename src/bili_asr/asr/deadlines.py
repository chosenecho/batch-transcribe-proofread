"""Frozen, bounded total budgets and independent real-progress watchdogs."""

from __future__ import annotations

import math
from dataclasses import dataclass

DEADLINE_DEFAULTS = {
    "inference_timeout_policy": "fixed",
    "inference_max_rtf": 0.25,
    "inference_startup_seconds": 300.0,
    "inference_hard_cap_seconds": 14400.0,
    "inference_no_progress_seconds": 600.0,
}


@dataclass(frozen=True)
class InferenceBudget:
    policy: str
    total_seconds: float
    startup_seconds: float | None
    no_progress_seconds: float | None
    audio_duration_ms: int | None
    capped: bool = False

    def evidence(self) -> dict:
        return {"schema_version": 1, **self.__dict__}


def validate_deadline_config(config) -> None:
    if type(config.inference_timeout_policy) is not str or config.inference_timeout_policy not in {"fixed", "audio-scaled"}:
        raise ValueError("unsupported inference timeout policy")
    for name in DEADLINE_DEFAULTS:
        if name == "inference_timeout_policy":
            continue
        value = getattr(config, name)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if config.inference_timeout_policy == "audio-scaled":
        if config.inference_hard_cap_seconds < max(config.inference_timeout_seconds, config.inference_startup_seconds):
            raise ValueError("inference hard cap cannot be below minimum or startup budget")


def inference_budget(config, *, duration_ms: int | None) -> InferenceBudget:
    """Legacy snapshots keep their exact fixed deadline; scaled profiles require evidence."""
    if config.inference_timeout_policy == "fixed":
        return InferenceBudget("fixed", float(config.inference_timeout_seconds), None, None, duration_ms)
    if type(duration_ms) is not int or duration_ms <= 0:
        raise ValueError("audio-scaled inference deadline requires a verified positive audio duration")
    predicted = config.inference_startup_seconds + duration_ms / 1000 * config.inference_max_rtf
    desired = max(float(config.inference_timeout_seconds), predicted)
    return InferenceBudget("audio-scaled", min(desired, config.inference_hard_cap_seconds),
                           float(config.inference_startup_seconds), float(config.inference_no_progress_seconds),
                           duration_ms, desired > config.inference_hard_cap_seconds)


class ProgressWatchdog:
    """Only finite work completions advance this clock; thread heartbeats do not."""

    def __init__(self, *, started: float, total_seconds: float, startup_seconds: float | None = None,
                 no_progress_seconds: float | None = None):
        self.started, self.total_seconds = started, total_seconds
        self.startup_seconds, self.no_progress_seconds = startup_seconds, no_progress_seconds
        self.last_progress = started
        self.progress_count = 0

    def observe(self, event: object, now: float) -> bool:
        if (not isinstance(event, dict) or type(event.get("sequence")) is not int
                or event["sequence"] != self.progress_count + 1
                or type(event.get("phase")) is not str
                or event["phase"] not in {"model_ready", "audio_ready", "decoded", "aligned", "chunk_complete"}
                or type(now) not in (int, float) or not math.isfinite(now)
                or now < self.last_progress):
            return False
        self.progress_count = event["sequence"]
        self.last_progress = now
        return True

    def expired(self, now: float) -> str | None:
        if now - self.started >= self.total_seconds:
            return "total_deadline"
        if self.progress_count == 0:
            if self.startup_seconds is not None and now - self.started >= self.startup_seconds:
                return "startup_deadline"
        elif self.no_progress_seconds is not None and now - self.last_progress >= self.no_progress_seconds:
            return "no_progress"
        return None
