"""Validated offline records and compatibility protocol for archived metadata.

Live clients and gateway adapters belong to the separate collector repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Protocol

from bili_asr.contracts.values import caption_text as _caption_text
from bili_asr.contracts.values import integer as _integer
from bili_asr.contracts.values import text as _text
from bili_asr.error_codes import validate_error_code
from bili_asr.metadata_acceptance import SourceFieldReceipt
from bili_asr.metadata_policy import MetadataFieldObservation
from bili_asr.platform_identity import ContentRef
from bili_asr.source_metadata import valid_source_text


@dataclass(frozen=True, slots=True)
class VideoSummary:
    """One video summary from a user's video page.

    ``aid`` stays nullable: the list response may omit it, and only a
    deliberate detail call may fill the gap.

    ``author`` is the uploader's own display name as this page reported it, and
    it is nullable too: the list response may omit it, and absence stays
    absence rather than being filled with ``str(mid)`` here.  The gateway must
    not fabricate a display label, because ``None`` is what lets the ingestor
    tell "upstream sent no name" from "upstream sent this name" — the ingestor
    owns the user record, so the fallback is its decision, not this DTO's.

    ``pic``/``desc``/``tid`` are the video's cover URL, description and
    category id as **this list response** carried them, and all three are
    nullable: upstream leaves the description empty on some videos and the
    fields are read from the response already received, so absence is a fact
    about the page rather than an error.  The category key is spelled
    ``typeid`` on this endpoint (the view endpoint calls it ``tid``) and this
    field keeps the stored column's name.  These are the three values the
    ``video_details`` row is built from, and a run that observed none of them
    writes no row — so blank text is normalized to ``None`` at the boundary,
    where "upstream sent nothing" can still be told from "upstream sent a
    space".
    """

    bvid: str
    aid: int | None
    title: str
    pubdate: int
    mid: int
    author: str | None = None
    pic: str | None = None
    desc: str | None = None
    tid: int | None = None
    # Participants verified against the video's detail response. ``mid``
    # remains the actual uploader, even on another participant's upload page.
    collaborator_mids: tuple[int, ...] = ()
    source_receipts: tuple[SourceFieldReceipt, ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        _text(self.bvid, "bvid")
        if self.aid is not None:
            _integer(self.aid, "aid", minimum=1)
        _text(self.title, "title")
        _integer(self.pubdate, "pubdate", minimum=0)
        _integer(self.mid, "mid", minimum=1)
        if self.author is not None:
            _text(self.author, "author")
        if self.pic is not None:
            _text(self.pic, "pic")
        if self.desc is not None:
            if not valid_source_text(self.desc, limit=65536, multiline=True):
                raise ValueError("desc contains invalid source text")
        if self.tid is not None:
            _integer(self.tid, "tid", minimum=1)
        if not isinstance(self.collaborator_mids, tuple):
            raise TypeError("collaborator_mids must be a tuple")
        for collaborator_mid in self.collaborator_mids:
            _integer(collaborator_mid, "collaborator_mid", minimum=1)
        if not isinstance(self.source_receipts, tuple) or any(
                not isinstance(receipt, SourceFieldReceipt) for receipt in self.source_receipts):
            raise TypeError("source_receipts must be typed source field receipts")

    @property
    def content_ref(self) -> ContentRef:
        """Video lookup reference; fetching parts supplies each real index."""

        return ContentRef("bilibili", self.bvid)


@dataclass(frozen=True, slots=True)
class VideoPart:
    """One part of one video, normalized to a zero-based index and ms."""

    bvid: str
    page_index: int
    cid: int
    title: str
    duration_ms: int
    source_receipt: SourceFieldReceipt | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _text(self.bvid, "bvid")
        _integer(self.page_index, "page_index", minimum=0)
        _integer(self.cid, "cid", minimum=1)
        _text(self.title, "title")
        _integer(self.duration_ms, "duration_ms", minimum=1)
        if self.source_receipt is not None and not isinstance(self.source_receipt, SourceFieldReceipt):
            raise TypeError("source_receipt must be a typed source field receipt")

    @property
    def content_ref(self) -> ContentRef:
        """Source-port identity without adding a field to the legacy DTO."""

        return ContentRef("bilibili", self.bvid, self.page_index)


@dataclass(frozen=True, slots=True)
class VideoTag:
    """One tag upstream reports for one video, normalized to three fields.

    Like ``SubtitleTrack``, this is one entry of an inventory whose owner is
    the *call's* argument: ``get_video_tags(bvid)`` already names the video, so
    the DTO does not repeat it.  The identity is ``(bvid, tag_id)`` and the
    store holds both; here the ``bvid`` is the caller's, carried to the
    repository by the ingestor rather than through every entry.

    ``tag_name`` is a display label upstream may rename while the id stays the
    same, and ``tag_type`` is upstream's own classification (``old_channel``
    and the like).  Only these three are carried: upstream's response also
    holds a ``music_id`` and a ``jump_url``, and neither is a fact this archive
    stores — a URL in particular is the kind of value the store's no-URL rule
    keeps out, so it never reaches a DTO in the first place.
    """

    tag_id: int
    tag_name: str
    tag_type: str

    def __post_init__(self) -> None:
        _integer(self.tag_id, "tag_id", minimum=1)
        _text(self.tag_name, "tag_name")
        _text(self.tag_type, "tag_type")


@dataclass(frozen=True, slots=True)
class VideoMetadataRead:
    summary: VideoSummary
    fields: tuple[MetadataFieldObservation, ...]
    parts: tuple[VideoPart, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.summary, VideoSummary):
            raise TypeError("metadata read requires a video summary")
        if not isinstance(self.fields, tuple) or any(not isinstance(field, MetadataFieldObservation) for field in self.fields):
            raise TypeError("metadata read requires typed field observations")
        if len({field.field for field in self.fields}) != len(self.fields):
            raise ValueError("metadata fields must not repeat")
        if self.parts is not None and (not isinstance(self.parts, tuple) or any(
                not isinstance(part, VideoPart) or part.bvid != self.summary.bvid for part in self.parts)):
            raise ValueError("metadata parts must match their source video")


@dataclass(frozen=True, slots=True)
class UserVideoPage:
    """One bounded page of one user's video summaries."""

    mid: int
    page_number: int
    videos: tuple[VideoSummary, ...]
    observed_total: int | None

    def __post_init__(self) -> None:
        _integer(self.mid, "mid", minimum=1)
        _integer(self.page_number, "page_number", minimum=1)
        if not isinstance(self.videos, tuple):
            raise TypeError("videos must be a tuple of VideoSummary")
        for video in self.videos:
            if not isinstance(video, VideoSummary):
                raise TypeError("videos must be a tuple of VideoSummary")
        if self.observed_total is not None:
            _integer(self.observed_total, "observed_total", minimum=0)


