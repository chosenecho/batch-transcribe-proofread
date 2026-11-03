"""Models implementation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TranscriptSearchHit:
    """One matched time-bounded transcript block."""

    block_key: str
    bvid: str
    page_index: int
    start_ms: int
    end_ms: int
    pubdate: int
    text: str
    source: str
    rank: float
    snippet: str
    video_title: str = ""

    @property
    def hit_type(self) -> str:
        return "transcript"

    def to_dict(self) -> dict[str, object]:
        return {
            "hit_type": self.hit_type,
            "block_key": self.block_key,
            "bvid": self.bvid,
            "page_index": self.page_index,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "pubdate": self.pubdate,
            "text": self.text,
            "source": self.source,
            "rank": self.rank,
            "snippet": self.snippet,
            "video_title": self.video_title,
        }


@dataclass(frozen=True)
class MetadataSearchHit:
    """One video-metadata match, expanded to each stored part when present."""

    block_key: str
    bvid: str
    page_index: int | None
    pubdate: int
    text: str
    snippet: str
    video_title: str
    matched_fields: tuple[str, ...]
    hit_type: str = "metadata"
    source: str = "metadata"
    start_ms: None = None
    end_ms: None = None
    rank: None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "hit_type": self.hit_type,
            "block_key": self.block_key,
            "bvid": self.bvid,
            "page_index": self.page_index,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "pubdate": self.pubdate,
            "text": self.text,
            "source": self.source,
            "rank": self.rank,
            "snippet": self.snippet,
            "video_title": self.video_title,
            "matched_fields": list(self.matched_fields),
        }
