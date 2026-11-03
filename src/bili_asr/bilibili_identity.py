"""Explicit adapters for the unchanged legacy Bilibili source contract."""

from __future__ import annotations

import re
from collections.abc import Mapping

from bili_asr.platform_identity import ContentRef, require_platform

_BVID = re.compile(r"[A-Za-z0-9_-]+\Z")
_LEGACY_SOURCE_KEYS = frozenset({"bvid", "pageIndex", "videoPartId", "url"})


def bilibili_source_url(ref: ContentRef) -> str:
    """The existing public URL shape, derived only for a Bilibili reference."""

    require_platform(ref, "bilibili")
    if _BVID.fullmatch(ref.external_video_id) is None:
        raise ValueError("publication-content: invalid BVID")
    return f"https://www.bilibili.com/video/{ref.external_video_id}/?p={ref.part_index + 1}"


def legacy_bilibili_source_ref(source: Mapping[str, object]) -> ContentRef:
    """Read a publish-v1 source without changing its JSON or content digest.

    This conversion is for validation and application calls only.  It must not
    be serialized into an existing ``prepared_json``/``content_json`` or used
    as a replacement hash input.  Other platform/contract shapes require an
    explicit future contract reader and are refused here.
    """

    if not isinstance(source, Mapping) or set(source) != _LEGACY_SOURCE_KEYS:
        # publish-v1 treats every invalid source shape as a content validation
        # failure, including wrong input types; retain its ValueError contract.
        raise ValueError("publication-content: invalid source fields")
    bvid = source["bvid"]
    if not isinstance(bvid, str):
        raise ValueError("publication-content: source.bvid must be text")  # noqa: TRY004
    try:
        bvid.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("publication-content: source.bvid must be valid UTF-8 text") from exc
    if "\x00" in bvid or any(ord(char) < 32 and char not in "\n\r\t" for char in bvid):
        raise ValueError("publication-content: source.bvid contains control characters")
    if any(char in bvid for char in "\n\r\t"):
        raise ValueError("publication-content: source.bvid must be one line")
    bvid = bvid.strip()
    if not bvid:
        raise ValueError("publication-content: source.bvid must not be empty")
    if _BVID.fullmatch(bvid) is None:
        raise ValueError("publication-content: invalid BVID")
    for key, minimum in (("pageIndex", 0), ("videoPartId", 1)):
        if type(source[key]) is not int or source[key] < minimum:
            raise ValueError(f"publication-content: source.{key} must be an integer >= {minimum}")
    ref = ContentRef("bilibili", bvid, source["pageIndex"])
    if source["url"] != bilibili_source_url(ref):
        raise ValueError("publication-content: source URL must match the frozen video and part")
    return ref


__all__ = ["bilibili_source_url", "legacy_bilibili_source_ref"]
