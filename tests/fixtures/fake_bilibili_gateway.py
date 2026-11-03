"""The single deterministic offline fake for the Bilibili package seam.

Every package-seam test scripts these fakes instead of touching the pinned
``bilibili-api-python`` distribution or the network:

- ``FakeGateway`` is the plain ``BilibiliGateway`` protocol double used by
  the ingestor and CLI tests: scripted pages, parts, summary completions and
  per-part subtitle listings/bodies with recorded fetch calls, plus the
  credential the composition root handed the double; unexpected fetches fail
  loudly instead of returning script-free data.
- ``FakeUpstreamScript`` / ``build_fake_package`` / ``bilibili_api_seam``
  install the fake ``bilibili_api`` package on ``sys.modules`` for the real
  adapter tests.  The fake mirrors only the documented import surface the
  adapter may use (``Credential``, ``request_settings.set_proxy`` /
  ``get_proxy``, the ``user`` and ``video`` endpoint descriptions and the
  ``access_id`` route, the WBI-signed ``utils.network.Api`` every documented
  request is issued through, ``video.Video.get_info`` / ``get_pages``, and the
  exceptions taxonomy) and exposes no subtitle, playback, audio, or download
  *method* on those classes, so a silent switch to another package API fails
  loudly instead of silently succeeding.  ``applied_proxies`` records every
  proxy the adapter hands to the package settings, so its apply-once behavior
  is asserted without a network call.
- ``FakeApiRequest`` with ``script.api_requests`` records every request the
  adapter issues through the package's ``Api``, in issue order: the transport
  flags it took from the package endpoint description (device-fingerprint
  ``dm`` overridden), the ``raw`` request argument, whether the credential it
  carried held a SESSDATA value, and the exact parameters it handed over, so
  the risk-control-relevant and credential-boundary call shapes are assertable
  without a network call.  ``script.access_id_calls`` records the separate
  ``access_id`` token route (kept out of ``calls``: it is a memoized,
  best-effort token fetch, not a metadata call).
- ``script.tags_response`` / ``script.tags_error`` script the tag call
  (``video.API["info"]["tags"]``, whose unwrapped payload is the tag array),
  and ``script.tag_calls`` records every ``bvid`` whose tags were requested —
  in issue order and independently of the answer — so the once-per-video rule
  is observable even for a video that carries no tags.
- ``script.player_response`` / ``script.player_error`` script the player call
  (``video.API["info"]["get_player_info"]``, whose unwrapped payload carries
  the subtitle inventory) and ``script.subtitle_bodies`` scripts the signed
  subtitle documents by URL: a plain document, a ``BaseException`` to raise,
  or a callable receiving the fetched URL.  An unscripted document URL fails
  loudly, so an adapter that fetches somewhere it did not mean to is caught.
- ``script_parts_by_bvid`` scripts one parts payload per requested ``bvid``
  for seam tests that collect a page holding several distinct videos.
- ``DOCUMENTED_METADATA_CALLS`` with ``assert_only_documented_metadata_calls``
  pins the upstream call names the gateway may issue.
- ``NO_LEAK_MARKERS`` with ``assert_leaks_no_markers`` holds the
  realistic-looking secret and raw-payload sentinel texts (a SESSDATA
  value, a signed playback URL, the signed subtitle-document URL in both the
  absolute and the protocol-relative form upstream answers, a raw JSON
  response body, and raw upstream exception text) plus the scanner behind the
  no-secret assertions, and ``persisted_row_text`` renders persisted rows for
  those assertions.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
from functools import wraps
import sys
import types
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bili_asr.sources.models import (
        UserVideoPage, VideoPart, VideoSummary, VideoTag, SubtitleTrack, SubtitleSegment,
    )

import pytest

_REAL_ASYNC_SLEEP = asyncio.sleep

MID = 23191782
BVID = "BV1AbCdEfGhJ"
PUBDATE = 1725859200

#: Package version the fake gateway reports for run metadata.
FAKE_PACKAGE_VERSION = "17.4.2"

#: Adapter module the seam fixture reloads against the installed fake.
GATEWAY_ADAPTER_MODULE = "bili_asr.sources.bilibili_api_gateway"

#: The package's own endpoint description for the user-video page call
#: (``bilibili_api.user.API["info"]["video"]``), mirroring the pinned
#: distribution literally — including ``dm: True``.  The adapter must take
#: ``url``/``method``/``verify``/``wbi`` from it and override ``dm``, so a
#: package-side change to any of those fields stays visible here.  The mirror
#: itself is not taken on trust: the offline parity test in
#: ``tests/test_bilibili_api_gateway.py`` compares it against the installed
#: pinned distribution it claims to mirror.
FAKE_USER_VIDEO_PAGE_ENDPOINT = {
    "url": "https://api.bilibili.com/x/space/wbi/arc/search",
    "method": "GET",
    "verify": False,
    "wbi": True,
    "dm": True,
    "params": {
        "mid": "int: uid",
        "ps": "const int: 30",
        "tid": "int: 分区 ID，0 表示全部",
        "pn": "int: 页码",
        "keyword": "str: 关键词，可为空",
        "w_webid": "str: w_webid",
    },
    "comment": "搜索用户视频",
}

#: The endpoint-description fields the adapter reads from the package's own
#: description (``dm`` is the one field it overrides).  The offline mirror
#: parity test compares exactly these against the installed distribution, so
#: a pin that renames, drops, or flips one of them fails there instead of
#: surfacing only live.
MIRRORED_ENDPOINT_FIELDS = ("url", "method", "verify", "wbi")

#: The package's own endpoint description for the player call
#: (``bilibili_api.video.API["info"]["get_player_info"]``), mirroring the
#: pinned distribution literally — including ``dm: True``.  The adapter must
#: take ``url``/``method``/``wbi`` from it and override ``dm``/``verify``, so a
#: package-side change to any of those fields stays visible here.  Unlike the
#: user-video page description, this endpoint declares its query fields under
#: ``data``; those are field documentation the adapter must not forward
#: verbatim, and the player call's parameters are none of the values below.
#: The mirror itself is not taken on trust: the offline parity test in
#: ``tests/test_bilibili_api_gateway.py`` compares it against the installed
#: pinned distribution it claims to mirror, field for field.
FAKE_PLAYER_ENDPOINT = {
    "url": "https://api.bilibili.com/x/player/wbi/v2",
    "method": "GET",
    "verify": True,
    "wbi": True,
    "dm": True,
    "data": {
        "aid": "int: av 号。与 bvid 任选其一",
        "cid": "int: 分 P id",
        "ep_id": "int: 番剧分集 id",
        "isGaiaAvoided": "bool: false",
        "web_location": "int: 1315873",
    },
    "comment": "获取视频上一次播放的记录，字幕和地区信息。需要 分集的 cid, 返回数据中含有json字幕的链接",
}

#: The package's own endpoint description for the video-tag call
#: (``bilibili_api.video.API["info"]["tags"]``), mirroring the pinned
#: distribution literally.  This is the one documented metadata endpoint whose
#: description declares neither ``verify`` nor ``wbi``, which is the structural
#: reason the call needs no credential: the adapter must read ``verify: False``
#: from here rather than assert it, so a pin bump that flips it fails the
#: offline parity test instead of silently sending a credential-bound request.
#: Its ``params`` are field documentation (``aid``/``bvid``), not a parameter
#: mapping to forward verbatim — the call sends ``bvid`` alone.
FAKE_TAG_ENDPOINT = {
    "url": "https://api.bilibili.com/x/web-interface/view/detail/tag",
    "method": "GET",
    "verify": False,
    "params": {
        "aid": "int: av 号",
        "bvid": "string: BV 号",
    },
    "comment": "视频标签信息",
}

#: The exact upstream call names the gateway adapter may issue.  The page call
#: is recorded as ``space.arc.search`` because the adapter issues that request
#: itself through the package's ``Api``: the package's ``User.get_videos``
#: delegate cannot pass upstream risk control (it injects device-fingerprint
#: ``dm`` parameters and scrapes ``w_webid`` from a page that no longer
#: server-renders it).  The two subtitle-acquisition routes are the plan's own
#: authorized surface: the player track listing and the signed subtitle
#: document.  ``video.tags`` is the tag call, recorded through the same
#: ``video`` endpoint-description surface ``video.get_info`` records the detail
#: route with.  The set is exact — nothing else may appear.
DOCUMENTED_METADATA_CALLS = (
    "space.arc.search",
    "video.get_info",
    "video.get_pages",
    "video.tags",
    "player.track_list",
    "subtitle.body",
    "credential.nav",
)

#: Realistic-looking SESSDATA value that must never leave the process.
SESSDATA_BOUNDARY_VALUE = "SESSDATA-VALUE-THAT-MUST-NOT-LEAK"

#: Realistic-looking signed playback URL that must never be persisted.
SIGNED_URL_MARKER = (
    "https://upos.example.com/upyun/ssl/part.m4s?sign=SIGNED-URL-THAT-MUST-NOT-LEAK"
)

#: Realistic-looking signed subtitle-document URL that must never leave the
#: process: it is what a leaked ``subtitle_url`` would look like, and it is
#: what the seam scripts every track with — so the shipped no-leak scanner
#: catches a leaked subtitle URL the same way it catches a playback URL.
SIGNED_SUBTITLE_URL_MARKER = (
    "https://aisubtitle.example.com/bfs/subtitle/part.json"
    "?sign=SIGNED-SUBTITLE-URL-THAT-MUST-NOT-LEAK"
)

#: The same signed document URL in the protocol-relative form upstream also
#: returns, which the adapter must normalize to ``https:`` before fetching.
PROTOCOL_RELATIVE_SUBTITLE_URL = SIGNED_SUBTITLE_URL_MARKER.removeprefix("https:")

#: Realistic-looking raw JSON response body that must never be persisted.
RAW_JSON_BODY_MARKER = '{"code":-412,"message":"RAW-JSON-BODY-THAT-MUST-NOT-LEAK"}'

#: Realistic-looking raw upstream exception text that must never be persisted.
RAW_UPSTREAM_EXCEPTION_MARKER = "RAW-UPSTREAM-EXCEPTION-THAT-MUST-NOT-LEAK"

#: All sentinels the no-secret assertions scan persisted surfaces for.  The
#: subtitle-document URL is held in both forms upstream answers it in — the
#: absolute one and the protocol-relative one — so a URL that skipped the
#: adapter's ``https:`` normalization is still caught by the scanner.
NO_LEAK_MARKERS = (
    SESSDATA_BOUNDARY_VALUE,
    SIGNED_URL_MARKER,
    SIGNED_SUBTITLE_URL_MARKER,
    PROTOCOL_RELATIVE_SUBTITLE_URL,
    RAW_JSON_BODY_MARKER,
    RAW_UPSTREAM_EXCEPTION_MARKER,
)

#: One upstream failure message carrying every sentinel at once.
UPSTREAM_ERROR_TEXT = " ".join(
    (
        "RISK-CHALLENGE-PAYLOAD",
        SIGNED_URL_MARKER,
        RAW_JSON_BODY_MARKER,
        RAW_UPSTREAM_EXCEPTION_MARKER,
    )
)


class FakeApiException(Exception):
    """Mirror of ``bilibili_api.exceptions.ApiException``."""

    def __init__(self, msg: str = "出现了错误，但是未说明具体原因。") -> None:
        super().__init__(msg)
        self.msg = msg


class FakeNetworkException(FakeApiException):
    """Mirror of ``NetworkException(status, msg)`` carrying the HTTP status."""

    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.status = status


class FakeResponseCodeException(FakeApiException):
    """Mirror of ``ResponseCodeException(code, msg, raw)`` carrying the code."""

    def __init__(self, code: int, msg: str, raw: object = None) -> None:
        super().__init__(msg)
        self.code = code


class FakeResponseException(FakeApiException):
    """Mirror of ``ResponseException(msg)``."""

    def __init__(self, msg: str) -> None:
        super().__init__(msg)


class FakeWbiRetryTimesExceedException(FakeApiException):
    """Mirror of ``WbiRetryTimesExceedException()`` (WBI retry budget gone)."""

    def __init__(self) -> None:
        super().__init__("WBI 重试达到最大次数")


def _device_fingerprint_params() -> dict:
    """Mirror of the package's ``_enc_dm`` injection: the same parameter names.

    A request built with ``dm`` enabled carries these parameters upstream, so
    the fake adds them too: the "no device-fingerprint parameters are sent"
    assertion then detects the real risk-control defect instead of merely
    restating the recorded ``dm`` flag.
    """

    return {
        "dm_img_list": "[]",
        "dm_img_str": "AB",
        "dm_cover_img_str": "AB",
        "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
    }


@dataclasses.dataclass
class FakeApiRequest:
    """One recorded package-``Api`` request: the call shape that was issued.

    ``params`` is the exact mapping the adapter handed to ``update_params``,
    captured when the request was issued, with the package's
    device-fingerprint injection mirrored when ``dm`` is on.  ``raw`` is the
    ``Api.request(raw=...)`` argument the call carried (in the pinned package
    the request argument decides whether the ``data``/``result`` envelope is
    unwrapped).  ``has_sessdata`` records whether the credential the call
    carried held a SESSDATA value, which is how the empty-credential document
    fetch is assertable without ever comparing credential values.
    """

    url: str
    method: str
    verify: bool
    wbi: bool
    dm: bool
    params: dict
    raw: bool = False
    has_sessdata: bool = False


@dataclasses.dataclass
class FakeUpstreamScript:
    """Scripted upstream behavior; records every call the gateway makes.

    A scripted page response may be a plain value or a callable receiving the
    documented page parameters (``pn``, ``ps``) so per-page behavior can be
    scripted for multi-page runs.  The scripted ``parts_response`` may
    likewise be a plain value or a callable receiving the requested
    ``bvid``, so each video's parts can be scripted independently (see
    :func:`script_parts_by_bvid`).

    ``videos_response`` / ``videos_error`` script the user-video page request
    the adapter issues through the package ``Api``; ``player_response`` /
    ``player_error`` script the player request the same way (a plain value or
    a callable receiving the requested ``bvid``/``cid``), whose unwrapped
    payload carries the subtitle inventory; ``tags_response`` / ``tags_error``
    script the tag request (a plain array, a ``BaseException`` to raise, or a
    callable receiving the requested ``bvid``), whose unwrapped payload is the
    array of tag objects; an unscripted ``tags_response`` answers the empty
    array, because that is what upstream returns for a video with no tags and
    the call is best-effort by design.  ``tag_calls`` records every ``bvid`` it
    was asked for, in issue order, independently of what was answered — that
    list is what makes a per-part fetch observable; ``subtitle_bodies`` scripts the
    signed subtitle documents by URL (a plain document, a ``BaseException`` to
    raise, or a callable receiving the fetched URL).  ``access_id`` /
    ``access_id_error`` script the separate ``access_id`` token route
    (``None`` by default, which is what the route currently yields in
    production).  ``user_video_page_endpoint`` / ``player_endpoint`` /
    ``tag_endpoint`` are the
    package-side endpoint descriptions the adapter must read its transport
    fields from; a test may rewrite any of them before the adapter module is
    (re-)imported.
    """

    videos_response: object = None
    videos_error: BaseException | None = None
    parts_response: object = None
    parts_error: BaseException | None = None
    info_response: object = None
    info_error: BaseException | None = None
    tags_response: object = None
    tags_error: BaseException | None = None
    player_response: object = None
    player_error: BaseException | None = None
    nav_response: object = dataclasses.field(default_factory=lambda: {"isLogin": True})
    nav_error: BaseException | None = None
    subtitle_bodies: dict[str, object] = dataclasses.field(default_factory=dict)
    access_id: str | None = None
    access_id_error: BaseException | None = None
    user_video_page_endpoint: dict = dataclasses.field(
        default_factory=lambda: dict(FAKE_USER_VIDEO_PAGE_ENDPOINT)
    )
    player_endpoint: dict = dataclasses.field(
        default_factory=lambda: dict(FAKE_PLAYER_ENDPOINT)
    )
    tag_endpoint: dict = dataclasses.field(
        default_factory=lambda: dict(FAKE_TAG_ENDPOINT)
    )
    calls: list[str] = dataclasses.field(default_factory=list)
    pacing_delays: list[float] = dataclasses.field(default_factory=list)
    tag_calls: list[str] = dataclasses.field(default_factory=list)
    access_id_calls: list[str] = dataclasses.field(default_factory=list)
    api_requests: list[FakeApiRequest] = dataclasses.field(default_factory=list)
    applied_proxies: list[str] = dataclasses.field(default_factory=list)


class FakeGateway:
    """Scripted ``BilibiliGateway`` protocol double for ingestor and CLI tests.

    Unexpected fetches fail loudly instead of returning script-free data;
    a scripted ``BaseException`` value is raised as-is.

    The two subtitle calls are scripted per part, by ``cid``:
    ``script_subtitle_tracks`` scripts what ``get_subtitle_tracks`` answers
    (an inventory, or an empty tuple for a part nothing is visible for, or a
    ``GatewayError`` to raise) and ``script_subtitle_segments`` scripts what
    ``fetch_subtitle_segments`` answers (a body, or a ``GatewayError`` to
    raise).  ``listing_cids`` and ``body_cids`` record the parts whose listing
    and whose body were requested, in issue order, so the candidate order, the
    selected part, and "the body was never fetched" are assertable.  The
    ``sessdata`` attribute holds the credential the composition root handed the
    double — ``None`` until a test installs it that way — so the credential
    boundary is assertable without ever comparing a value in output.

    ``script_tags`` scripts one video's tag inventory and ``tag_calls`` records
    every ``bvid`` whose tags were requested, in issue order.  A video whose
    tags were not scripted answers the empty inventory rather than failing:
    upstream really does answer an empty array for a video with no tags, the
    tag call is best-effort by design, and making every pre-tag test script it
    would be an unrelated diff.  ``tag_calls`` records the request rather than
    the answer, so a per-part implementation is caught by the call count even
    when a test scripts no tags at all.
    """

    def __init__(self, package_version: str = FAKE_PACKAGE_VERSION) -> None:
        self.package_version = package_version
        self.sessdata: str | None = None
        self.page_calls: list[tuple[int, int, int]] = []
        self.parts_calls: list[str] = []
        self.completion_calls: list[str] = []
        self.tag_calls: list[str] = []
        self.listing_cids: list[int] = []
        self.body_cids: list[int] = []
        self.credential_checks = 0
        self.subtitle_credential_error: BaseException | None = None
        self._pages: dict[int, object] = {}
        self._parts: dict[str, object] = {}
        self._completions: dict[str, object] = {}
        self._tags: dict[str, object] = {}
        self._subtitle_tracks: dict[int, object] = {}
        self._subtitle_segments: dict[int, object] = {}

    def script_page(self, page_number: int, page: object) -> None:
        self._pages[page_number] = page

    def script_parts(self, bvid: str, parts: object) -> None:
        self._parts[bvid] = parts

    def script_completion(self, bvid: str, completed: object) -> None:
        self._completions[bvid] = completed

    def script_tags(self, bvid: str, tags: object) -> None:
        """Script what the tag listing answers for one video, by its ``bvid``.

        ``None`` scripts the degraded answer — "could not read this time" —
        which is a different state from ``()`` ("read it, carries none"); see
        :meth:`get_video_tags`.
        """

        self._tags[bvid] = tags

    def script_subtitle_tracks(self, cid: int, tracks: object) -> None:
        """Script what the track listing answers for one part, by its ``cid``."""

        self._subtitle_tracks[cid] = tracks

    def script_subtitle_segments(self, cid: int, segments: object) -> None:
        """Script what the body fetch answers for one part, by its ``cid``."""

        self._subtitle_segments[cid] = segments

    async def get_user_video_page(
        self, mid: int, page_number: int, page_size: int = 30
    ) -> UserVideoPage:
        self.page_calls.append((mid, page_number, page_size))
        return self._scripted(self._pages, page_number, "user-video-page")

    async def get_video_parts(
        self, bvid: str, video_title_fallback: str = ""
    ) -> tuple[VideoPart, ...]:
        self.parts_calls.append(bvid)
        return self._scripted(self._parts, bvid, "video-parts")

    async def get_completed_video_summary(self, summary: VideoSummary) -> VideoSummary:
        self.completion_calls.append(summary.bvid)
        return self._scripted(self._completions, summary.bvid, "completed-summary")

    async def get_video_tags(self, bvid: str) -> tuple[VideoTag, ...] | None:
        """Record the request and answer the scripted inventory.

        An unscripted ``bvid`` answers the empty inventory rather than failing
        loudly, unlike every other fetch here: the tag call is a best-effort
        addition whose production answer for an untagged video is exactly that
        empty array, so defaulting it keeps the pre-tag tests meaningful
        instead of rewriting each one.  The request is recorded either way.

        ``script_tags(bvid, None)`` is how a case scripts the *third* state
        compass D16 added: ``None`` means the call could not read the tags this
        time, while ``()`` means it did read and the video carries none.  The
        two drive opposite behavior in the ingestor — the absent key preserves
        a previous run's rows, the present empty key clears them — so a case
        that needs the preserving arm must script ``None`` explicitly rather
        than relying on the unscripted default, which is the clearing one.

        A *scripted* ``BaseException`` is raised as-is, like every sibling
        fetch, so a test can drive the ingestor with a gateway that fails.  The
        production degradation lives in the adapter, not here: this double is
        the protocol boundary, and a raising tag fetch through it is precisely
        the case the real adapter's guard exists to prevent.
        """

        self.tag_calls.append(bvid)
        if bvid not in self._tags:
            return ()
        return self._scripted(self._tags, bvid, "video-tags")

    async def get_subtitle_tracks(
        self, bvid: str, cid: int
    ) -> tuple[SubtitleTrack, ...]:
        self.listing_cids.append(cid)
        return self._scripted(
            self._subtitle_tracks, cid, "subtitle-track listing"
        )

    async def validate_subtitle_credentials(self) -> None:
        """Model a valid login unless the test scripts its bounded failure."""

        self.credential_checks += 1
        if self.subtitle_credential_error is not None:
            raise self.subtitle_credential_error

    async def fetch_subtitle_segments(
        self, track: SubtitleTrack, bvid: str, cid: int
    ) -> tuple[SubtitleSegment, ...]:
        self.body_cids.append(cid)
        return self._scripted(
            self._subtitle_segments, cid, "subtitle-segment body"
        )

    def get_package_version(self) -> str:
        return self.package_version

    @staticmethod
    def _scripted(script: dict, key: object, what: str) -> object:
        if key not in script:
            raise AssertionError(f"unexpected {what} fetch: {key!r}")
        value = script[key]
        if isinstance(value, BaseException):
            raise value
        return value


def build_fake_package(script: FakeUpstreamScript) -> dict[str, types.ModuleType]:
    """Build the fake ``bilibili_api`` package with the documented surface."""

    package = types.ModuleType("bilibili_api")

    class Credential:
        def __init__(self, sessdata: str | None = None) -> None:
            self.sessdata = sessdata

    package.Credential = Credential

    request_settings_mod = types.ModuleType("bilibili_api.request_settings")

    def set_proxy(proxy: str) -> None:
        """Mirror of ``request_settings.set_proxy``; records the applied value.

        The pinned ``RequestSettings.set_proxy(self, proxy: str)`` has no
        default, so neither does this double: a no-argument call must fail
        offline exactly where it would fail live (``TypeError``) instead of
        passing here and breaking against the real package.
        """

        script.applied_proxies.append(proxy)

    def get_proxy() -> str:
        """Mirror of ``request_settings.get_proxy`` (``""`` before any set)."""

        return script.applied_proxies[-1] if script.applied_proxies else ""

    request_settings_mod.set_proxy = set_proxy
    request_settings_mod.get_proxy = get_proxy

    exceptions_mod = types.ModuleType("bilibili_api.exceptions")
    exceptions_mod.ApiException = FakeApiException
    exceptions_mod.NetworkException = FakeNetworkException
    exceptions_mod.ResponseCodeException = FakeResponseCodeException
    exceptions_mod.ResponseException = FakeResponseException
    exceptions_mod.WbiRetryTimesExceedException = FakeWbiRetryTimesExceedException

    user_mod = types.ModuleType("bilibili_api.user")

    class VideoOrder(Enum):
        """Mirror of ``user.VideoOrder`` (only the default order is exposed)."""

        PUBDATE = "pubdate"

    class User:
        """Mirror of ``user.User(uid, credential)`` with only the access_id route.

        The user-video page data no longer flows through this class: the
        adapter issues the endpoint description's request itself through the
        package ``Api``.
        """

        def __init__(self, uid: int, credential: object = None) -> None:
            self.uid = uid
            self.credential = credential

        async def get_access_id(self) -> str | None:
            script.access_id_calls.append(f"user.get_access_id(uid={self.uid})")
            if script.access_id_error is not None:
                raise script.access_id_error
            return script.access_id

    user_mod.API = {"info": {"video": script.user_video_page_endpoint}}
    user_mod.User = User
    user_mod.VideoOrder = VideoOrder

    network_mod = types.ModuleType("bilibili_api.utils.network")
    network_mod.session_pool = {}
    network_mod.lazy_settings = {}

    class Api:
        """Mirror of ``utils.network.Api`` for the documented requests.

        The package's ``Api`` is built from an endpoint description whose
        ``url``/``method``/``verify``/``wbi`` the adapter must state and whose
        ``dm`` it must override, so those fields are required here and are
        recorded with the parameters of the issued request.  Four routes are
        mirrored: the user-video page call, the tag call and the player call,
        all answered with the scripted unwrapped payload exactly as the pin's
        ``raw=False`` result delivers it, and the signed subtitle-document
        fetch, which the pin answers only to ``request(raw=True)`` because a
        subtitle document has no ``code``/``data`` envelope to unwrap.  Only
        the local request shaping under test is mirrored (the ``dm`` parameter
        injection and the ``raw`` request argument); signing, cookies, retries,
        and byte transport are not, because the adapter may not depend on them.
        """

        def __init__(
            self,
            url: str,
            method: str,
            verify: bool,
            wbi: bool,
            dm: bool,
            credential: object = None,
        ) -> None:
            self.url = url
            self.method = method
            self.verify = verify
            self.wbi = wbi
            self.dm = dm
            self.credential = credential
            self.params: dict = {}

        def update_params(self, **kwargs: object) -> "Api":
            """Mirror of ``Api.update_params`` (replaces the parameters)."""

            self.params = dict(kwargs)
            return self

        @property
        async def result(self) -> object:
            """Mirror of ``Api.result``: the pin's ``request()`` with no args."""

            return await self.request()

        async def request(self, raw: bool = False, byte: bool = False) -> object:
            """Mirror of ``Api.request``: record the call, then answer or fail.

            ``raw`` selects which document is answered with: unless it is set
            the pin unwraps the ``data``/``result`` envelope, which a subtitle
            document does not have, so an unset ``raw`` on the signed-document
            route is rejected the way the pin rejects it.
            """

            if byte:
                raise AssertionError("the seam mirrors no byte transport")
            if self.url == "https://api.bilibili.com/x/web-interface/nav":
                if raw:
                    raise AssertionError("the nav seam scripts the unwrapped payload")
                self._record("credential.nav")
                if script.nav_error is not None:
                    raise script.nav_error
                return script.nav_response
            if self._is_player_call():
                if raw:
                    raise AssertionError(
                        "the pin answers the whole envelope to"
                        " request(raw=True); the seam scripts the unwrapped"
                        " player payload only"
                    )
                return self._player_result()
            if self._is_tag_call():
                if raw:
                    raise AssertionError(
                        "the pin answers the whole envelope to"
                        " request(raw=True); the seam scripts the unwrapped"
                        " tag array only"
                    )
                return self._tag_result()
            if self._is_user_video_page_call():
                if raw:
                    raise AssertionError(
                        "the pin answers the whole envelope to"
                        " request(raw=True); the seam scripts the unwrapped"
                        " payload only"
                    )
                return self._user_video_page_result()
            if not raw:
                raise FakeResponseCodeException(-1, "API 返回数据未含 code 字段")
            return self._subtitle_document_result()

        def _is_player_call(self) -> bool:
            """True for the player call, at the scripted or the mirrored URL."""

            return self.url in (
                script.player_endpoint["url"],
                FAKE_PLAYER_ENDPOINT["url"],
            )

        def _is_tag_call(self) -> bool:
            """True for the tag call, at the scripted or the mirrored URL.

            The tag endpoint declares neither ``wbi`` nor ``dm``, so it is
            matched on its URL alone — the transport flags the adapter chose
            are asserted from the recorded request, not from this predicate.
            """

            return self.url in (
                script.tag_endpoint["url"],
                FAKE_TAG_ENDPOINT["url"],
            )

        def _is_user_video_page_call(self) -> bool:
            """True for the page call, at the scripted or the mirrored URL."""

            return self.url in (
                script.user_video_page_endpoint["url"],
                FAKE_USER_VIDEO_PAGE_ENDPOINT["url"],
            )

        def _record(self, call: str, *, raw: bool = False) -> None:
            """Record one issued request with its flags and parameter set."""

            params = dict(self.params)
            if self.dm:
                params.update(_device_fingerprint_params())
            script.api_requests.append(
                FakeApiRequest(
                    url=self.url,
                    method=self.method,
                    verify=self.verify,
                    wbi=self.wbi,
                    dm=self.dm,
                    params=params,
                    raw=raw,
                    has_sessdata=bool(getattr(self.credential, "sessdata", None)),
                )
            )
            script.calls.append(call)

        def _user_video_page_result(self) -> object:
            """Answer the user-video page call with the scripted payload."""

            page_number = self.params.get("pn")
            page_size = self.params.get("ps")
            self._record(f"space.arc.search(pn={page_number}, ps={page_size})")
            if script.videos_error is not None:
                raise script.videos_error
            response = script.videos_response
            if callable(response):
                response = response(pn=page_number, ps=page_size)
            return response

        def _player_result(self) -> object:
            """Answer the player call with the scripted subtitle inventory."""

            bvid = self.params.get("bvid")
            cid = self.params.get("cid")
            self._record(f"player.track_list(bvid={bvid}, cid={cid})")
            if script.player_error is not None:
                raise script.player_error
            response = script.player_response
            if callable(response):
                response = response(bvid=bvid, cid=cid)
            return response

        def _tag_result(self) -> object:
            """Answer the tag call with the scripted tag array.

            Recorded under ``video.tags`` (the documented call name) and under
            ``tag_calls`` by ``bvid``, so both the allow-list scan and the
            once-per-video count see it.
            """

            bvid = self.params.get("bvid")
            self._record("video.tags")
            script.tag_calls.append(bvid)
            if script.tags_error is not None:
                raise script.tags_error
            response = script.tags_response
            if callable(response):
                response = response(bvid=bvid)
            # Unscripted means the empty tag array, not ``None``: the tag call
            # is best-effort in production, an untagged video really does answer
            # ``data: []``, and answering ``None`` here would make every
            # pre-tag test fail on a shape error instead.  A test that wants a
            # malformed payload scripts one explicitly.
            return [] if response is None else response

        def _subtitle_document_result(self) -> object:
            """Answer the signed subtitle-document fetch with its scripted body."""

            self._record("subtitle.body", raw=True)
            if self.url not in script.subtitle_bodies:
                raise AssertionError(
                    f"unexpected subtitle-document fetch: {self.url!r}"
                )
            outcome = script.subtitle_bodies[self.url]
            if callable(outcome):
                outcome = outcome(self.url)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    network_mod.Api = Api

    video_mod = types.ModuleType("bilibili_api.video")

    class Video:
        """Mirror of ``video.Video(bvid, credential)`` with metadata calls only."""

        def __init__(
            self,
            bvid: str | None = None,
            aid: int | None = None,
            credential: object = None,
        ) -> None:
            self.bvid = bvid
            self.aid = aid
            self.credential = credential

        async def get_info(self) -> dict:
            script.calls.append("video.get_info")
            if script.info_error is not None:
                raise script.info_error
            return script.info_response

        async def get_pages(self) -> list:
            script.calls.append("video.get_pages")
            if script.parts_error is not None:
                raise script.parts_error
            response = script.parts_response
            if callable(response):
                response = response(bvid=self.bvid)
            return response

    video_mod.Video = Video
    video_mod.API = {
        "info": {"get_player_info": script.player_endpoint, "tags": script.tag_endpoint}
    }

    package.user = user_mod
    package.video = video_mod
    package.exceptions = exceptions_mod
    package.request_settings = request_settings_mod

    return {
        "bilibili_api": package,
        "bilibili_api.user": user_mod,
        "bilibili_api.utils.network": network_mod,
        "bilibili_api.video": video_mod,
        "bilibili_api.exceptions": exceptions_mod,
        "bilibili_api.request_settings": request_settings_mod,
    }


