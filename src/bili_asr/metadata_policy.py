"""Field-aware metadata refresh decisions, independent of repositories."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MetadataRefreshPolicy:
    mode: str = "force"
    ttl_seconds: int = 86400

    def __post_init__(self) -> None:
        if self.mode not in {"new", "missing", "stale", "force"}:
            raise ValueError("unknown metadata refresh mode")
        if type(self.ttl_seconds) is not int or self.ttl_seconds < 0:
            raise ValueError("metadata TTL must be nonnegative")

    def needs_read(self, *, known_video: bool, present: bool,
                   observed_at: int | None, now: int) -> bool:
        if not known_video or self.mode == "force":
            return True
        if self.mode == "new":
            return False
        if not present:
            return True
        if self.mode == "missing":
            return False
        return observed_at is None or now - observed_at >= self.ttl_seconds


@dataclass(frozen=True, slots=True)
class MetadataFieldObservation:
    field: str
    state: str
    value: str | int | None = None

    def __post_init__(self) -> None:
        if self.field not in {"title", "pubdate", "aid", "pic", "desc", "tid"}:
            raise ValueError("unknown metadata field")
        if self.state not in {"present", "empty", "missing", "unavailable", "denied"}:
            raise ValueError("unknown metadata observation state")
        if self.state == "present" and (self.value is None or type(self.value) not in {str, int}):
            raise ValueError("present metadata requires a scalar value")
        if self.state != "present" and self.value is not None:
            raise ValueError("a non-present observation cannot carry a value")


__all__ = ["MetadataFieldObservation", "MetadataRefreshPolicy"]
