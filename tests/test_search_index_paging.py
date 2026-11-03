"""Bounded store indexing preserves durable cursors and markdown fallback."""

from __future__ import annotations

from importlib import resources
import sqlite3
import threading

import pytest

from bili_asr.archive_maintenance import ArchiveBusyError
from bili_asr.archive_session import ArchiveAccessMode, ArchiveContract, open_archive_connection
from bili_asr.search_index import common, constants
from bili_asr.search_index.store import TranscriptSearchIndex
from bili_asr.storage import TranscriptRepository, open_database
from tests.support.transcript_repository import _record, _run, _video_with_parts


pytestmark = pytest.mark.skipif(not common.check_fts5_available(), reason="SQLite FTS5 unavailable")


def _seed_transcript(root, bvid, count, cid):
    connection = open_database(root)
    try:
        part_id = _video_with_parts(connection, bvid, (cid,))[0]
        repository = TranscriptRepository(connection)
        index = connection.execute("SELECT COUNT(*) + 1 FROM acquisition_runs").fetchone()[0]
        run_id = _run(repository, index)
        stored = _record(
            repository, part_id, run_id=run_id,
            body=tuple((i * 1000, (i + 1) * 1000, f"{bvid} needle{i}") for i in range(count)),
        )
        repository.finish_acquisition_run(run_id, 300)
        return stored.transcript_id
    finally:
        connection.close()


def test_block_reads_are_bounded_and_incremental_across_many_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "INDEX_BUILD_BATCH_SIZE", 7)
    first = _seed_transcript(tmp_path, "BVpagedA", 25, 1301)
    last = _seed_transcript(tmp_path, "BVpagedB", 4, 1302)
    index = TranscriptSearchIndex(tmp_path)
    connect = index._connect_for_build
    page_queries = []

    class BoundedReads:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, params=()):
            if "JOIN transcript_segments AS ts" in sql:
                assert "LIMIT ?" in sql, "the builder loaded every unindexed block"
                assert params[-1] == 7
                page_queries.append(params)
            return self.connection.execute(sql, params)

    monkeypatch.setattr(index, "_connect_for_build", lambda: BoundedReads(connect()))
    assert index.build() == 29
    assert index.count() == 29
    assert index.stamp() == last
    assert len(page_queries) == 6  # Five bounded pages and the terminating read.
    assert len(index.search_blocks("needle24")) == 1
    assert len(index.search_blocks("needle3")) == 2
    page_queries.clear()
    assert index.build() == 0
    assert len(page_queries) == 1
    assert first < last


def test_interruption_after_a_page_commits_only_its_cursor_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "INDEX_BUILD_BATCH_SIZE", 7)
    transcript_id = _seed_transcript(tmp_path, "BVpagedfault", 19, 1311)
    index = TranscriptSearchIndex(tmp_path)
    redact = common._redact_text
    calls = 0

    def interrupted(text):
        nonlocal calls
        calls += 1
        if calls == 9:
            raise RuntimeError("interrupted after one bounded page")
        return redact(text)

    monkeypatch.setattr(common, "_redact_text", interrupted)
    with pytest.raises(RuntimeError, match="bounded page"):
        index.build()
    assert index.count() == 7
    assert index.stamp() == -1
    assert index.metadata()["cursor_ordinal"] == "6"
    monkeypatch.setattr(common, "_redact_text", redact)
    assert index.build() == 12
    assert index.stamp() == transcript_id
    assert index.count() == 19
    assert len(index.search_blocks("needle18")) == 1
    assert index.build() == 0


def test_concurrent_acquisition_is_indexed_on_the_next_build(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "INDEX_BUILD_BATCH_SIZE", 3)
    first = _seed_transcript(tmp_path, "BVinitial", 5, 1321)
    index = TranscriptSearchIndex(tmp_path)
    redact = common._redact_text
    added_id = None

    def acquire_during_build(text):
        nonlocal added_id
        if added_id is None:
            added_id = _seed_transcript(tmp_path, "BVconcurrent", 4, 1322)
        return redact(text)

    monkeypatch.setattr(common, "_redact_text", acquire_during_build)
    assert index.build() == 5
    assert index.stamp() == first
    assert added_id is not None and added_id > first
    monkeypatch.setattr(common, "_redact_text", redact)
    assert index.build() == 4
    assert index.count() == 9
    assert index.stamp() == added_id