def make_vlist_item(**overrides: object) -> dict:
    """Build one documented arc/search vlist item with literal values.

    ``author`` is the uploader's display name as upstream's own vlist items
    carry it.  It is a literal default rather than an override-only key because
    the run-level ingestor reads it: a test that wants the ``str(mid)``
    fallback pops the key, which is also how it shows the fallback is the
    ingestor's decision rather than the page boundary's.

    ``typeid``, ``pic`` and ``description`` are the **list** endpoint's own
    spellings for the category id, the cover URL and the description (the view
    endpoint spells the category ``tid``), and they are literal defaults for
    the same reason: the normalizer reads them, so an override-only key would
    prove only that it can read a dictionary a test invented.  A test that
    wants the absent case pops them, exactly as it does for the name.
    ``pic`` is a recorded form rather than a plausible-looking one — the
    ``arc/search`` archive stem upstream issues is 40 hex characters.
    """

    item = {
        "aid": 111,
        "bvid": BVID,
        "title": "未明子讲座",
        "created": PUBDATE,
        "mid": MID,
        "author": "未明子",
        "typeid": 124,
        "pic": "http://i1.hdslb.com/bfs/archive/367e793f720ea124722f970965a2db1ba3a733a7.jpg",
        "description": "哲学讲座简介",
    }
    item.update(overrides)
    return item


