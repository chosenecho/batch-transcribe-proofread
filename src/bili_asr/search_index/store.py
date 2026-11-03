"""Store implementation."""

from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Iterator, Sequence
from bili_asr.archive import bundle_relpaths_for_stem
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.archive_session import ArchiveAccessMode, ArchiveContract, open_archive_connection
import bili_asr.search_index.common as _dependency_common
import bili_asr.search_index.constants as _dependency_constants
import bili_asr.search_index.errors as _dependency_errors
import bili_asr.search_index.models as _dependency_models
import bili_asr.search_index.readers as _dependency_readers
from bili_asr.storage.archive_contracts import BILIBILI_V1, UNIVERSAL_V2, runtime_contract
from . import source_store


class TranscriptSearchIndex:
    """Store-backed FTS5 index over archived transcript blocks in ``archive.db``.

    The index table is created only through :meth:`build` (the ``search-index``
    command path) — never from the store's own ``open_database`` — per the
    no-migration rule: no ALTER, no ``user_version`` bump, and
    ``schema-transcripts.sql`` is untouched, so old stores keep their shape.
    Builds are incremental and idempotent: existing rows are left alone
    (re-running changes nothing) and only newly stored transcript segments are
    appended, one transaction per batch.
    """

    def __init__(
        self,
        root: str | os.PathLike[str] | Path,
        *,
        artifact_roots: ArtifactRoots | None = None,
    ) -> None:
        self.root = os.fspath(root)
        self.artifact_roots = (
            artifact_roots if artifact_roots is not None else ArtifactRoots.of(self.root)
        )
        self.db_path = os.path.join(self.root, "archive.db")

    # -- helpers -----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open the store; missing means backlog (the search-index path creates)."""
        if not os.path.isfile(self.db_path):
            raise _dependency_errors.SearchIndexMissingError(
                "transcript store missing — index missing; run `bili-asr search-index`"
            )
        try:
            return open_archive_connection(
                self.db_path, mode=ArchiveAccessMode.READ, contract=ArchiveContract.NONE,
                artifact_roots=self.artifact_roots,
            )
        except sqlite3.DatabaseError as exc:
            raise _dependency_errors.TranscriptStoreError(f"transcript store unreadable: {exc}") from exc

    def _connect_for_build(self) -> sqlite3.Connection:
        """Open the store for indexing, creating a fresh one when absent.

        Only the ``search-index`` path may reach here — read paths never
        auto-create the store or the index (exit-contract §1–§2: a missing
        index is backlog, not a defect).
        """
        os.makedirs(self.root, exist_ok=True)
        fresh = not os.path.exists(self.db_path)
        try:
            conn = open_archive_connection(
                self.db_path,
                mode=ArchiveAccessMode.BOOTSTRAP if fresh else ArchiveAccessMode.WRITE,
                contract=ArchiveContract.RUNTIME if fresh else ArchiveContract.NONE,
                artifact_roots=self.artifact_roots,
            )
        except sqlite3.DatabaseError as exc:
            raise _dependency_errors.TranscriptStoreError(f"transcript store unreadable: {exc}") from exc
        if fresh:
            # Bootstrap the contract schema, exactly as the store's own
            # ``open_database`` does — the search-index path may create a
            # store (an empty archive indexes to zero blocks) but never
            # ALTERs one.
            return conn
        if not self._looks_like_store(conn):
            conn.close()
            raise _dependency_errors.TranscriptStoreError(
                f"transcript store corrupt: {self.db_path} is not a valid archive database"
            )
        return conn

    @staticmethod
    def _looks_like_store(conn: sqlite3.Connection) -> bool:
        """A store always carries the contract's user table after bootstrap."""
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'bilibili_users'"
            ).fetchone()
            return row is not None
        except sqlite3.DatabaseError:
            return False

    def _probe_tokenizer(self, conn: sqlite3.Connection) -> str:
        """Pick the auxiliary-column tokenizer this SQLite build supports.

        A failed probe leaves the connection inside an aborted transaction
        (SQLite auto-opens one for the failing DDL), so each attempt rolls
        back before the next.
        """
        for tokenizer in ("trigram", "simple", "unicode61"):
            try:
                if tokenizer == "unicode61":
                    # The default tokenizer (unicode61): present whenever FTS5 is.
                    conn.execute("CREATE VIRTUAL TABLE _fts_tok_probe USING fts5(x);")
                else:
                    conn.execute(
                        "CREATE VIRTUAL TABLE _fts_tok_probe "
                        f"USING fts5(x, tokenize='{tokenizer}');"
                    )
                conn.execute("DROP TABLE _fts_tok_probe;")
                return tokenizer
            except sqlite3.OperationalError:
                conn.rollback()
        raise _dependency_errors.FTS5UnavailableError(
            "SQLite FTS5 extension is not available in this Python environment"
        )

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """The gated FTS migration shim: run only from the search-index path."""
        if not _dependency_common.check_fts5_available(conn):
            raise _dependency_errors.FTS5UnavailableError(
                "SQLite FTS5 extension is not available in this Python environment"
            )
        if self._has_index(conn):
            # Re-CREATE is a no-op and the stored tokenizer declaration stays
            # authoritative — a re-run must not fail because this build would
            # have picked a different tokenizer for a fresh table.
            pass
        else:
            tokenizer = self._probe_tokenizer(conn)
            conn.execute(_dependency_common._store_fts_ddl(tokenizer))
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_dependency_constants.STORE_INDEX_META_TABLE} ("
            "key TEXT PRIMARY KEY, value TEXT"
            ");"
        )

    def _has_index(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_dependency_constants.STORE_FTS5_TABLE,),
        ).fetchone()
        return row is not None

    def _assert_index_shape(self, conn: sqlite3.Connection) -> str:
        """Dispatch marked archives and validate legacy FTS in one schema read."""
        objects = dict(conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name IN (?, 'archive_contract')",
            (_dependency_constants.STORE_FTS5_TABLE,),
        ))
        if "archive_contract" in objects:
            return runtime_contract(conn)
        if _dependency_constants.STORE_FTS5_TABLE not in objects:
            raise _dependency_errors.SearchIndexMissingError(
                "index missing — run `bili-asr search-index` to build it"
            )
        if not re.search(r"\bUSING\s+fts5\s*\(", str(objects[_dependency_constants.STORE_FTS5_TABLE]), re.IGNORECASE):
            raise _dependency_errors.TranscriptStoreError("transcript index corrupt: expected an FTS5 table")
        try:
            columns = {
                str(item[1]) for item in conn.execute(
                    f"PRAGMA table_info({_dependency_constants.STORE_FTS5_TABLE})"
                )
            }
        except sqlite3.OperationalError as exc:
            if "no such module: fts5" in str(exc).lower():
                raise _dependency_errors.FTS5UnavailableError("SQLite FTS5 is unavailable") from exc
            raise
        required = {"block_key", "bvid", "page_index", "start_ms", "end_ms", "pubdate", "text", "source"}
        if not required <= columns:
            raise _dependency_errors.TranscriptStoreError(
                "transcript index corrupt: missing required columns " + ", ".join(sorted(required - columns))
            )
        return BILIBILI_V1

    def stamp(self) -> int:
        """Last fully indexed transcript id, or -1 before any is complete."""
        conn = self._connect()
        try:
            if runtime_contract(conn) == UNIVERSAL_V2:
                return source_store.stamp(conn)
            if not self._has_index(conn):
                return -1
            return self._store_progress(conn)[0]
        except sqlite3.DatabaseError as exc:
            raise _dependency_errors.TranscriptStoreError(f"transcript store corrupt: {exc}") from exc
        finally:
            conn.close()

    def count(self) -> int:
        """Number of indexed blocks, or 0 when no index exists."""
        conn = self._connect()
        try:
            if runtime_contract(conn) == UNIVERSAL_V2:
                return source_store.count(conn)
            if not self._has_index(conn):
                return 0
            row = conn.execute(f"SELECT COUNT(*) FROM {_dependency_constants.STORE_FTS5_TABLE}").fetchone()
            return int(row[0]) if row else 0
        except sqlite3.DatabaseError as exc:
            raise _dependency_errors.TranscriptStoreError(f"transcript store corrupt: {exc}") from exc
        finally:
            conn.close()

    def metadata(self) -> dict[str, str]:
        """Return build metadata without exposing the SQLite connection.

        A missing index is a normal pre-build state, so it returns an empty mapping;
        malformed store state remains a typed store error like :meth:`count`.
        """
        conn = self._connect()
        try:
            if runtime_contract(conn) == UNIVERSAL_V2:
                return source_store.metadata(conn)
            if not self._has_index(conn):
                return {}
            rows = conn.execute(
                f"SELECT key, value FROM {_dependency_constants.STORE_INDEX_META_TABLE} ORDER BY key"
            ).fetchall()
            return {str(row[0]): str(row[1]) for row in rows}
        except sqlite3.DatabaseError as exc:
            raise _dependency_errors.TranscriptStoreError(f"transcript store corrupt: {exc}") from exc
        finally:
            conn.close()

    # -- build -------------------------------------------------------------

    @staticmethod
    def _legacy_store_progress(
        conn: sqlite3.Connection, *, temporary_keys: bool = False,
    ) -> tuple[int, int, int]:
        """Stream the verified old prefix without a Python set of every key.

        Builds reuse their connection-local primary-key table. A read-only
        legacy stamp uses a materialized read query instead: SQLite owns the
        temporary index and no DDL runs on the read connection.
        """
        if temporary_keys:
            prefix, table, params = "", "temp._fts_legacy_store_keys", ()
        else:
            prefix = ("WITH indexed AS MATERIALIZED (SELECT block_key FROM "
                      f"{_dependency_constants.STORE_FTS5_TABLE} WHERE source = ?) ")
            table, params = "indexed", (_dependency_constants._SOURCE_STORE,)
        sql = (prefix + "SELECT ts.transcript_id, ts.ordinal, indexed.block_key "
               "FROM transcript_segments AS ts "
               f"LEFT JOIN {table} AS indexed ON indexed.block_key = "
               "'t' || ts.transcript_id || ':' || ts.ordinal ")

        def prefix_rows():
            if not temporary_keys:
                # Materialize the read-only key index once, then stream rows;
                # repeating this CTE for every page would rescan the whole FTS.
                yield from conn.execute(sql + "ORDER BY ts.transcript_id, ts.ordinal", params)
                return
            after_id, after_ordinal = -1, -1
            while True:
                rows = conn.execute(
                    sql + "WHERE ts.transcript_id > ? OR "
                    "(ts.transcript_id = ? AND ts.ordinal > ?) "
                    "ORDER BY ts.transcript_id, ts.ordinal LIMIT ?",
                    (after_id, after_id, after_ordinal, _dependency_constants.INDEX_BUILD_BATCH_SIZE),
                ).fetchall()
                if not rows:
                    return
                yield from rows
                after_id, after_ordinal = int(rows[-1][0]), int(rows[-1][1])

        completed, cursor_id, cursor_ordinal = -1, -1, -1
        current_id = None
        for row in prefix_rows():
            transcript_id, ordinal = int(row[0]), int(row[1])
            if current_id is not None and transcript_id != current_id:
                completed = current_id
            current_id = transcript_id
            if row[2] is None:
                return completed, cursor_id, cursor_ordinal
            cursor_id, cursor_ordinal = transcript_id, ordinal
        if current_id is not None:
            completed = current_id
        return completed, cursor_id, cursor_ordinal

    def _store_progress(
        self, conn: sqlite3.Connection, *, for_build: bool = False,
    ) -> tuple[int, int, int]:
        rows = dict(conn.execute(
            f"SELECT key, value FROM {_dependency_constants.STORE_INDEX_META_TABLE} "
            "WHERE key IN (?, ?, ?)", _dependency_constants._STORE_PROGRESS_KEYS,
        ))
        if not rows:
            if for_build:
                # Preserve already indexed blocks beyond a legacy gap without
                # materializing their keys in Python. This keyed, temporary
                # table is scoped to this build connection and is not schema.
                conn.execute("CREATE TEMP TABLE _fts_legacy_store_keys (block_key TEXT PRIMARY KEY)")
                conn.execute(
                    "INSERT OR IGNORE INTO temp._fts_legacy_store_keys "
                    f"SELECT block_key FROM {_dependency_constants.STORE_FTS5_TABLE} WHERE source = ?",
                    (_dependency_constants._SOURCE_STORE,),
                )
            return self._legacy_store_progress(conn, temporary_keys=for_build)
        try:
            completed, cursor_id, cursor_ordinal = (
                int(rows[key]) for key in _dependency_constants._STORE_PROGRESS_KEYS
            )
        except (KeyError, TypeError, ValueError):
            raise _dependency_errors.TranscriptStoreError("transcript index progress corrupt") from None
        if (completed < -1 or cursor_id < completed or cursor_ordinal < -1
                or (cursor_id == -1) != (cursor_ordinal == -1)):
            raise _dependency_errors.TranscriptStoreError("transcript index progress corrupt")
        return completed, cursor_id, cursor_ordinal

    @staticmethod
    def _write_store_progress(conn: sqlite3.Connection, state: tuple[int, int, int]) -> None:
        conn.executemany(
            f"INSERT OR REPLACE INTO {_dependency_constants.STORE_INDEX_META_TABLE}(key, value) VALUES (?, ?)",
            [(key, str(value)) for key, value in zip(_dependency_constants._STORE_PROGRESS_KEYS, state)],
        )

    def _new_store_rows(
        self, conn: sqlite3.Connection, after_transcript_id: int, after_ordinal: int = -1,
    ) -> Iterator[sqlite3.Row]:
        """Read a stable transcript high-water mark with bounded keyset pages.

        Closing each page query before yielding lets index batches commit on
        this connection. A concurrent acquisition belongs to the next build,
        and a failure resumes at the separately committed segment cursor.
        """
        upper_id = int(conn.execute(
            "SELECT COALESCE(MAX(transcript_id), -1) FROM transcripts"
        ).fetchone()[0])
        legacy = conn.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE name = '_fts_legacy_store_keys'"
        ).fetchone() is not None
        base_sql = _dependency_constants._STORE_BLOCKS_SQL.replace(
            "ts.text ", "ts.text, " + (
                "indexed.block_key IS NOT NULL" if legacy else "0"
            ) + " AS already_indexed ",
        )
        if legacy:
            base_sql = base_sql.replace(
                "ORDER BY t.transcript_id, ts.ordinal",
                "LEFT JOIN temp._fts_legacy_store_keys AS indexed "
                "ON indexed.block_key = 't' || t.transcript_id || ':' || ts.ordinal "
                "ORDER BY t.transcript_id, ts.ordinal",
            )
        while True:
            rows = conn.execute(
                base_sql.replace(
                    "ORDER BY t.transcript_id, ts.ordinal",
                    "WHERE t.transcript_id <= ? AND (t.transcript_id > ? "
                    "OR (t.transcript_id = ? AND ts.ordinal > ?)) "
                    "ORDER BY t.transcript_id, ts.ordinal LIMIT ?",
                ),
                (upper_id, after_transcript_id, after_transcript_id, after_ordinal,
                 _dependency_constants.INDEX_BUILD_BATCH_SIZE),
            ).fetchall()
            if not rows:
                return
            yield from rows
            after_transcript_id = int(rows[-1]["transcript_id"])
            after_ordinal = int(rows[-1]["ordinal"])

    def _published_md_candidates(
        self, conn: sqlite3.Connection, exclude: set[int] | None = None
    ) -> Iterator[sqlite3.Row]:
        """Parts the store holds but has no stored transcript for.

        These are the only candidates for the published-markdown fallback;
        parts already indexed (from the store or from markdown) carry a
        ``transcript_fts`` row keyed by their ``video_part_id`` and are
        excluded here, in SQL, so the caller never re-probes or re-reads
        their markdown on a rebuild.
        """
        # FTS5's unindexed block_key column has no equality index. Materialize
        # its stamped part ids once in a connection-local keyed table, rather
        # than scanning every FTS block for every candidate part. This also
        # avoids retaining the complete set in Python or binding an unbounded
        # NOT IN list; no persistent archive table or contract is changed.
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _fts_stamped_md_parts (video_part_id INTEGER PRIMARY KEY)")
        conn.execute("DELETE FROM temp._fts_stamped_md_parts")
        conn.execute(
            "INSERT OR IGNORE INTO temp._fts_stamped_md_parts "
            f"SELECT CAST(substr(block_key, 2, instr(block_key, ':') - 2) AS INTEGER) "
            f"FROM {_dependency_constants.STORE_FTS5_TABLE} WHERE substr(block_key, 1, 1) = ?",
            (_dependency_constants._MD_KEY_PREFIX,),
        )
        conn.commit()
        after_id = 0
        upper_id = int(conn.execute(
            "SELECT COALESCE(MAX(video_part_id), 0) FROM video_parts"
        ).fetchone()[0])
        while True:
            rows = conn.execute(
                "SELECT vp.video_part_id, vp.bvid, vp.page_index, vp.duration_ms, "
                "vp.cid, vd.pubdate "
                "FROM video_parts AS vp JOIN videos AS vd ON vd.bvid = vp.bvid "
                "WHERE vp.video_part_id > ? AND vp.video_part_id <= ? AND NOT EXISTS ("
                "SELECT 1 FROM transcripts AS t WHERE t.video_part_id = vp.video_part_id) "
                "AND NOT EXISTS (SELECT 1 FROM temp._fts_stamped_md_parts AS stamped "
                "WHERE stamped.video_part_id = vp.video_part_id) "
                "ORDER BY vp.video_part_id LIMIT ?",
                (after_id, upper_id, _dependency_constants.INDEX_BUILD_BATCH_SIZE),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                if not exclude or int(row["video_part_id"]) not in exclude:
                    yield row
            after_id = int(rows[-1]["video_part_id"])

    def _published_md_text_for(self, bvid: str, page_index: int) -> str | None:
        """Best-effort published-markdown text for one part, first hit wins."""
        stem = f"{bvid}.p{page_index}"
        for base in _dependency_readers._published_md_bases(self.artifact_roots):
            for rel in (
                bundle_relpaths_for_stem(stem)["md_path"],
                os.path.join("published", "md", f"{stem}.md"),
                os.path.join("published", f"{stem}.md"),
            ):
                full = os.path.join(os.fspath(base), rel)
                if os.path.isfile(full):
                    return _dependency_readers._parse_published_md_text(full)
        return None

    def build(self, force: bool = False) -> int:
        """Create the index when absent and index new transcript blocks.

        Idempotent: existing rows are never re-written, so re-running after a
        complete build appends nothing and changes nothing.  Incremental:
        only segments after the last committed segment are appended, one
        transaction per batch of :data:`INDEX_BUILD_BATCH_SIZE` rows or at
        a transcript boundary. The complete-transcript stamp advances only
        when every segment is committed. ``force`` keeps the historical
        rebuild flag shape; the store layer's idempotent incremental build
        needs no special-casing to honour it, and a drop-and-rebuild is
        deliberately not offered under the no-migration rule.

        Returns the number of rows appended by this invocation.
        """
        conn = self._connect_for_build()
        try:
            if runtime_contract(conn) == UNIVERSAL_V2:
                return source_store.build(conn)
            self._ensure_schema(conn)
            completed_id, after_id, after_ordinal = self._store_progress(conn, for_build=True)
            self._write_store_progress(conn, (completed_id, after_id, after_ordinal))
            conn.commit()

            pending: list[tuple[str, str, int, int, int, int, str, str, str]] = []
            indexed = 0

            def flush(store_progress: tuple[int, int, int] | None = None) -> None:
                nonlocal indexed
                if not pending and store_progress is None:
                    return
                conn.executemany(
                    f"INSERT INTO {_dependency_constants.STORE_FTS5_TABLE}("
                    "block_key, bvid, page_index, start_ms, end_ms, pubdate, "
                    "text, bigram, source"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
                    pending,
                )
                if store_progress is not None:
                    self._write_store_progress(conn, store_progress)
                conn.commit()
                indexed += len(pending)
                pending.clear()

            store_rows = iter(self._new_store_rows(conn, after_id, after_ordinal))
            row = next(store_rows, None)
            while row is not None:
                following = next(store_rows, None)
                transcript_id = int(row["transcript_id"])
                ordinal = int(row["ordinal"])
                block_key = _dependency_common._store_block_key(transcript_id, ordinal)
                if not row["already_indexed"]:
                    text = _dependency_common._redact_text(str(row["text"]))
                    pending.append(
                        (
                            block_key,
                            str(row["bvid"]),
                            int(row["page_index"]),
                            int(row["start_ms"]),
                            int(row["end_ms"]),
                            int(row["pubdate"]),
                            text,
                            _dependency_common._cjk_bigram_stream(text),
                            _dependency_constants._SOURCE_STORE,
                        )
                    )
                last_segment = (
                    following is None or int(following["transcript_id"]) != transcript_id
                )
                if last_segment:
                    completed_id = transcript_id
                if len(pending) >= _dependency_constants.INDEX_BUILD_BATCH_SIZE or last_segment:
                    flush((completed_id, transcript_id, ordinal))
                row = following
            for part in self._published_md_candidates(conn):
                part_id = int(part["video_part_id"])
                text = self._published_md_text_for(
                    str(part["bvid"]), int(part["page_index"])
                )
                if not text:
                    continue
                text = _dependency_common._redact_text(text)
                pending.append(
                    (
                        _dependency_common._md_block_key(part_id),
                        str(part["bvid"]),
                        int(part["page_index"]),
                        0,
                        int(part["duration_ms"]),
                        int(part["pubdate"]),
                        text,
                        _dependency_common._cjk_bigram_stream(text),
                        _dependency_constants._SOURCE_PUBLISHED_MD,
                    )
                )
                if len(pending) >= _dependency_constants.INDEX_BUILD_BATCH_SIZE:
                    flush()

            flush()
            total_indexed = int(
                conn.execute(f"SELECT COUNT(*) FROM {_dependency_constants.STORE_FTS5_TABLE}").fetchone()[0]
            )
            conn.execute(
                f"INSERT OR REPLACE INTO {_dependency_constants.STORE_INDEX_META_TABLE}(key, value) "
                "VALUES ('indexed_count', ?);",
                (str(total_indexed),),
            )
            conn.execute(
                f"INSERT OR REPLACE INTO {_dependency_constants.STORE_INDEX_META_TABLE}(key, value) "
                "VALUES ('built_at', ?);",
                (str(int(time.time())),),
            )
            conn.commit()
            return indexed
        except sqlite3.DatabaseError as exc:
            conn.rollback()
            raise _dependency_errors.TranscriptStoreError(f"transcript store corrupt: {exc}") from exc
        finally:
            conn.close()

    # -- query -------------------------------------------------------------

    def search_blocks(
        self,
        query: str,
        *,
        pubdate_from: int | None = None,
        pubdate_to: int | None = None,
        limit: int | None = 20,
    ) -> list[_dependency_models.TranscriptSearchHit]:
        """Query the index for matching blocks, optionally date-windowed.

        Read-only against the store except the index table; never creates the
        index — a missing ``transcript_fts`` raises :class:`SearchIndexMissingError`
        (backlog class) and a corrupt store raises :class:`TranscriptStoreError`
        (defect class).
        """
        if limit is not None and limit <= 0:
            return []
        clean_q = (query or "").strip()
        if not clean_q:
            return []

        conn = self._connect()
        try:
            if self._assert_index_shape(conn) == UNIVERSAL_V2:
                return source_store.search_blocks(conn, clean_q, pubdate_from=pubdate_from,
                    pubdate_to=pubdate_to, limit=limit)
            where = [f"{_dependency_constants.STORE_FTS5_TABLE} MATCH ?"]
            params: list[Any] = [clean_q]
            if pubdate_from is not None:
                where.append("pubdate >= ?")
                params.append(pubdate_from)
            if pubdate_to is not None:
                where.append("pubdate < ?")
                params.append(pubdate_to)
            sql = (
                f"SELECT block_key, bvid, page_index, start_ms, end_ms, pubdate, "
                f"text, source, rank FROM {_dependency_constants.STORE_FTS5_TABLE} "
                f"WHERE {' AND '.join(where)} ORDER BY rank ASC, block_key ASC"
            )
            if limit is not None:
                sql += " LIMIT ?"
                params.append(limit)
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                # FTS5 query-syntax error (unclosed quote, bare NOT, …): the
                # plain-text intent is retried as one literal phrase.
                message = str(exc).lower()
                if not any(token in message for token in (
                    "fts5: syntax error", "unterminated string", "no such column:",
                    "unknown special query:",
                )):
                    raise
                params[0] = '"' + clean_q.replace('"', '""') + '"'
                rows = conn.execute(sql, params).fetchall()
            if not rows:
                # Defect class must not depend on the match count: prove the
                # store is readable before reporting a clean empty result, the
                # way the (pre-batching) eager title read did.  A damaged
                # `videos` table still surfaces as TranscriptStoreError here.
                self._probe_videos_readable(conn)
                return []
            # Batch the per-hit reads: one query fetches the snippets for ALL
            # hit block_keys and one bounds the title scan to the hit bvids —
            # a constant query count, not one-per-hit (residual O-R3).  Both
            # helpers chunk their ``IN (...)`` lists (keys per batch) so the
            # list never approaches SQLite's variable ceiling (~250000 on the
            # 3.45.1 build here) and no single batch can degrade the rest.
            block_keys = [str(row[0]) for row in rows]
            bvids = sorted({str(row[1]) for row in rows})
            snippets = self._snippets_for_hits(conn, block_keys, clean_q)
            titles = self._titles_for_bvids(conn, bvids)
            hits: list[_dependency_models.TranscriptSearchHit] = []
            for (
                block_key, bvid, page_index, start_ms, end_ms, pubdate,
                text, source, rank,
            ) in rows:
                hits.append(
                    _dependency_models.TranscriptSearchHit(
                        block_key=str(block_key),
                        bvid=str(bvid),
                        page_index=int(page_index),
                        start_ms=int(start_ms),
                        end_ms=int(end_ms),
                        pubdate=int(pubdate),
                        text=str(text),
                        source=str(source),
                        rank=float(rank) if rank is not None else 0.0,
                        snippet=snippets.get(str(block_key), ""),
                        video_title=titles.get(str(bvid), ""),
                    )
                )
            return hits
        except sqlite3.DatabaseError as exc:
            raise _dependency_errors.TranscriptStoreError(f"transcript store corrupt: {exc}") from exc
        finally:
            conn.close()

    def _probe_videos_readable(self, conn: sqlite3.Connection) -> None:
        """Prove the ``videos`` table is readable, or raise the defect class.

        With no hits to decorate, the batched title read never runs — and a
        zero-hit query on a store whose ``videos`` table is missing/damaged
        would otherwise read as a clean empty result (exit 0) where the
        pre-batching code raised :class:`TranscriptStoreError` (exit 1).

        The probe must force the SAME decode the title path performs: a
        ``bvid``-only read is answered from the covering index (and ``LIMIT 1``
        stops on the first leaf page), so it survives exactly the damage this
        probe exists to catch.  Reading both columns without a LIMIT makes the
        whole table's column data get decoded, so a schema drift (``title``
        renamed/dropped) and page-level corruption both surface here.
        """

        conn.execute("SELECT bvid, title FROM videos").fetchall()

    # SQLite's bound-variable ceiling is ~250000 on the 3.45.1 build here; this
    # is a conservative chunk so a large result set never approaches it.
    _IN_CHUNK = 900

    @staticmethod
    def _chunked(items: Sequence[str]):
        """Yield ``items`` in bounded slices (keeps every IN (...) list small)."""

        for start in range(0, len(items), TranscriptSearchIndex._IN_CHUNK):
            yield items[start : start + TranscriptSearchIndex._IN_CHUNK]

    def _snippets_for_hits(
        self, conn: sqlite3.Connection, block_keys: Sequence[str], query: str
    ) -> dict[str, str]:
        """Snippets for ALL hit block_keys, in bounded batched queries (no N+1)."""
        if not block_keys:
            return {}
        snippets: dict[str, str] = {}
        for chunk in self._chunked(list(block_keys)):
            placeholders = ", ".join("?" for _ in chunk)
            try:
                rows = conn.execute(
                    f"SELECT block_key, snippet({_dependency_constants.STORE_FTS5_TABLE}, 6, '[', ']', '…', 12) "
                    f"FROM {_dependency_constants.STORE_FTS5_TABLE} WHERE {_dependency_constants.STORE_FTS5_TABLE} MATCH ? "
                    f"AND block_key IN ({placeholders})",
                    (query, *chunk),
                ).fetchall()
            except sqlite3.DatabaseError:
                # Snippets are presentation-only; a damaged snippet read degrades
                # this chunk rather than failing the search (pre-existing shape).
                continue
            snippets.update(
                {str(row[0]): _dependency_common._redact_text(str(row[1])) for row in rows if row[1]}
            )
        return snippets

    def _titles_for_bvids(
        self, conn: sqlite3.Connection, bvids: Sequence[str]
    ) -> dict[str, str]:
        """Titles for exactly the hit bvids (bounded scan, not the whole table).

        Deliberately does not swallow ``sqlite3.DatabaseError``: a corrupt
        ``videos`` read must propagate to ``search_blocks``'s handler and stay
        defect-class (exit 1), exactly as it was when this read sat inline.
        Chunked so a large hit set never approaches the variable ceiling.
        """

        if not bvids:
            return {}
        titles: dict[str, str] = {}
        for chunk in self._chunked(list(bvids)):
            placeholders = ", ".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT bvid, title FROM videos WHERE bvid IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            titles.update({str(row[0]): str(row[1]) for row in rows})
        return titles