def test_markdown_fallback_is_paged_and_never_reprobes_indexed_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "INDEX_BUILD_BATCH_SIZE", 3)
    connection = open_database(tmp_path)
    try:
        parts = _video_with_parts(connection, "BVmanymd", tuple(range(1331, 1341)))
    finally:
        connection.close()
    for page in parts:
        path = tmp_path / "transcripts" / f"BVmanymd.p{page}" / "bundle.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"markdown needle{page}", encoding="utf-8")
    index = TranscriptSearchIndex(tmp_path)
    connect = index._connect_for_build
    page_queries = []

    class BoundedMarkdownReads:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, params=()):
            if "FROM video_parts AS vp JOIN videos AS vd" in sql:
                assert "LIMIT ?" in sql
                assert "temp._fts_stamped_md_parts" in sql
                assert params[-1] == 3
                page_queries.append(params)
            return self.connection.execute(sql, params)

    monkeypatch.setattr(index, "_connect_for_build", lambda: BoundedMarkdownReads(connect()))
    assert index.build() == 10
    assert index.count() == 10
    assert len(page_queries) == 5
    page_queries.clear()
    monkeypatch.setattr(index, "_published_md_text_for", lambda *args: pytest.fail("reprobed indexed markdown"))
    assert index.build() == 0
    assert len(page_queries) == 1

    # A stored transcript for an md-indexed part remains independently indexable.
    connection = open_database(tmp_path)
    try:
        repository = TranscriptRepository(connection)
        run_id = _run(repository, 1)
        stored = _record(repository, parts[9], run_id=run_id, body=((0, 1000, "stored replacement"),))
        repository.finish_acquisition_run(run_id, 300)
    finally:
        connection.close()
    assert index.build() == 1
    assert index.count() == 11
    assert index.stamp() == stored.transcript_id
    assert len(index.search_blocks("replacement")) == 1


def test_index_session_keeps_legacy_contract_and_queries_leave_database_unchanged(tmp_path, monkeypatch):
    # A metadata/transcript archive predates the workflow and manuscript tables.
    # Indexing remains supported without implicitly bootstrapping those tables.
    database = tmp_path / "archive.db"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        scripts = resources.files("bili_asr.storage")
        for name in ("schema.sql", "schema-transcripts.sql"):
            connection.executescript(scripts.joinpath(name).read_text(encoding="utf-8"))
        connection.execute("PRAGMA user_version = 41")
        part_id = _video_with_parts(connection, "BVlegacysession", (1341,))[0]
        repository = TranscriptRepository(connection)
        run_id = _run(repository, 1)
        stored = _record(repository, part_id, run_id=run_id, body=((0, 1000, "legacy searchable"),))
        repository.finish_acquisition_run(run_id, 300)
        before = dict(connection.execute("SELECT name, sql FROM sqlite_master"))
    finally:
        connection.close()

    index = TranscriptSearchIndex(tmp_path)
    assert index.build() == 1
    connection = sqlite3.connect(database)
    try:
        after = dict(connection.execute("SELECT name, sql FROM sqlite_master"))
        assert {name: after[name] for name in before} == before
        assert set(after) - set(before) == {
            "transcript_fts", "transcript_fts_data", "transcript_fts_idx",
            "transcript_fts_content", "transcript_fts_docsize", "transcript_fts_config",
            "transcript_fts_index_meta", "sqlite_autoindex_transcript_fts_index_meta_1",
        }
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 41
    finally:
        connection.close()

    before_reads = database.read_bytes()
    statements = []
    connect = index._connect

    def traced_reader():
        result = connect()
        assert result.execute("PRAGMA query_only").fetchone()[0] == 1
        result.set_trace_callback(statements.append)
        return result

    monkeypatch.setattr(index, "_connect", traced_reader)
    assert index.count() == 1
    assert index.stamp() == stored.transcript_id
    assert index.metadata()["indexed_count"] == "1"
    assert len(index.search_blocks("searchable")) == 1
    assert database.read_bytes() == before_reads
    assert statements
    # FTS5 emits internal trace entries with a leading '-- ' comment marker.
    assert all(
        sql.lstrip().removeprefix("-- ").upper().startswith(("SELECT", "PRAGMA"))
        for sql in statements
    )