def make_videos_response(*items: object, count: int | None = 2) -> dict:
    """Build the inner arc/search data: ``list.vlist`` plus ``page.count``."""

    response: dict = {"list": {"vlist": list(items)}}
    if count is not None:
        response["page"] = {"pn": 1, "ps": 30, "count": count}
    return response


def make_part_item(**overrides: object) -> dict:
    """Build one documented pagelist element with literal values."""

    item = {
        "cid": 2222,
        "page": 1,
        "part": "第一部分",
        "duration": 12,
    }
    item.update(overrides)
    return item


def make_tag_item(**overrides: object) -> dict:
    """Build one documented tag element with literal values.

    The shape is the pinned descriptor's response entry, live-probed: the
    fields the archive keeps plus the two it deliberately does not
    (``music_id``, ``jump_url``).  ``jump_url`` is present and empty on
    purpose: it is the field a loosened normalizer might forward, and its
    emptiness here means the store's "no URL column" rule is asserted against
    the schema rather than against a value that happens to be blank.
    """

    item = {
        "tag_id": 943,
        "tag_name": "爱情",
        "music_id": "",
        "tag_type": "old_channel",
        "jump_url": "",
    }
    item.update(overrides)
    return item


def script_parts_by_bvid(
    script: FakeUpstreamScript, parts_by_bvid: dict[str, object]
) -> None:
    """Script one parts payload per requested ``bvid`` on the package seam.

    ``video.Video.get_pages`` answers with the scripted entry for its own
    ``bvid``; an unexpected bvid fails loudly like every other unscripted
    fetch.  A plain ``parts_response`` value keeps the previous behavior of
    answering every video with the same list.
    """

    def parts_response(bvid: str) -> object:
        if bvid not in parts_by_bvid:
            raise AssertionError(f"unexpected video-parts fetch: {bvid!r}")
        return parts_by_bvid[bvid]

    script.parts_response = parts_response