@dataclass(frozen=True, slots=True)
class SubtitleTrack:
    """One subtitle inventory entry of one part, as upstream listed it.

    ``language`` is the upstream ``lan`` code and ``label`` its human-readable
    ``lan_doc``; both are printable as-is.  ``is_ai`` separates
    machine-generated captions from uploader/human ones.  ``track_id`` is the
    track's upstream identity when it carries one — never a URL: a signed
    ``subtitle_url`` is process-local for the duration of one call and cannot
    reach this DTO.
    """

    language: str
    label: str
    is_ai: bool
    track_id: str | None

    def __post_init__(self) -> None:
        _text(self.language, "language")
        # The service derives the language family from the primary subtag, so a
        # vocabulary it could not rank (``-zh``) is rejected here.
        if not self.language.split("-", 1)[0].strip():
            raise ValueError("language must carry a non-empty primary subtag")
        _text(self.label, "label")
        if not isinstance(self.is_ai, bool):
            raise TypeError("is_ai must be a boolean")
        if self.track_id is not None:
            _text(self.track_id, "track_id")


@dataclass(frozen=True, slots=True)
class SubtitleSegment:
    """One normalized caption row in milliseconds.

    ``end_ms > start_ms >= 0`` with ``text`` non-empty after stripping is the
    invariant this DTO enforces on what a call returns.  It is not a filter:
    the adapter drops a row carrying nothing usable before constructing it.
    ``text`` is validated as caption body text (:func:`_caption_text`), so a
    cue that legitimately spans two lines stays one row — the store keeps
    caption text verbatim, control characters included.
    """

    start_ms: int
    end_ms: int
    text: str

    def __post_init__(self) -> None:
        _integer(self.start_ms, "start_ms", minimum=0)
        _integer(self.end_ms, "end_ms", minimum=1)
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        _caption_text(self.text, "text")