@pytest.mark.parametrize("opener", ["_connect", "_connect_for_build"])
def test_index_connection_holds_maintenance_access_until_close(tmp_path, opener):
    _seed_transcript(tmp_path, "BVindexlease", 1, 1351)
    index = TranscriptSearchIndex(tmp_path)
    connection = getattr(index, opener)()
    errors = []

    def try_maintenance():
        try:
            maintenance = open_archive_connection(
                tmp_path, mode=ArchiveAccessMode.MAINTENANCE, contract=ArchiveContract.NONE,
            )
        except Exception as exc:
            errors.append(exc)
        else:
            maintenance.close()

    try:
        worker = threading.Thread(target=try_maintenance)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], ArchiveBusyError)
    finally:
        connection.close()
    maintenance = open_archive_connection(
        tmp_path, mode=ArchiveAccessMode.MAINTENANCE, contract=ArchiveContract.NONE,
    )
    maintenance.close()


@pytest.mark.parametrize("later_already_indexed", [False, True])
def test_legacy_recovery_keeps_keys_in_sqlite_and_reads_bounded_pages(
    tmp_path, monkeypatch, later_already_indexed,
):
    monkeypatch.setattr(constants, "INDEX_BUILD_BATCH_SIZE", 7)
    first = _seed_transcript(tmp_path, "BVlegacypaged", 19, 1361)
    index = TranscriptSearchIndex(tmp_path)
    redact = common._redact_text
    calls = 0

    def interrupt(text):
        nonlocal calls
        calls += 1
        if calls == 9:
            raise RuntimeError("legacy prefix interruption")
        return redact(text)

    monkeypatch.setattr(common, "_redact_text", interrupt)
    with pytest.raises(RuntimeError, match="legacy prefix"):
        index.build()
    last = first
    if later_already_indexed:
        last = _seed_transcript(tmp_path, "BVlegacylater", 2, 1362)
    with sqlite3.connect(index.db_path) as connection:
        if later_already_indexed:
            connection.execute(
                "INSERT INTO transcript_fts "
                "(block_key, bvid, page_index, start_ms, end_ms, pubdate, text, bigram, source) "
                "SELECT 't' || ts.transcript_id || ':' || ts.ordinal, vp.bvid, vp.page_index, "
                "ts.start_ms, ts.end_ms, v.pubdate, ts.text, ts.text, 'store' "
                "FROM transcript_segments ts JOIN transcripts t ON t.transcript_id = ts.transcript_id "
                "JOIN video_parts vp ON vp.video_part_id = t.video_part_id "
                "JOIN videos v ON v.bvid = vp.bvid WHERE t.transcript_id = ?", (last,),
            )
        connection.execute(
            "DELETE FROM transcript_fts_index_meta WHERE key NOT IN ('indexed_count', 'built_at')"
        )
    before_read = (tmp_path / "archive.db").read_bytes()
    assert index.stamp() == -1
    assert (tmp_path / "archive.db").read_bytes() == before_read
    connect = index._connect_for_build
    pages = []
    prefix_pages = []
    statements = []

    class BoundedLegacyReads:
        def __init__(self, connection):
            self.connection = connection
            self.connection.set_trace_callback(statements.append)

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, params=()):
            # Existing keys must be inserted into SQLite, never fetched as a
            # Python collection. Prefix checks stream an indexed join.
            assert not sql.lstrip().startswith("SELECT block_key FROM transcript_fts")
            if "JOIN transcript_segments AS ts" in sql:
                assert "LIMIT ?" in sql and params[-1] == 7
                assert "LEFT JOIN temp._fts_legacy_store_keys" in sql
                pages.append(params)
            elif "FROM transcript_segments AS ts" in sql:
                assert "LIMIT ?" in sql and params[-1] == 7
                assert "LEFT JOIN temp._fts_legacy_store_keys" in sql
                prefix_pages.append(params)
            return self.connection.execute(sql, params)

    monkeypatch.setattr(common, "_redact_text", redact)
    monkeypatch.setattr(index, "_connect_for_build", lambda: BoundedLegacyReads(connect()))
    assert index.build() == 12
    assert index.count() == 19 + (2 if later_already_indexed else 0)
    assert index.stamp() == last
    assert len(index.search_blocks("needle18")) == 1
    assert len(pages) == 3  # Remaining rows cross two pages, then end detection.
    assert len(prefix_pages) == 2  # Seven committed segments, then the gap.
    assert any("CREATE TEMP TABLE _fts_legacy_store_keys" in sql for sql in statements)
    assert any("INSERT OR IGNORE INTO temp._fts_legacy_store_keys" in sql for sql in statements)
    connection = sqlite3.connect(index.db_path)
    try:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = '_fts_legacy_store_keys'"
        ).fetchone()
    finally:
        connection.close()