def make_detail_response(**overrides: object) -> dict:
    """Build the view-API detail body used to fill a missing aid."""

    detail = {
        "aid": 111,
        "bvid": BVID,
        "title": "未明子讲座",
        "pubdate": PUBDATE,
        "owner": {"mid": MID, "name": "未明子"},
    }
    detail.update(overrides)
    return detail


def make_subtitle_track(**overrides: object) -> dict:
    """Build one documented ``data.subtitle.subtitles[]`` entry.

    The default is an uploader/human (CC) track: it carries neither of the
    upstream AI markers, which is the conservative reading.  Its
    ``subtitle_url`` is the signed-document sentinel, so a leaked track URL is
    caught by the same scanner as every other secret.
    """

    track = {
        "id": 1,
        "lan": "zh-CN",
        "lan_doc": "中文（中国）",
        "subtitle_url": SIGNED_SUBTITLE_URL_MARKER,
    }
    track.update(overrides)
    return track


def make_player_response(*tracks: object, allow_submit: bool = False) -> dict:
    """Build the player call's unwrapped payload carrying subtitle tracks."""

    return {"subtitle": {"allow_submit": allow_submit, "subtitles": list(tracks)}}


def make_subtitle_entry(**overrides: object) -> dict:
    """Build one documented subtitle-document ``body`` row, in seconds."""

    entry = {"from": 0.0, "to": 1.5, "content": "未明子"}
    entry.update(overrides)
    return entry


