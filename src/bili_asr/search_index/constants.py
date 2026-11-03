"""Constants implementation."""

from __future__ import annotations

import re


FTS5_TABLE_NAME = "transcripts_fts"


INDEX_META_TABLE = "_index_meta"


DEFAULT_SEARCH_LIMIT = 100


MAX_SNIPPET_LENGTH = 150


_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(?:https?://)[^\s\"\']+"),
    re.compile(
        r"(?i)(?:sessdata|access[_-]?token|authorization|cookie|token|signature|sign|deadline)"
        r"\s*(?:=|:)\s*[^\s,;]+"
    ),
)


STORE_FTS5_TABLE = "transcript_fts"


STORE_INDEX_META_TABLE = "transcript_fts_index_meta"


_STORE_PROGRESS_KEYS = (
    "completed_transcript_id", "cursor_transcript_id", "cursor_ordinal",
)


_STORE_BLOCKS_SQL = (
    "SELECT t.transcript_id, t.video_part_id, vp.bvid, vp.page_index, "
    "vp.cid, vd.pubdate, vd.title AS video_title, vp.title AS part_title, "
    "ts.ordinal, ts.start_ms, ts.end_ms, ts.text "
    "FROM transcripts AS t "
    "JOIN video_parts AS vp ON vp.video_part_id = t.video_part_id "
    "JOIN videos AS vd ON vd.bvid = vp.bvid "
    "JOIN transcript_segments AS ts ON ts.transcript_id = t.transcript_id "
    "ORDER BY t.transcript_id, ts.ordinal"
)


INDEX_BUILD_BATCH_SIZE = 500


_SOURCE_STORE = "store"


_SOURCE_PUBLISHED_MD = "published-md"


_STORE_KEY_PREFIX = "t"


_MD_KEY_PREFIX = "m"