@dataclass(frozen=True, slots=True)
class SubtitleBodyRead:
    """A fully read caption document, distinct from inaccessible or damaged data.

    Empty evidence requires a valid body array and valid timing for every row.
    Transport, authentication, missing tracks and malformed timelines raise a
    bounded gateway error; they can never be represented by this empty result.
    """

    segments: tuple[SubtitleSegment, ...]
    row_count: int
    empty_kind: str | None = None

    def __post_init__(self) -> None:
        _integer(self.row_count, "row_count", minimum=0)
        if not isinstance(self.segments, tuple) or any(
            not isinstance(segment, SubtitleSegment) for segment in self.segments
        ):
            raise TypeError("segments must be normalized caption rows")
        if self.row_count < len(self.segments):
            raise ValueError("row_count cannot be below the normalized count")
        if self.segments:
            if self.empty_kind is not None:
                raise ValueError("a nonempty caption body cannot attest emptiness")
        elif self.empty_kind not in {"empty_body", "empty_text"}:
            raise ValueError("an empty body requires an explicit verified reason")
        elif (self.empty_kind == "empty_body") != (self.row_count == 0):
            raise ValueError("empty reason must agree with document row count")


@dataclass(frozen=True, slots=True)
class TagRead:
    """One tag observation with its own error, safe for concurrent consumers."""

    tags: tuple[VideoTag, ...] | None
    error_code: str | None = None
    source_receipt: SourceFieldReceipt | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.tags is None:
            if not self.error_code:
                raise ValueError("an unavailable tag observation requires a code")
            validate_error_code(self.error_code)
        elif not isinstance(self.tags, tuple) or any(not isinstance(tag, VideoTag) for tag in self.tags):
            raise TypeError("tags must be normalized video tags")
        elif self.error_code is not None:
            raise ValueError("a successful tag read cannot carry a failure code")
        if self.source_receipt is not None and (self.tags is None or not isinstance(self.source_receipt, SourceFieldReceipt)):
            raise ValueError("only successful tag reads may carry a typed receipt")


class BilibiliGateway(Protocol):
    """Application-owned gateway protocol for the pinned package adapter."""

    # 30 is the page size the user-video endpoint accepts: the pinned package
    # documents ``ps`` as ``const int: 30`` and upstream answers ``ps=100``
    # with its bounded ``-400``/HTTP 412 rejection.
    async def get_user_video_page(
        self, mid: int, page_number: int, page_size: int = 30
    ) -> UserVideoPage: ...

    async def get_video_parts(
        self, bvid: str, video_title_fallback: str = ""
    ) -> tuple[VideoPart, ...]: ...

    async def get_completed_video_summary(
        self, summary: VideoSummary
    ) -> VideoSummary: ...

    async def get_video_metadata(self, bvid: str) -> VideoMetadataRead: ...

    # The tag set is a property of the VIDEO, not of a part: it is fetched
    # per distinct video, with bounded reuse of recent observations in a run.
    # A new run refreshes relisted videos. A tag fetch is
    # retry-free and degrades rather than raising — risk control and transport
    # failures on this call may not fail the collection run — but the
    # degradation is a *third state*, not an empty set: ``None`` means "this
    # call could not read the tags this time" and ``()`` means "read it, and
    # this video carries none".  Callers must not turn the first into the
    # second: the ingestor omits such a bvid from a page's tag sets, so
    # nothing is written for it and rows a previous run stored survive
    # (compass D16).  A key present with an empty iterable is the observation
    # that clears them.
    async def get_video_tags(self, bvid: str) -> tuple[VideoTag, ...] | None: ...

    async def read_video_tags(self, bvid: str) -> TagRead: ...

    # A subtitle inventory is an observation, not a promise: an empty tuple
    # means nothing usable was visible with the credentials in effect, and it
    # is a legitimate result rather than a ``not_found`` failure.
    async def get_subtitle_tracks(
        self, bvid: str, cid: int
    ) -> tuple[SubtitleTrack, ...]: ...

    # A configured cookie is not proof of a logged-in request.  Before an
    # empty credentialed inventory is recorded as absence, validate the login
    # in effect; rejection and an unavailable check raise bounded failures.
    async def validate_subtitle_credentials(self) -> None: ...

    # The body fetch is the opposite signal, because an empty success would
    # claim a subtitle it does not have: nothing usable raises
    # ``GatewayNotFound`` and the return is never an empty tuple.
    async def fetch_subtitle_segments(
        self, track: SubtitleTrack, bvid: str, cid: int
    ) -> tuple[SubtitleSegment, ...]: ...

    async def read_subtitle_body(
        self, track: SubtitleTrack, bvid: str, cid: int
    ) -> SubtitleBodyRead: ...

    def get_package_version(self) -> str: ...