def make_subtitle_document(*entries: object) -> dict:
    """Build one documented subtitle JSON document around the given rows."""

    return {"font_size": 0.4, "body": list(entries)}


def persisted_row_text(connection: object) -> str:
    """Render every persisted row of every table and view as one text blob.

    The no-secret assertions scan this blob so a leaked sentinel anywhere in
    the repository is caught, not only in one hand-picked table.
    """

    persisted_objects = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        " AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    lines = []
    for persisted_object in persisted_objects:
        object_name = persisted_object["name"]
        for row in connection.execute(f"SELECT * FROM {object_name}"):
            lines.append(f"{object_name}:{tuple(row)!r}")
    return "\n".join(lines)


def assert_only_documented_metadata_calls(
    calls: object, *, context: str = "gateway calls"
) -> None:
    """Assert every recorded upstream call stays on the documented surface."""

    unexpected = [
        call for call in calls if not call.startswith(DOCUMENTED_METADATA_CALLS)
    ]
    if unexpected:
        raise AssertionError(
            f"{context} left the documented metadata surface: {unexpected}"
        )


def assert_leaks_no_markers(text: str, *, context: str) -> None:
    """Assert the text carries none of the seam's secret/payload sentinels."""

    leaked = [marker for marker in NO_LEAK_MARKERS if marker in text]
    if leaked:
        raise AssertionError(f"{context} leaked seam payload sentinels: {leaked}")


