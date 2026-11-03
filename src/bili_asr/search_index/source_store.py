"""Neutral FTS cache for explicit universal archives.

Authority remains the source/part/transcript tables. Only an explicit build
creates these rebuildable cache objects; every query is a read-only operation.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from bili_asr.platform_identity import ContentRef
from bili_asr.source_identity import source_url
from bili_asr.storage.archive_contracts import require_universal_contract

from .common import _cjk_bigram_stream, _create_snippet, _redact_text
from .errors import FTS5UnavailableError, SearchIndexMissingError, TranscriptStoreError

TABLE = "source_transcript_fts"
META = "source_transcript_index_meta"
KEYS = "source_transcript_index_keys"
_COLUMNS = ("block_key", "video_part_id", "platform", "external_video_id", "page_index",
            "start_ms", "end_ms", "pubdate", "text", "bigram", "source")


@dataclass(frozen=True)
class SourceSearchHit:
    block_key: str
    video_part_id: int
    platform: str
    external_video_id: str
    page_index: int
    pubdate: int | None
    text: str
    snippet: str
    video_title: str
    source: str = "store"
    start_ms: int | None = None
    end_ms: int | None = None
    rank: float | None = None
    hit_type: str = "transcript"
    matched_fields: tuple[str, ...] = ()

    @property
    def bvid(self):
        return self.external_video_id if self.platform == "bilibili" else None

    def to_dict(self):
        result = {"hit_type": self.hit_type, "block_key": self.block_key, "video_part_id": self.video_part_id,
                  "platform": self.platform, "external_video_id": self.external_video_id, "page_index": self.page_index,
                  "start_ms": self.start_ms, "end_ms": self.end_ms, "pubdate": self.pubdate,
                  "text": self.text, "source": self.source, "rank": self.rank, "snippet": self.snippet,
                  "video_title": self.video_title, "url": source_url(ContentRef(self.platform, self.external_video_id, self.page_index))}
        if self.bvid is not None:
            result["bvid"] = self.bvid
        if self.hit_type == "metadata":
            result["matched_fields"] = list(self.matched_fields)
        return result


def has_index(connection):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is not None


def assert_index(connection):
    require_universal_contract(connection)
    if not has_index(connection):
        raise SearchIndexMissingError("source transcript index missing; run `bili-asr search-index`")
    actual = tuple(row[1] for row in connection.execute(f"PRAGMA table_info({TABLE})"))
    if actual != _COLUMNS:
        raise TranscriptStoreError("source transcript index columns are altered")
    for name in (META, KEYS):
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone():
            raise TranscriptStoreError("source transcript index progress is missing")


def metadata(connection):
    if not has_index(connection):
        return {}
    assert_index(connection)
    return dict(connection.execute(f"SELECT key,value FROM {META} ORDER BY key"))


def count(connection):
    if not has_index(connection):
        return 0
    assert_index(connection)
    return int(connection.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0])


def stamp(connection):
    return int(metadata(connection).get("completed_transcript_id", -1))


def build(connection):
    require_universal_contract(connection)
    try:
        connection.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE} USING fts5("
            "block_key UNINDEXED,video_part_id UNINDEXED,platform UNINDEXED,external_video_id UNINDEXED,"
            "page_index UNINDEXED,start_ms UNINDEXED,end_ms UNINDEXED,pubdate UNINDEXED,text,bigram,source UNINDEXED,tokenize='unicode61')")
    except sqlite3.OperationalError as exc:
        if "no such module" in str(exc).lower():
            raise FTS5UnavailableError("SQLite FTS5 is unavailable") from exc
        raise
    connection.execute(f"CREATE TABLE IF NOT EXISTS {META}(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    connection.execute(f"CREATE TABLE IF NOT EXISTS {KEYS}(transcript_id INTEGER NOT NULL,ordinal INTEGER NOT NULL,PRIMARY KEY(transcript_id,ordinal))")
    connection.commit()
    assert_index(connection)
    upper = connection.execute("SELECT COALESCE(MAX(transcript_id),-1) FROM transcripts").fetchone()[0]
    total = 0
    after_id, after_ordinal = -1, -1
    while True:
        # Seek the existing segment composite key in its own order. An OR on
        # the parent ID makes SQLite sort all remaining segments for each page.
        rows = connection.execute(
            f"SELECT t.transcript_id,s.ordinal,s.start_ms,s.end_ms,s.text,p.video_part_id,p.platform,p.external_video_id,p.page_index,p.pubdate "
            "FROM transcript_segments s JOIN transcripts t USING(transcript_id) JOIN v_source_parts p USING(video_part_id) "
            f"WHERE s.transcript_id<=? AND (s.transcript_id,s.ordinal)>(?,?) AND NOT EXISTS "
            f"(SELECT 1 FROM {KEYS} k WHERE k.transcript_id=s.transcript_id AND k.ordinal=s.ordinal) "
            "ORDER BY s.transcript_id,s.ordinal LIMIT 500", (upper, after_id, after_ordinal)).fetchall()
        if not rows:
            break
        with connection:
            for row in rows:
                text = _redact_text(str(row["text"]))
                connection.execute(f"INSERT INTO {TABLE} VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                    f"t{row['transcript_id']}:{row['ordinal']}",row["video_part_id"],row["platform"],row["external_video_id"],
                    row["page_index"],row["start_ms"],row["end_ms"],row["pubdate"],text,_cjk_bigram_stream(text),"store"))
                connection.execute(f"INSERT INTO {KEYS} VALUES(?,?)", (row["transcript_id"],row["ordinal"]))
        total += len(rows)
        after_id, after_ordinal = rows[-1]["transcript_id"], rows[-1]["ordinal"]
    with connection:
        # The key ledger and FTS rows commit together. A partial batch failure
        # leaves no false completion stamp and the next build resumes by key.
        connection.executemany(f"INSERT OR REPLACE INTO {META} VALUES(?,?)", (
            ("completed_transcript_id", str(upper)), ("indexed_count", str(count(connection))), ("built_at", str(int(time.time())))))
    return total


def search_blocks(connection, query, *, pubdate_from=None, pubdate_to=None, limit=20):
    assert_index(connection)
    if not query.strip() or limit is not None and limit <= 0:
        return []
    where = [f"{TABLE} MATCH ?"]
    phrase = '"' + query.strip().replace('"', '""') + '"'
    bigrams = _cjk_bigram_stream(query.strip())
    parameters = [f"text:{phrase} OR bigram:\"{bigrams.replace(chr(34), chr(34)*2)}\"" if bigrams else phrase]
    if pubdate_from is not None:
        where.append("p.pubdate>=?"); parameters.append(pubdate_from)
    if pubdate_to is not None:
        where.append("p.pubdate<?"); parameters.append(pubdate_to)
    # Text remains an incremental cache, while a verified metadata refresh may
    # correct the source publication time without changing any transcript. Use
    # the current source fact for both date filtering and the returned hit.
    sql = (f"SELECT f.*,p.pubdate AS current_pubdate,bm25({TABLE}) AS relevance,p.video_title FROM {TABLE} f "
           "JOIN v_source_parts p ON p.video_part_id=f.video_part_id "
           f"WHERE {' AND '.join(where)} ORDER BY relevance,f.block_key")
    if limit is not None:
        sql += " LIMIT ?"; parameters.append(limit)
    rows = connection.execute(sql, parameters).fetchall()
    return [SourceSearchHit(block_key=row["block_key"],video_part_id=int(row["video_part_id"]),platform=row["platform"],
        external_video_id=row["external_video_id"],page_index=int(row["page_index"]),pubdate=row["current_pubdate"],text=row["text"],
        snippet=_create_snippet(row["text"],query),video_title=row["video_title"],source=row["source"],
        start_ms=int(row["start_ms"]),end_ms=int(row["end_ms"]),rank=float(row["relevance"])) for row in rows]


def search_metadata(connection, query, *, pubdate_from=None, pubdate_to=None, limit=20):
    require_universal_contract(connection)
    # Metadata remains useful before any transcript/cache exists. Read bounded
    # pages rather than creating an index or collecting the whole archive.
    needle = query.casefold().strip()
    if not needle or limit <= 0:
        return []
    hits, after = [], 0
    while len(hits) < limit:
        rows = connection.execute(
            'SELECT p.*,d."desc" AS description,(SELECT group_concat(tag_name,char(10)) FROM '
            '(SELECT tag_name FROM video_tags WHERE bvid=p.bvid ORDER BY tag_name,tag_id)) AS tags '
            'FROM v_source_parts p LEFT JOIN video_details d ON d.bvid=p.bvid '
            'WHERE p.video_part_id>? ORDER BY p.video_part_id LIMIT 256', (after,)).fetchall()
        if not rows:
            break
        from bili_asr.storage.source_projection import read_youtube_fields_many
        youtube = read_youtube_fields_many(connection, tuple(dict.fromkeys(
            row["external_video_id"] for row in rows if row["platform"] == "youtube")))
        for row in rows:
            published = row["pubdate"]
            if pubdate_from is not None and (published is None or published < pubdate_from):
                continue
            if pubdate_to is not None and (published is None or published >= pubdate_to):
                continue
            fields = {"video_title": row["video_title"], "part_title": row["title"], "creator_name": row["creator_name"]}
            if row["platform"] == "youtube":
                projected = youtube[row["external_video_id"]]
                fields.update(description=projected.description, tags="\n".join(projected.tags))
            else:
                fields.update(description=row["description"], tags=row["tags"])
            matched = tuple(key for key,value in fields.items() if value and needle in value.casefold())
            if matched:
                text = _redact_text("\n".join(fields[key] for key in matched))
                hits.append(SourceSearchHit(block_key=f"source-meta:{row['video_part_id']}",video_part_id=row["video_part_id"],
                    platform=row["platform"],external_video_id=row["external_video_id"],page_index=row["page_index"],pubdate=published,
                    text=text,snippet=_create_snippet(text,query),video_title=row["video_title"],source="metadata",hit_type="metadata",matched_fields=matched))
                if len(hits) == limit:
                    break
        after = rows[-1]["video_part_id"]
    return hits
