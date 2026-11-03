"""Real index pagination is bounded work and preserves resumable key semantics."""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.search_index import source_store
from bili_asr.services.archive_migration import initialize_archive
from tests.test_workflow_control_plane import _seed_part


class _MeasuredConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        if sql.startswith("SELECT ") and "FROM transcript_segments " in sql:
            self.selection_queries.append((sql, parameters))
        return super().execute(sql, parameters)


def _transcript(connection, identity, segments, *, part_id=1):
    connection.execute(
        "INSERT INTO transcripts(transcript_id,video_part_id,source_kind,language,version,content_sha256,created_at) "
        "VALUES(?,?,'subtitle-cc','en',?,?,0)", (identity, part_id, identity, f"{identity:064x}"))
    connection.executemany("INSERT INTO transcript_segments VALUES(?,?,?,?,?)",
        ((identity, ordinal, ordinal * 1000, ordinal * 1000 + 900, "searchable words")
         for ordinal in range(segments)))


def test_build_selection_work_scales_with_segments_without_repeated_tail_sort(tmp_path):
    instructions = []
    plans = []
    for transcript_count in (20, 80):
        root = tmp_path / str(transcript_count)
        initialize_archive(root)
        with closing(sqlite3.connect(root / "archive.db", factory=_MeasuredConnection)) as connection:
            connection.row_factory = sqlite3.Row
            connection.selection_queries = []
            with connection:
                _seed_part(connection)
                for identity in range(1, transcript_count + 1):
                    _transcript(connection, identity, 200)
            work = [0]

            def progress(work=work):
                work[0] += 1000
                return 0

            connection.set_progress_handler(progress, 1000)
            try:
                assert source_store.build(connection) == transcript_count * 200
            finally:
                connection.set_progress_handler(None, 0)
            assert work[0] > 0
            instructions.append(work[0])
            assert source_store.count(connection) == transcript_count * 200
            assert len(connection.selection_queries) == transcript_count * 200 // 500 + 1
            sql, parameters = connection.selection_queries[1]
            plans.extend(row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters))
    # Four times the input must not repeatedly scan/sort each remaining tail.
    # VM instructions measure deterministic database work rather than wall time.
    assert instructions[1] < instructions[0] * 6, instructions
    assert not any("TEMP B-TREE" in detail for detail in plans), plans


def test_build_resumes_committed_pages_and_fills_earlier_ledger_holes(tmp_path):
    root = tmp_path / "archive"
    initialize_archive(root)
    with ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session:
        connection = session.connection
        with connection:
            _seed_part(connection)
            _transcript(connection, 1, 600)
            _transcript(connection, 2, 20)
        # The interruption is in the FTS/ledger transaction, after one committed
        # page and part of the next page. Neither half may be falsely completed.
        source_store.build(connection)  # Initialize cache tables, then clear them.
        with connection:
            connection.execute(f"DELETE FROM {source_store.TABLE}")
            connection.execute(f"DELETE FROM {source_store.KEYS}")
            connection.execute(f"DELETE FROM {source_store.META}")
            connection.execute(f"CREATE TRIGGER interrupt_index BEFORE INSERT ON {source_store.KEYS} "
                               "WHEN NEW.transcript_id=1 AND NEW.ordinal=550 "
                               "BEGIN SELECT RAISE(ABORT,'injected batch interruption'); END")
        with pytest.raises(sqlite3.IntegrityError, match="injected batch interruption"):
            source_store.build(connection)
        assert source_store.count(connection) == 500
        assert source_store.stamp(connection) == -1
        with connection:
            connection.execute("DROP TRIGGER interrupt_index")
        assert source_store.build(connection) == 120
        with connection:
            connection.execute(f"DELETE FROM {source_store.TABLE} WHERE block_key='t1:7'")
            connection.execute(f"DELETE FROM {source_store.KEYS} WHERE transcript_id=1 AND ordinal=7")
        assert source_store.build(connection) == 1
        assert source_store.build(connection) == 0
        assert source_store.count(connection) == 620
        assert connection.execute(f"SELECT COUNT(DISTINCT block_key) FROM {source_store.TABLE}").fetchone()[0] == 620


def test_build_keeps_upper_bound_and_inner_join_semantics(tmp_path):
    root = tmp_path / "archive"
    initialize_archive(root)
    with ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session:
        connection = session.connection
        # Model an unavailable source reference: pagination must still retain
        # the original inner-join behavior and continue beyond its segment key.
        connection.execute("PRAGMA foreign_keys=OFF")
        with connection:
            _seed_part(connection)
            _transcript(connection, 1, 501)
            _transcript(connection, 2, 7, part_id=999)
            _transcript(connection, 3, 20)
        connection.execute("PRAGMA foreign_keys=ON")
        source_store.build(connection)
        with connection:
            connection.execute(f"DELETE FROM {source_store.TABLE}")
            connection.execute(f"DELETE FROM {source_store.KEYS}")
            connection.execute(f"DELETE FROM {source_store.META}")
            connection.execute(f"CREATE TRIGGER arrive_during_index AFTER INSERT ON {source_store.KEYS} "
                "WHEN NEW.transcript_id=1 AND NEW.ordinal=499 BEGIN "
                "INSERT INTO transcripts VALUES(4,1,'subtitle-cc','en',NULL,4,'" + "4" * 64 + "',0); "
                "INSERT INTO transcript_segments VALUES(4,0,0,900,'later transcript'); END")
        assert source_store.build(connection) == 521
        assert source_store.stamp(connection) == 3
        assert connection.execute(f"SELECT 1 FROM {source_store.KEYS} WHERE transcript_id IN (2,4)").fetchone() is None
        assert source_store.build(connection) == 1
        assert source_store.stamp(connection) == 4
        assert source_store.count(connection) == 522