@pytest.fixture
def bilibili_api_seam(monkeypatch) -> FakeUpstreamScript:
    """Install the fake ``bilibili_api`` package and yield its script.

    Only the adapter module is dropped from the module cache, before and
    after each test: it re-imports against the current seam, while every
    other ``bili_asr`` module stays cached so class identity is preserved
    between the test imports and the adapter imports.
    """

    # These references apply only to this fake upstream, never a live request.
    monkeypatch.setenv("BILI_PLATFORM_ACCESS_REFERENCE", "test:offline-platform")
    monkeypatch.setenv("BILI_CONTENT_PROCESSING_REFERENCE", "test:offline-content")
    script = FakeUpstreamScript()
    for name, module in build_fake_package(script).items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, GATEWAY_ADAPTER_MODULE, raising=False)
    try:
        # Offline package calls must exercise pacing without waiting for real
        # network rate limits. Patch only this adapter's constructor, leaving
        # asyncio.sleep, test-patched defaults, and injected sleepers untouched.
        adapter = importlib.import_module(GATEWAY_ADAPTER_MODULE)
        original_init = adapter.BilibiliApiGateway.__init__

        async def record_sleep(delay: float) -> None:
            script.pacing_delays.append(delay)

        @wraps(original_init)
        def offline_init(self, *args, **kwargs):
            if (
                kwargs.get("_sleeper") is None
                and adapter.asyncio.sleep is _REAL_ASYNC_SLEEP
            ):
                kwargs["_sleeper"] = record_sleep
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(adapter.BilibiliApiGateway, "__init__", offline_init)
        yield script
    finally:
        sys.modules.pop(GATEWAY_ADAPTER_MODULE, None)


