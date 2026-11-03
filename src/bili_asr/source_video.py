"""Source video facts shared by providers and persistence, without IO."""
from __future__ import annotations

from dataclasses import dataclass, field

from bili_asr.platform_identity import ContentRef
from bili_asr.source_identity import source_url
from bili_asr.source_metadata import SourceMetadataSnapshot, valid_source_text
from bili_asr.source_observations import PlatformObservation


@dataclass(frozen=True, slots=True)
class SourceVideoMetadata:
    ref: ContentRef
    title: str
    duration_ms: int
    creator_external_id: str | None = None
    creator_name: str | None = None
    published_at: int | None = None
    original_language: str | None = None
    observed_at: int | None = None
    observations: tuple[PlatformObservation, ...] = field(default=(), compare=False, repr=False)

    def __post_init__(self):
        source_url(self.ref)
        if not isinstance(self.observations, tuple) or any(not isinstance(item, PlatformObservation) for item in self.observations):
            raise ValueError("invalid raw source observations")
        if any(item.envelope["platform"] != self.ref.platform or
               item.envelope["scope"].get("externalVideoId") != self.ref.external_video_id
               for item in self.observations):
            raise ValueError("raw observation scope differs from source video")
        if type(self.duration_ms) is not int or self.duration_ms <= 0:
            raise ValueError("source duration must be positive milliseconds")
        # Source ingestion and publication must accept the same domain facts.
        # Reuse the frozen DTO's bounds instead of admitting rows that later
        # make archive projection/export fail when a DTO is reconstructed.
        SourceMetadataSnapshot(self.ref, self.title, self.creator_external_id,
                               self.creator_name, self.published_at, self.observed_at,
                               duration_ms=self.duration_ms)
        value = self.original_language
        if value is not None and (not valid_source_text(value, limit=512) or not value.strip()):
            raise ValueError("invalid original source language")
