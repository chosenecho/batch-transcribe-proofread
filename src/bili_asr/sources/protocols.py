"""Application-owned acquisition ports; provider responses stay in adapters.

These ports consume ``ContentRef`` and expose bounded application DTOs.
Signed media URLs, credentials, raw third-party dictionaries, storage writes,
workflow scheduling, and platform absence policy do not cross this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal, Protocol

from bili_asr.platform_identity import ContentRef
from bili_asr.sources.models import SubtitleBodyRead, SubtitleSegment, SubtitleTrack
from bili_asr.source_video import SourceVideoMetadata


class ContentPart(Protocol):
    """The shared facts of a part; provider extensions stay in its adapter."""

    @property
    def content_ref(self) -> ContentRef: ...

    @property
    def title(self) -> str: ...

    @property
    def duration_ms(self) -> int: ...


@dataclass(frozen=True, slots=True)
class SourceAccessObservation:
    """What an adapter checked, without deciding transcript/ASR eligibility.

    A verified anonymous request is still anonymous.  A platform policy must
    interpret the access context; it must not relabel it as a verified login.
    """

    access_context: Literal["anonymous", "credentialed"]
    verified: bool

    def __post_init__(self) -> None:
        if self.access_context not in {"anonymous", "credentialed"}:
            raise ValueError("unknown source access context")
        if type(self.verified) is not bool:
            raise TypeError("verified must be a boolean")


class MetadataSource(Protocol):
    """Fetch the processing units of one provider video.

    Creator discovery/pagination is a separate provider operation.  The
    returned parts carry their own zero-based references rather than treating
    chapters or playlists as a provider's multi-part video.
    """

    async def get_parts(
        self, ref: ContentRef, video_title_fallback: str = ""
    ) -> tuple[ContentPart, ...]: ...


class SubtitleSource(Protocol):
    """Read tracks and caption bodies with provider ids resolved by the adapter.

    A bounded provider failure raises the existing ``GatewayError`` taxonomy.
    ``verify_access`` is called only when the application needs to interpret
    an empty selection; it returns observed access facts, never ASR policy.
    """

    async def list_tracks(self, ref: ContentRef) -> tuple[SubtitleTrack, ...]: ...

    async def fetch_segments(
        self, track: SubtitleTrack, ref: ContentRef
    ) -> tuple[SubtitleSegment, ...]: ...

    async def read_body(self, track: SubtitleTrack, ref: ContentRef) -> SubtitleBodyRead: ...

    async def verify_access(self, ref: ContentRef) -> SourceAccessObservation: ...


class AudioSource(Protocol):
    """Download into a caller-owned staging root; return the real final path.

    The handler owns probing, hashing, lease checks, installation and database
    registration.  The returned file may have another real container suffix
    than the requested target (for example a retained FLAC stream).
    """

    def download_audio(
        self, ref: ContentRef, target: Path, *, staging_root: Path
    ) -> Path: ...


class CaptionCandidate(Protocol):
    """Provider-owned caption facts used by the application selection policy."""

    @property
    def track(self) -> SubtitleTrack: ...
    @property
    def language(self) -> str: ...
    @property
    def original_language(self) -> str | None: ...
    @property
    def translated(self) -> bool | None: ...

    def provenance(self) -> dict[str, Any]: ...


class AcquisitionSource(SubtitleSource, AudioSource, Protocol):
    """The complete single-video source capability consumed by its use cases."""

    def metadata(self, ref: ContentRef) -> SourceVideoMetadata: ...
    def captions(self, ref: ContentRef) -> tuple[CaptionCandidate, ...]: ...


class SourceFactory(Protocol):
    def __call__(self, *, checkpoint: Callable[[], None] | None = None,
                 cookie_file: Path | None = None) -> AcquisitionSource: ...


__all__ = [
    "AudioSource", "ContentPart", "MetadataSource", "SourceAccessObservation",
    "SubtitleSource", "AcquisitionSource", "CaptionCandidate", "SourceFactory",
]
