"""Read-only literal search of the current video metadata, without FTS."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3

from .errors import SearchIndexMissingError, TranscriptStoreError
from .models import MetadataSearchHit
from bili_asr.storage.archive_contracts import BILIBILI_V1, UNIVERSAL_V2, runtime_contract


_REQUIRED_COLUMNS = {
    "videos": {"bvid", "title", "pubdate"},
    "video_parts": {"bvid", "page_index"},
    "video_details": {"bvid", "desc"},
    "video_tags": {"bvid", "tag_id", "tag_name"},
}
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _like_pattern(query: str) -> str:
    # Order matters: first escape the escape character, then SQL wildcards.
    return "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _snippet(text: str, query: str, *, width: int = 180) -> str:
    """Bound display text around the literal match, with SQLite's ASCII folding."""
    position = text.translate(_ASCII_LOWER).find(query.translate(_ASCII_LOWER))
    start = max(0, position - 45) if position >= 0 else 0
    end = min(len(text), start + width)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


class MetadataSearchIndex:
    """Query stored title, description, and tags; never create or modify a store."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.db_path = Path(root) / "archive.db"

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise SearchIndexMissingError("metadata store missing — collect metadata with `bili-asr fetch-meta`")
        try:
            connection = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            return connection
        except sqlite3.DatabaseError as exc:
            raise TranscriptStoreError(f"metadata store unreadable: {exc}") from exc

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> str:
        for table, required in _REQUIRED_COLUMNS.items():
            if table == "videos":
                # LIMIT 0 validates column references without reading video rows.
                # Unlike pragma_table_info(), it emits no nested trace statement.
                try:
                    marked = connection.execute("SELECT EXISTS (SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='archive_contract'), "
                        "EXISTS (SELECT bvid,title,pubdate FROM videos LIMIT 0)").fetchone()[0]
                except sqlite3.OperationalError:
                    columns = {str(row[1]) for row in connection.execute('PRAGMA table_info("videos")')}
                else:
                    if marked:
                        return runtime_contract(connection)
                    continue
            else:
                columns = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
            if missing := required - columns:
                raise TranscriptStoreError(
                    f"metadata schema incompatible: {table} missing {', '.join(sorted(missing))}; "
                    "use a database with the current metadata schema"
                )
        return BILIBILI_V1

    def search_metadata(
        self,
        query: str,
        *,
        pubdate_from: int | None = None,
        pubdate_to: int | None = None,
        limit: int | None = 20,
    ) -> list[MetadataSearchHit]:
        """Return title, tags, then description matches, followed by stable identity.

        Date bounds are Unix seconds in a half-open interval, matching the
        transcript API. Multiple matched fields/tags produce one hit per part.
        A video without any parts produces one video-level hit with no page.
        """
        connection = self._connect()
        try:
            if self._validate_schema(connection) == UNIVERSAL_V2:
                from .source_store import search_metadata
                return search_metadata(connection, query, pubdate_from=pubdate_from,
                    pubdate_to=pubdate_to, limit=20 if limit is None else limit)
            clean_query = (query or "").strip()
            if not clean_query or (limit is not None and limit <= 0):
                return []
            pattern = _like_pattern(clean_query)
            where: list[str] = []
            parameters: list[object] = [pattern, pattern, pattern, pattern]
            if pubdate_from is not None:
                where.append("v.pubdate >= ?")
                parameters.append(pubdate_from)
            if pubdate_to is not None:
                where.append("v.pubdate < ?")
                parameters.append(pubdate_to)
            date_clause = "WHERE " + " AND ".join(where) if where else ""
            sql = f"""
                WITH matched AS (
                    SELECT v.bvid, v.title, v.pubdate, d."desc" AS description,
                           v.title LIKE ? ESCAPE '\\' AS title_match,
                           COALESCE(d."desc" LIKE ? ESCAPE '\\', 0) AS description_match,
                           EXISTS (
                               SELECT 1 FROM video_tags t
                               WHERE t.bvid = v.bvid AND t.tag_name LIKE ? ESCAPE '\\'
                           ) AS tags_match,
                           (SELECT group_concat(tag_name, char(10)) FROM (
                               SELECT t.tag_name FROM video_tags t
                               WHERE t.bvid = v.bvid AND t.tag_name LIKE ? ESCAPE '\\'
                               ORDER BY t.tag_name COLLATE BINARY, t.tag_id
                           )) AS matching_tags
                    FROM videos v LEFT JOIN video_details d ON d.bvid = v.bvid
                    {date_clause}
                )
                SELECT m.*, p.page_index FROM matched m
                LEFT JOIN video_parts p ON p.bvid = m.bvid
                WHERE m.title_match OR m.tags_match OR m.description_match
                ORDER BY CASE WHEN m.title_match THEN 0 WHEN m.tags_match THEN 1 ELSE 2 END,
                         m.pubdate DESC, m.bvid ASC, p.page_index ASC
            """
            if limit is not None:
                sql += " LIMIT ?"
                parameters.append(limit)
            hits: list[MetadataSearchHit] = []
            for row in connection.execute(sql, parameters):
                fields = tuple(
                    field for field, column in (
                        ("title", "title_match"), ("tags", "tags_match"),
                        ("description", "description_match"),
                    ) if row[column]
                )
                primary = fields[0]
                text = str(row[{"title": "title", "tags": "matching_tags", "description": "description"}[primary]])
                page = int(row["page_index"]) if row["page_index"] is not None else None
                bvid = str(row["bvid"])
                identity = f"p{page}" if page is not None else "video"
                hits.append(MetadataSearchHit(
                    block_key=f"metadata:{bvid}:{identity}", bvid=bvid, page_index=page,
                    pubdate=int(row["pubdate"]), text=text, snippet=f"{primary}: {_snippet(text, clean_query)}",
                    video_title=str(row["title"]), matched_fields=fields,
                ))
            return hits
        except sqlite3.DatabaseError as exc:
            raise TranscriptStoreError(f"metadata store corrupt or incompatible: {exc}") from exc
        finally:
            connection.close()
