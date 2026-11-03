"""Store FTS indexing resumes at committed segments after interrupted batches."""

import bili_asr.search_index.common as _module_search_index_common
import bili_asr.search_index.errors as _module_search_index_errors


import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from bili_asr import search_index
from bili_asr.search_index.store import TranscriptSearchIndex
from bili_asr.storage import TranscriptRepository, open_database
from tests.support.transcript_repository import _record, _run, _video_with_parts


pytestmark = pytest.mark.skipif(
    not _module_search_index_common.check_fts5_available(), reason="SQLite FTS5 unavailable"
)


def _transcript(root, bvid, segment_count, token):
    connection = open_database(root)
    try:
        cid = connection.execute("SELECT COALESCE(MAX(cid), 9200) + 1 FROM video_parts").fetchone()[0]
        part_id = _video_with_parts(connection, bvid, (cid,))[0]
        repository = TranscriptRepository(connection)
        run_id = _run(repository, connection.execute("SELECT COUNT(*) + 1 FROM acquisition_runs").fetchone()[0])
        result = _record(
            repository, part_id, run_id=run_id,
            body=tuple((i * 1000, (i + 1) * 1000, f"{token}{i}")
                       for i in range(segment_count)),
        )
        return result.transcript_id
    finally:
        connection.close()


def _interrupt_redaction(monkeypatch, after_calls):
    redact = _module_search_index_common._redact_text
    calls = 0

    def interrupt(text):
        nonlocal calls
        calls += 1
        if calls > after_calls:
            raise RuntimeError("injected indexing interruption")
        return redact(text)

    monkeypatch.setattr(_module_search_index_common, "_redact_text", interrupt)
    return redact


@pytest.mark.parametrize("committed_segments", [0, 500, 1000])
def test_incomplete_transcript_resumes_without_skipping_or_duplicating_committed_blocks(
    tmp_root, monkeypatch, committed_segments,
):
    total = max(501, committed_segments + 1)
    transcript_id = _transcript(tmp_root, "BVrecovery", total, "needle")
    index = TranscriptSearchIndex(tmp_root)
    redact = _interrupt_redaction(monkeypatch, max(100, committed_segments))

    with pytest.raises(RuntimeError, match="injected"):
        index.build()
    assert index.count() == committed_segments
    assert index.stamp() == -1
    assert index.search_blocks(f"needle{total - 1}") == []

    monkeypatch.setattr(_module_search_index_common, "_redact_text", redact)
    assert index.build() == total - committed_segments
    assert index.count() == total
    assert index.stamp() == transcript_id
    assert index.metadata()["indexed_count"] == str(total)
    assert len(index.search_blocks(f"needle{total - 1}")) == 1
    assert index.build() == 0


def test_completed_transcripts_and_build_metadata_survive_interruption_of_the_next(
    tmp_root, monkeypatch,
):
    first = _transcript(tmp_root, "BVfirst", 2, "first")
    index = TranscriptSearchIndex(tmp_root)
    assert index.build() == 2
    previous_metadata = index.metadata()
    _transcript(tmp_root, "BVsecond", 501, "second")
    last = _transcript(tmp_root, "BVthird", 2, "third")
    redact = _interrupt_redaction(monkeypatch, 500)
    with pytest.raises(RuntimeError, match="injected"):
        index.build()
    assert index.stamp() == first
    assert index.count() == 502
    assert index.metadata()["indexed_count"] == previous_metadata["indexed_count"]
    monkeypatch.setattr(_module_search_index_common, "_redact_text", redact)
    assert len(index.search_blocks("first1")) == 1
    assert index.build() == 3
    assert index.stamp() == last
    assert index.count() == 505
    assert index.metadata()["indexed_count"] == "505"
    assert len(index.search_blocks("second500")) == 1
    assert len(index.search_blocks("third1")) == 1


def test_exact_batch_end_is_a_complete_transcript_boundary(tmp_root, monkeypatch):
    first = _transcript(tmp_root, "BVexactbatch", 500, "boundary")
    last = _transcript(tmp_root, "BVafterbatch", 2, "later")
    index = TranscriptSearchIndex(tmp_root)
    redact = _interrupt_redaction(monkeypatch, 500)
    with pytest.raises(RuntimeError, match="injected"):
        index.build()
    assert index.count() == 500
    assert index.stamp() == first
    monkeypatch.setattr(_module_search_index_common, "_redact_text", redact)
    assert index.build() == 2
    assert index.count() == 502
    assert index.stamp() == last


def test_legacy_partial_index_recovers_a_verified_segment_prefix(tmp_root, monkeypatch):
    transcript_id = _transcript(tmp_root, "BVoldpartial", 501, "legacy")
    index = TranscriptSearchIndex(tmp_root)
    redact = _interrupt_redaction(monkeypatch, 500)
    with pytest.raises(RuntimeError, match="injected"):
        index.build()
    with sqlite3.connect(index.db_path) as connection:
        # Old builds stored only aggregate metadata, with progress inferred
        # from the largest transcript id present in the FTS table.
        connection.execute(
            "DELETE FROM transcript_fts_index_meta "
            "WHERE key NOT IN ('indexed_count', 'built_at')"
        )
    assert index.stamp() == -1
    monkeypatch.setattr(_module_search_index_common, "_redact_text", redact)
    assert index.build() == 1
    assert index.count() == 501
    assert index.stamp() == transcript_id
    assert len(index.search_blocks("legacy500")) == 1