@pytest.fixture
def fake_gateway_seam(monkeypatch) -> FakeGateway:
    """Install one ``FakeGateway`` as the product adapter and yield it.

    Every command handler imports ``BilibiliApiGateway`` inside its own body,
    so replacing that class in its module puts the whole command path — CLI
    composition root included — on this protocol double, with no network and no
    package seam.  The replacement records the credential the composition root
    handed it on ``FakeGateway.sessdata``.

    The module is reached through ``importlib`` deliberately: the package-seam
    fixture above drops the adapter module from ``sys.modules``, and a plain
    ``import ... as`` would then bind the parent package's stale attribute
    instead of the module the handler imports from.
    """

    # This seam is an explicitly offline test provider.  Its opaque policy
    # references are test labels, never live authorization or credentials.
    monkeypatch.setenv("BILI_PLATFORM_ACCESS_REFERENCE", "test:offline-platform")
    monkeypatch.setenv("BILI_CONTENT_PROCESSING_REFERENCE", "test:offline-content")
    gateway = FakeGateway()
    gateway_module = importlib.import_module(GATEWAY_ADAPTER_MODULE)

    def factory(
        sessdata: str | None = None, proxy: str | None = None, request_scheduler=None
    ) -> FakeGateway:
        del proxy
        gateway.sessdata = sessdata
        if request_scheduler is not None:
            gateway.request_scheduler = request_scheduler
        return gateway

    monkeypatch.setattr(gateway_module, "BilibiliApiGateway", factory)
    return gateway


