"""Offline source DTOs retained for existing archive compatibility."""

from bili_asr.platform_identity import ContentRef
from bili_asr.sources.models import (
    BilibiliGateway,
    GatewayAuthenticationError,
    GatewayError,
    GatewayNotFound,
    GatewayRateLimited,
    GatewayResponseError,
    GatewayShapeError,
    GatewayTransportError,
    SubtitleSegment,
    SubtitleTrack,
    UserVideoPage,
    VideoPart,
    VideoSummary,
    VideoTag,
)
from bili_asr.sources.protocols import (
    AudioSource,
    ContentPart,
    MetadataSource,
    SourceAccessObservation,
    SubtitleSource,
)

__all__ = [
    "AudioSource",
    "BilibiliGateway",
    "ContentPart",
    "ContentRef",
    "GatewayAuthenticationError",
    "GatewayError",
    "GatewayNotFound",
    "GatewayRateLimited",
    "GatewayResponseError",
    "GatewayShapeError",
    "GatewayTransportError",
    "MetadataSource",
    "SourceAccessObservation",
    "SubtitleSegment",
    "SubtitleSource",
    "SubtitleTrack",
    "UserVideoPage",
    "VideoPart",
    "VideoSummary",
    "VideoTag",
]
