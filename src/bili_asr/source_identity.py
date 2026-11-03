"""Portable artifact and URL identities for registered source platforms."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, urlsplit

from bili_asr.platform_identity import ContentRef
from bili_asr.bilibili_identity import bilibili_source_url

_YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")


def source_url(ref: ContentRef) -> str:
    if ref.platform == "bilibili":
        return bilibili_source_url(ref)
    if ref.platform != "youtube" or not _YOUTUBE_ID.fullmatch(ref.external_video_id) or ref.part_index != 0:
        raise ValueError("unsupported source identity")
    return f"https://www.youtube.com/watch?v={ref.external_video_id}"


def artifact_stem(ref: ContentRef) -> str:
    source_url(ref)  # Validate provider-specific identity before generating paths.
    if ref.platform == "bilibili":
        return f"{ref.external_video_id}.p{ref.part_index}"
    return f"{ref.platform}.{hashlib.sha256(ref.external_video_id.encode('utf-8')).hexdigest()}.p{ref.part_index}"


def display_work_id(ref: ContentRef) -> str:
    source_url(ref)
    if ref.platform == "bilibili":
        return f"{ref.external_video_id}:p{ref.part_index}"
    return f"{ref.platform}:{ref.external_video_id}:p{ref.part_index}"


def youtube_ref(value: str) -> ContentRef:
    """Accept one video ID or an allowlisted single-video HTTPS URL.

    Playlists and channel discovery are separate operations. URL query inputs
    never cross into a downloader's option parser or filesystem paths.
    """
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise ValueError("invalid YouTube video identity")
    if _YOUTUBE_ID.fullmatch(value):
        return ContentRef("youtube", value)
    url = urlsplit(value)
    if url.scheme != "https" or url.username or url.password or url.port not in (None, 443) or url.fragment:
        raise ValueError("YouTube requires an HTTPS single-video URL")
    if url.hostname == "youtu.be":
        identity = url.path.removeprefix("/")
    elif url.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if url.path == "/watch":
            values = parse_qs(url.query).get("v", ())
            identity = values[0] if len(values) == 1 else ""
        elif url.path.startswith(("/shorts/", "/embed/")):
            identity = url.path.split("/")[-1]
        else:
            identity = ""
    else:
        identity = ""
    ref = ContentRef("youtube", identity)
    source_url(ref)
    return ref