__all__ = [
    "BVID",
    "DOCUMENTED_METADATA_CALLS",
    "FAKE_PACKAGE_VERSION",
    "FAKE_PLAYER_ENDPOINT",
    "FAKE_TAG_ENDPOINT",
    "FAKE_USER_VIDEO_PAGE_ENDPOINT",
    "FakeApiException",
    "FakeApiRequest",
    "FakeGateway",
    "FakeNetworkException",
    "FakeResponseCodeException",
    "FakeResponseException",
    "FakeUpstreamScript",
    "FakeWbiRetryTimesExceedException",
    "GATEWAY_ADAPTER_MODULE",
    "MID",
    "MIRRORED_ENDPOINT_FIELDS",
    "NO_LEAK_MARKERS",
    "PROTOCOL_RELATIVE_SUBTITLE_URL",
    "PUBDATE",
    "RAW_JSON_BODY_MARKER",
    "RAW_UPSTREAM_EXCEPTION_MARKER",
    "SESSDATA_BOUNDARY_VALUE",
    "SIGNED_SUBTITLE_URL_MARKER",
    "SIGNED_URL_MARKER",
    "UPSTREAM_ERROR_TEXT",
    "assert_leaks_no_markers",
    "assert_only_documented_metadata_calls",
    "bilibili_api_seam",
    "build_fake_package",
    "fake_gateway_seam",
    "make_detail_response",
    "make_part_item",
    "make_player_response",
    "make_subtitle_document",
    "make_subtitle_entry",
    "make_subtitle_track",
    "make_tag_item",
    "make_videos_response",
    "make_vlist_item",
    "persisted_row_text",
    "script_parts_by_bvid",
]