_DIE_AFTER_BATCH = """
import os
import sys
from bili_asr.search_index import common
redact = common._redact_text
calls = 0
def die(text):
    global calls
    calls += 1
    if calls == 501:
        os._exit(87)
    return redact(text)
common._redact_text = die
from bili_asr import search_index
search_index.TranscriptSearchIndex(sys.argv[1]).build()
"""


def test_abrupt_process_exit_preserves_only_the_committed_segment_cursor(tmp_root):
    transcript_id = _transcript(tmp_root, "BVhardexit", 501, "hardexit")
    package_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", _DIE_AFTER_BATCH, str(tmp_root)],
        cwd=package_root, env=dict(os.environ, PYTHONPATH=str(package_root / "src")),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 87, (result.stdout, result.stderr)
    index = TranscriptSearchIndex(tmp_root)
    assert index.count() == 500
    assert index.stamp() == -1
    assert index.build() == 1
    assert index.count() == 501
    assert index.stamp() == transcript_id
    assert len(index.search_blocks("hardexit500")) == 1


def test_force_keeps_completed_blocks_idempotent(tmp_root):
    transcript_id = _transcript(tmp_root, "BVforced", 2, "forced")
    index = TranscriptSearchIndex(tmp_root)
    assert index.build() == 2
    assert index.build(force=True) == 0
    assert index.count() == 2
    assert index.stamp() == transcript_id
    assert len(index.search_blocks("forced1")) == 1


def test_legacy_gap_recovery_preserves_later_completed_transcripts(tmp_root, monkeypatch):
    first = _transcript(tmp_root, "BVoldgap", 501, "oldgap")
    index = TranscriptSearchIndex(tmp_root)
    redact = _interrupt_redaction(monkeypatch, 500)
    with pytest.raises(RuntimeError, match="injected"):
        index.build()
    last = _transcript(tmp_root, "BVoldlater", 2, "oldlater")
    with sqlite3.connect(index.db_path) as connection:
        # Earlier builds skipped the interrupted transcript on the next run,
        # but still appended later transcripts. Preserve those complete rows.
        connection.execute(
            "INSERT INTO transcript_fts "
            "(block_key, bvid, page_index, start_ms, end_ms, pubdate, text, bigram, source) "
            "SELECT 't' || ts.transcript_id || ':' || ts.ordinal, vp.bvid, vp.page_index, "
            "ts.start_ms, ts.end_ms, vd.pubdate, ts.text, ts.text, 'store' "
            "FROM transcript_segments ts JOIN transcripts t ON t.transcript_id = ts.transcript_id "
            "JOIN video_parts vp ON vp.video_part_id = t.video_part_id "
            "JOIN videos vd ON vd.bvid = vp.bvid WHERE t.transcript_id = ?",
            (last,),
        )
        connection.execute(
            "DELETE FROM transcript_fts_index_meta "
            "WHERE key NOT IN ('indexed_count', 'built_at')"
        )
    assert first < last
    assert index.count() == 502
    assert index.stamp() == -1
    monkeypatch.setattr(_module_search_index_common, "_redact_text", redact)
    assert index.build() == 1
    assert index.count() == 503
    assert index.stamp() == last
    assert len(index.search_blocks("oldgap500")) == 1
    assert len(index.search_blocks("oldlater1")) == 1
    assert index.build() == 0


def test_complete_legacy_index_gains_progress_without_reinserting_blocks(tmp_root):
    transcript_id = _transcript(tmp_root, "BVoldcomplete", 2, "oldcomplete")
    index = TranscriptSearchIndex(tmp_root)
    assert index.build() == 2
    with sqlite3.connect(index.db_path) as connection:
        connection.execute(
            "DELETE FROM transcript_fts_index_meta "
            "WHERE key NOT IN ('indexed_count', 'built_at')"
        )
    assert index.stamp() == transcript_id
    assert index.build() == 0
    assert index.count() == 2
    assert index.stamp() == transcript_id
    assert index.metadata()["indexed_count"] == "2"


@pytest.mark.parametrize("committed_before_error", [False, True])
def test_batch_data_and_resume_cursor_share_the_same_commit(
    tmp_root, monkeypatch, committed_before_error,
):
    transcript_id = _transcript(tmp_root, "BVcommitfault", 501, "commitfault")
    index = TranscriptSearchIndex(tmp_root)
    connect = index._connect_for_build

    class FailingCommit:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            count = self.connection.execute("SELECT COUNT(*) FROM transcript_fts").fetchone()[0]
            if count == 500:
                if committed_before_error:
                    self.connection.commit()
                raise sqlite3.OperationalError("injected batch commit fault")
            self.connection.commit()

    monkeypatch.setattr(index, "_connect_for_build", lambda: FailingCommit(connect()))
    with pytest.raises(_module_search_index_errors.TranscriptStoreError, match="injected batch commit"):
        index.build()
    assert index.count() == (500 if committed_before_error else 0)
    assert index.stamp() == -1
    monkeypatch.setattr(index, "_connect_for_build", connect)
    assert index.build() == (1 if committed_before_error else 501)
    assert index.count() == 501
    assert index.stamp() == transcript_id
    assert len(index.search_blocks("commitfault500")) == 1
