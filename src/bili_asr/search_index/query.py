"""Scope routing and stable aggregation for metadata and transcript searches."""

from __future__ import annotations

from dataclasses import dataclass
import os

from bili_asr.artifact_root import ArtifactRoots
from .errors import FTS5UnavailableError, SearchIndexMissingError
from .metadata import MetadataSearchIndex
from .models import MetadataSearchHit, TranscriptSearchHit
from .store import TranscriptSearchIndex


@dataclass(frozen=True)
class SearchResults:
    hits: tuple[MetadataSearchHit | TranscriptSearchHit, ...]
    diagnostics: tuple[str, ...] = ()


def search_archive(
    root: str | os.PathLike[str],
    query: str,
    *,
    scope: str = "transcripts",
    pubdate_from: int | None = None,
    pubdate_to: int | None = None,
    limit: int = 20,
    artifact_roots: ArtifactRoots | None = None,
) -> SearchResults:
    """Apply one total limit; metadata precedes the unchanged transcript order.

    Missing search sources are backlog diagnostics. Read/shape defects propagate
    even when metadata has already filled the total result limit.
    """
    if scope not in {"transcripts", "metadata", "all"}:
        raise ValueError(f"unknown search scope: {scope}")
    if limit <= 0:
        raise ValueError("search limit must be positive")
    hits: list[MetadataSearchHit | TranscriptSearchHit] = []
    diagnostics: list[str] = []
    if scope in {"metadata", "all"}:
        try:
            hits.extend(MetadataSearchIndex(root).search_metadata(
                query, pubdate_from=pubdate_from, pubdate_to=pubdate_to, limit=limit,
            ))
        except SearchIndexMissingError as exc:
            diagnostics.append(str(exc))
    if scope in {"transcripts", "all"}:
        index = TranscriptSearchIndex(root, artifact_roots=artifact_roots)
        try:
            transcript_hits = index.search_blocks(
                query, pubdate_from=pubdate_from, pubdate_to=pubdate_to,
                limit=max(1, limit - len(hits)),
            )
            hits.extend(transcript_hits[:limit - len(hits)])
        except SearchIndexMissingError as exc:
            diagnostics.append(str(exc))
        except FTS5UnavailableError as exc:
            if scope != "all":
                raise
            diagnostics.append(f"transcript search unavailable: {exc}")
    return SearchResults(tuple(hits), tuple(diagnostics))