@dataclass(frozen=True, slots=True)
class GatewayDiagnostic:
    """Allowlisted upstream facts for operator output, never raw exception text."""

    operation: str
    http_status: int | None = None
    api_code: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.operation not in {
            "get_user_video_page", "get_video_parts", "get_completed_video_summary",
            "get_video_tags", "get_subtitle_tracks", "validate_subtitle_credentials",
            "fetch_subtitle_segments", "validate_metadata_credentials",
            "user_archive_profile", "user_archive_relation", "user_archive_live",
            "user_archive_notice", "user_archive_videos", "user_archive_dynamics",
            "user_archive_articles", "user_archive_article_lists", "user_archive_channels",
            "user_archive_season", "user_archive_series", "user_archive_article",
        }:
            raise ValueError("unknown gateway diagnostic operation")
        if self.http_status is not None:
            _integer(self.http_status, "http_status", minimum=100)
            if self.http_status > 599:
                raise ValueError("invalid HTTP status")
        if self.api_code is not None:
            if type(self.api_code) is not int or not -(2**31) <= self.api_code < 2**31:
                raise ValueError("invalid API code")
        if self.reason not in {None, "wbi_retry_exhausted", "response_error", "transport_error"}:
            raise ValueError("unknown gateway diagnostic reason")

    def format(self) -> str:
        fields = [f"operation={self.operation}"]
        for name in ("http_status", "api_code", "reason"):
            value = getattr(self, name)
            if value is not None:
                fields.append(f"{name}={value}")
        return " ".join(fields)


class GatewayError(Exception):
    """Base of the bounded gateway failure taxonomy.

    Carries the bounded scalar ``code`` that may be persisted in page and run
    outcome rows.  Upstream exception text, URLs, cookies, and raw response
    content never appear in the message.
    """

    default_code: ClassVar[str] = "gateway_error"

    def __init__(self, code: str | None = None, detail: str = "", *,
                 diagnostic: GatewayDiagnostic | None = None) -> None:
        self.code = validate_error_code(
            code if code is not None else self.default_code
        )
        self.detail = detail
        self.diagnostic = diagnostic
        message = f"{type(self).__name__}({self.code})"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class GatewayRateLimited(GatewayError):
    """Upstream rate control stopped the page (risk-control signal)."""

    default_code = "rate_limited"


class GatewayNotFound(GatewayError):
    """The requested user or video does not exist (or is gone)."""

    default_code = "not_found"


class GatewayResponseError(GatewayError):
    """The upstream API answered with an error outside shape validation."""

    default_code = "response_error"


class GatewayAuthenticationError(GatewayError):
    """The configured credential is not authenticated by upstream."""

    default_code = "auth_error"


class GatewayTransportError(GatewayError):
    """The request never produced a usable response (network/transport)."""

    default_code = "transport_error"


class GatewayShapeError(GatewayError):
    """The upstream response could not be normalized into the DTO contract."""

    default_code = "shape_error"


__all__ = [
    "BilibiliGateway",
    "GatewayError",
    "GatewayDiagnostic",
    "GatewayAuthenticationError",
    "GatewayNotFound",
    "GatewayRateLimited",
    "GatewayResponseError",
    "GatewayShapeError",
    "GatewayTransportError",
    "SubtitleSegment",
    "SubtitleBodyRead",
    "TagRead",
    "SubtitleTrack",
    "UserVideoPage",
    "VideoPart",
    "VideoMetadataRead",
    "VideoSummary",
    "VideoTag",
]
