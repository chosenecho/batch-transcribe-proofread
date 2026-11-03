"""Store-backed FTS5 transcript search: index builder, query command, exit contract.

The migrated search layer indexes the transcript store (``archive.db``) instead
of the manifest: ``search-index`` creates ``transcript_fts`` inside the store
behind a gated FTS migration shim (never from ``open_database``), and ``search``
queries it read-only.  Exit codes follow the two-class contract
(``iter-2026-09-coverage-truth/specs/exit-code-contract.md`` §1–§2): a missing
index is backlog-class — exit 0 with an explicit "index missing" hint; store
corruption is defect-class — exit 1.
"""

from __future__ import annotations

import bili_asr.asr.config as _module_asr_config
import bili_asr.asr.runner as _module_asr_runner
import bili_asr.cli.main as _module_cli_main
import bili_asr.search_index.constants as _module_search_index_constants
import bili_asr.search_index.errors as _module_search_index_errors
import bili_asr.search_index.store as _module_search_index_store


from dataclasses import replace
import json
import sqlite3
from pathlib import Path

import pytest

from bili_asr import cli
from bili_asr.search_index.store import TranscriptSearchIndex
from bili_asr.search_index.common import check_fts5_available
from bili_asr.storage import (
    AcquisitionRunRecord,
    MetadataRepository,
    TranscriptRepository,
    TranscriptSegmentRecord,
    open_database,
)
from tests.fixtures.metadata_records import (
    make_part_record,
    make_user_record,
    make_video_record,
)

pytestmark = pytest.mark.skipif(
    not check_fts5_available(), reason="SQLite FTS5 unavailable in this environment"
)

# 2020-09-13 and 2020-09-14 as unix seconds (UTC).
PUBDATE_A = 1_600_000_000
PUBDATE_B = 1_600_086_400


def _run_record(run_id: str) -> AcquisitionRunRecord:
    return AcquisitionRunRecord(
        run_id=run_id,
        kind="subtitle",
        selector_kind="pending",
        selector_target=None,
        requested_limit=None,
        credential_present=False,
        started_at=100,
        outcome="running",
        finished_at=None,
    )


def _segments(*triples: tuple[int, int, str]) -> tuple[TranscriptSegmentRecord, ...]:
    return tuple(TranscriptSegmentRecord(*triple) for triple in triples)


def _store_part(
    root: Path,
    connection,
    repository: TranscriptRepository,
    *,
    bvid: str,
    page_index: int,
    cid: int,
    pubdate: int,
    video_title: str,
    part_title: str,
    segments: tuple[TranscriptSegmentRecord, ...],
    source_kind: str = "subtitle-ai",
) -> None:
    """Store one video/part plus one transcript version for it."""
    metadata = MetadataRepository(connection)
    with metadata.transaction():
        metadata.upsert_user(make_user_record())
        metadata.upsert_video(
            replace(
                make_video_record(bvid, aid=None, title=video_title),
                pubdate=pubdate,
            )
        )
        metadata.upsert_part(
            make_part_record(
                bvid,
                page_index=page_index,
                cid=cid,
                title=part_title,
                processing_status="metadata_collected",
            )
        )
    part_id = int(
        connection.execute(
            "SELECT video_part_id FROM video_parts WHERE bvid = ? AND page_index = ?",
            (bvid, page_index),
        ).fetchone()["video_part_id"]
    )
    repository.record_acquired_transcript(
        run_id="search-fixture",
        video_part_id=part_id,
        source_kind=source_kind,
        language="zh-CN",
        segments=segments,
        started_at=200,
        finished_at=300,
        created_at=400,
    )


@pytest.fixture()
def indexed_store(tmp_path: Path) -> Path:
    """A store holding three parts (two videos, one multi-part) with known text."""
    connection = open_database(tmp_path)
    try:
        repository = TranscriptRepository(connection)
        repository.start_acquisition_run(_run_record("search-fixture"))

        _store_part(
            tmp_path, connection, repository,
            bvid="BV1alpha", page_index=0, cid=101, pubdate=PUBDATE_A,
            video_title="黑格尔辩证法第一讲", part_title="P1",
            segments=_segments(
                (0, 1_200, "今天我们讲黑格尔"),
                (1_200, 2_400, "辩证法的核心是否定之否定"),
            ),
        )
        _store_part(
            tmp_path, connection, repository,
            bvid="BV1beta", page_index=0, cid=201, pubdate=PUBDATE_B,
            video_title="康德先验哲学", part_title="P1",
            segments=_segments(
                (0, 1_200, "康德先验感性论"),
                (1_200, 2_400, "自在之物不可知"),
            ),
        )
        _store_part(
            tmp_path, connection, repository,
            bvid="BV1beta", page_index=1, cid=202, pubdate=PUBDATE_B,
            video_title="康德先验哲学", part_title="P2",
            segments=_segments(
                (0, 1_200, "黑格尔批评康德"),
                (1_200, 2_400, "辩证法在这里重新开始"),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    index = TranscriptSearchIndex(tmp_path)
    indexed = index.build()
    assert indexed == 6  # 3 parts x 2 time-bounded blocks each
    return tmp_path


def _argv(root: Path, *extra: str) -> list[str]:
    return ["search", "--archive-root", str(root), *extra]


# ---------------------------------------------------------------------------
# Index builder
# ---------------------------------------------------------------------------

def test_search_index_builds_table_inside_archive_db(indexed_store: Path) -> None:
    conn = sqlite3_connect_plain(indexed_store / "archive.db")
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
            )
        }
        assert "transcript_fts" in tables
        assert "transcript_fts_index_meta" in tables
        count = conn.execute("SELECT COUNT(*) FROM transcript_fts").fetchone()[0]
        assert count == 6
        sources = {
            row[0]
            for row in conn.execute("SELECT DISTINCT source FROM transcript_fts")
        }
        # every fixture part is served by the store, so every row is stamped 'store'
        assert sources == {"store"}
    finally:
        conn.close()


def test_search_index_idempotent_and_incremental(indexed_store: Path) -> None:
    """Re-running changes nothing; a new transcript is picked up incrementally."""
    index = TranscriptSearchIndex(indexed_store)
    first = index.build()
    assert first == 0  # the fixture already built the index
    assert index.metadata()["indexed_count"] == "6"
    assert index.metadata().get("built_at")
    stamp_after_first = index.stamp()

    again = index.build()
    assert again == 0
    assert index.stamp() == stamp_after_first

    # Add one more transcript version on a new part; incremental build appends it.
    connection = open_database(indexed_store)
    try:
        repository = TranscriptRepository(connection)
        _store_part(
            indexed_store, connection, repository,
            bvid="BV1gamma", page_index=0, cid=301, pubdate=PUBDATE_B,
            video_title="费希特知识学", part_title="P1",
            segments=_segments((0, 1_200, "费希特谈自我意识")),
        )
        connection.commit()
    finally:
        connection.close()

    added = index.build()
    assert added == 1  # one new part with one segment
    assert index.stamp() > stamp_after_first
    assert index.count() == 7
    assert index.metadata()["indexed_count"] == "7"


def test_search_index_metadata_is_empty_before_first_build(tmp_path: Path) -> None:
    connection = open_database(tmp_path)
    connection.close()
    index = TranscriptSearchIndex(tmp_path)
    assert index.metadata() == {}
    assert index.count() == 0


def test_search_index_blocks_served_from_store_and_bigram_aux(indexed_store: Path) -> None:
    """A block is a time-bounded segment row; CJK text lands in the bigram column."""
    index = TranscriptSearchIndex(indexed_store)
    hits = index.search_blocks("黑格尔")
    assert len(hits) == 2  # BV1alpha P1 and BV1beta P1
    by_part = {(h.bvid, h.page_index) for h in hits}
    assert by_part == {("BV1alpha", 0), ("BV1beta", 1)}
    for hit in hits:
        assert hit.start_ms >= 0
        assert hit.end_ms > hit.start_ms
        assert hit.source == "store"
        assert hit.pubdate is not None


# ---------------------------------------------------------------------------
# Query command: phrase, pubdate window, formats
# ---------------------------------------------------------------------------


class _CountingConnection:
    """sqlite3.Connection stand-in that counts execute() calls by SQL verb."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.counts: dict[str, int] = {}

    def execute(self, sql, parameters=(), /):
        verb = str(sql).strip().split(None, 1)[0].upper()
        self.counts[verb] = self.counts.get(verb, 0) + 1
        return self._real.execute(sql, parameters)

    def close(self) -> None:
        self._real.close()


def test_search_blocks_issues_a_bounded_number_of_queries(indexed_store: Path) -> None:
    """One snippet query for ALL hit block_keys + one bounded title scan (O-R3)."""
    import sqlite3 as _sqlite3

    from bili_asr import search_index

    counting = _CountingConnection(_sqlite3.connect(indexed_store / "archive.db"))

    def counting_connect(self):
        return counting

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            _module_search_index_store.TranscriptSearchIndex, "_connect", counting_connect
        )
        hits = _module_search_index_store.TranscriptSearchIndex(indexed_store).search_blocks("黑格尔")
    finally:
        monkeypatch.undo()
        counting.close()

    assert len(hits) == 2  # K hits …
    selects = counting.counts.get("SELECT", 0)
    # … but a constant number of SELECTs: 1 (index probe) + 1 (MATCH) +
    # 1 (batched snippets) + 1 (bounded titles), never 1-per-hit.
    assert selects <= 4, f"expected a bounded query count, got {selects}"
    for hit in hits:
        assert hit.snippet
        assert hit.video_title

def test_search_finds_phrase_with_bvid_time_range_and_pubdate(
    indexed_store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _module_cli_main.main(_argv(indexed_store, "否定之否定")) == 0
    out = capsys.readouterr().out
    assert "BV1alpha" in out
    assert "黑格尔辩证法第一讲" in out
    assert "00:00:01,200" in out  # 1_200 ms start of the matching block
    assert "2020-09-13" in out  # PUBDATE_A rendered as a date


def test_search_pubdate_filters_exclude_out_of_window_hits(
    indexed_store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # "黑格尔" matches BV1alpha (pubdate A) and BV1beta P1 (pubdate B).
    assert _module_cli_main.main(_argv(indexed_store, "黑格尔", "--from", "2020-09-14")) == 0
    out = capsys.readouterr().out
    assert "BV1beta" in out
    assert "BV1alpha" not in out

    assert _module_cli_main.main(_argv(indexed_store, "黑格尔", "--to", "2020-09-13")) == 0
    out = capsys.readouterr().out
    assert "BV1alpha" in out
    assert "BV1beta" not in out

    # An exclusive window on a phrase with hits elsewhere: explicit "no hits", exit 0.
    assert _module_cli_main.main(_argv(indexed_store, "黑格尔", "--from", "2021-01-01")) == 0
    captured = capsys.readouterr()
    assert "no hits" in captured.out


def test_search_default_limit_is_20(
    indexed_store: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from bili_asr import search_index

    seen: list[int | None] = []

    original = _module_search_index_store.TranscriptSearchIndex.search_blocks

    def spy(self, query, *, pubdate_from=None, pubdate_to=None, limit=None):
        seen.append(limit)
        return original(self, query, pubdate_from=pubdate_from,
                        pubdate_to=pubdate_to, limit=limit)

    monkeypatch.setattr(_module_search_index_store.TranscriptSearchIndex, "search_blocks", spy)
    assert _module_cli_main.main(_argv(indexed_store, "黑格尔")) == 0
    capsys.readouterr()
    assert seen == [20]


def test_search_json_format(indexed_store: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _module_cli_main.main(_argv(indexed_store, "否定之否定", "--format", "json")) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert len(payload) == 1
    row = payload[0]
    assert row["bvid"] == "BV1alpha"
    assert row["page_index"] == 0
    assert row["start_ms"] == 1_200
    assert row["end_ms"] == 2_400
    assert row["pubdate"] == PUBDATE_A
    assert row["source"] == "store"
    assert "否定之否定" in row["text"]


# ---------------------------------------------------------------------------
# search-index command
# ---------------------------------------------------------------------------

def test_search_index_command_builds_and_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _module_cli_main.main(["search-index", "--archive-root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "indexed 0 block(s); total 0" in out
    assert (tmp_path / "archive.db").is_file()

    assert _module_cli_main.main(["search-index", "--archive-root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "indexed 0 block(s); total 0" in out  # idempotent re-run changes nothing


def test_search_index_never_creates_index_on_search_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Read paths never auto-create: search-index alone owns table creation."""
    # Merely opening the store (any search attempt) must not create the FTS table.
    assert _module_cli_main.main(_argv(tmp_path, "黑格尔")) == 0
    captured = capsys.readouterr()
    assert "index missing" in captured.out
    assert "search-index" in captured.out
    conn = sqlite3_connect_plain(tmp_path / "archive.db")
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "transcript_fts" not in tables
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Exit contract (exit-code-contract §1–§2 applied to the index)
# ---------------------------------------------------------------------------

def test_search_no_hits_exits_zero_with_explicit_line(
    indexed_store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _module_cli_main.main(_argv(indexed_store, "谢林")) == 0
    out = capsys.readouterr().out
    assert "no hits" in out


def test_search_corrupt_store_is_defect_exit_1(
    indexed_store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (indexed_store / "archive.db").write_bytes(b"this is not sqlite")
    assert _module_cli_main.main(_argv(indexed_store, "黑格尔")) == 1
    err = capsys.readouterr().err
    assert "store" in err or "corrupt" in err


def test_search_corrupt_videos_read_is_defect_not_silent_empty_titles(
    indexed_store: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-read ``videos`` corruption stays defect-class (exit 1), not empty titles.

    The batched title read moved into ``_titles_for_bvids``; it must not swallow
    ``sqlite3.DatabaseError`` the way the snippet batch may, because the base
    behaviour (read inline under ``search_blocks``) raised
    :class:`TranscriptStoreError` (exit-code-contract §2, defect class).  The
    rest of the read path — index probe, MATCH, batched snippets — succeeds
    here, so the failure genuinely lands mid-read.
    """
    import sqlite3 as _sqlite3

    from bili_asr import search_index

    real = _sqlite3.connect(indexed_store / "archive.db")
    reached: list[str] = []

    class _VideosReadFails:
        def execute(self, sql, parameters=(), /):
            text = str(sql)
            if "FROM videos" in text:
                reached.append("videos")
                raise _sqlite3.DatabaseError("database disk image is malformed")
            if "snippet(" in text:
                reached.append("snippets")
            return real.execute(sql, parameters)

        def close(self) -> None:
            real.close()

    monkeypatch.setattr(
        _module_search_index_store.TranscriptSearchIndex, "_connect", lambda self: _VideosReadFails()
    )
    with pytest.raises(_module_search_index_errors.TranscriptStoreError):
        _module_search_index_store.TranscriptSearchIndex(indexed_store).search_blocks("黑格尔")
    real.close()
    # The failure is mid-read, after the snippets batch: not a connect/probe failure.
    assert reached == ["snippets", "videos"]


def test_search_usage_error_is_exit_2(indexed_store: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        _module_cli_main.main(_argv(indexed_store, "黑格尔", "--from", "2020-09-31"))
    assert exc.value.code == 2


def test_search_limit_validation_is_usage_error_exit_2(indexed_store: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        _module_cli_main.main(_argv(indexed_store, "黑格尔", "--limit", "0"))
    assert exc.value.code == 2


def test_search_index_on_corrupt_store_is_defect_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "archive.db").write_bytes(b"this is not sqlite")
    assert _module_cli_main.main(["search-index", "--archive-root", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "store" in err or "corrupt" in err


def test_search_index_without_fts5_refuses_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from bili_asr import search_index

    monkeypatch.setattr(
        search_index, "check_fts5_available", lambda conn=None: False
    )
    assert _module_cli_main.main(["search-index", "--archive-root", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "FTS5" in err
    conn = sqlite3_connect_plain(tmp_path / "archive.db")
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "transcript_fts" not in tables
    finally:
        conn.close()


def sqlite3_connect_plain(path: Path):
    import sqlite3

    return sqlite3.connect(path)


def test_mixed_md_and_store_keys_do_not_pollute_the_incremental_stamp(tmp_path):
    """QC F1 regression: a published-md row must not skip store transcripts."""
    from bili_asr import search_index

    root = tmp_path
    connection = open_database(root)
    try:
        repository = TranscriptRepository(connection)
        repository.start_acquisition_run(_run_record("search-fixture"))
        _store_part(
            root, connection, repository,
            bvid="BVA", page_index=0, cid=1, pubdate=1,
            video_title="video A", part_title="A p0",
            segments=_segments((0, 500, "store text A")),
        )
        connection.commit()
    finally:
        connection.close()

    index = _module_search_index_store.TranscriptSearchIndex(root)
    assert index.build() == 1

    # a second part the store has metadata for but no transcript: the md fallback
    from bili_asr.storage import MetadataRepository

    connection = open_database(root)
    try:
        metadata = MetadataRepository(connection)
        with metadata.transaction():
            metadata.upsert_video(
                replace(make_video_record("BVB", aid=None, title="video B"), pubdate=2)
            )
            metadata.upsert_part(
                make_part_record(
                    "BVB", page_index=0, cid=2, title="B p0",
                    processing_status="metadata_collected",
                )
            )
        connection.commit()
    finally:
        connection.close()
    (root / "transcripts" / "BVB.p0").mkdir(parents=True, exist_ok=True)
    (root / "transcripts" / "BVB.p0" / "bundle.md").write_text(
        "plain text body for BVB", encoding="utf-8"
    )
    assert index.build() == 1

    # a NEW store transcript for BVB must not be skipped by the md row's id
    connection = open_database(root)
    try:
        repository = TranscriptRepository(connection)
        part_b = int(connection.execute(
            "SELECT video_part_id FROM video_parts WHERE bvid = 'BVB'"
        ).fetchone()["video_part_id"])
        repository.record_acquired_transcript(
            run_id="search-fixture",
            video_part_id=part_b,
            source_kind="subtitle-ai",
            language="zh",
            segments=_segments((0, 500, "store text B")),
            started_at=2,
            finished_at=2,
            created_at=2,
        )
        connection.commit()
    finally:
        connection.close()
    assert index.build() == 1, "store transcript for BVB was silently skipped (F1)"



def test_skip_stamped_parts_before_probe_on_rebuild(tmp_path, monkeypatch):
    """Plan 005: a rebuild with zero new transcripts must not re-probe/re-read
    markdown for already-stamped parts — the ``stamped_part_ids`` anti-join is
    pushed into the candidate SQL, so the filesystem probe never runs."""
    from bili_asr import search_index

    root = tmp_path
    connection = open_database(root)
    try:
        repository = TranscriptRepository(connection)
        repository.start_acquisition_run(_run_record("search-fixture"))
        _store_part(
            root, connection, repository,
            bvid="BVA", page_index=0, cid=1, pubdate=1,
            video_title="video A", part_title="A p0",
            segments=_segments((0, 500, "store text A")),
        )
        connection.commit()
    finally:
        connection.close()

    # part B: metadata only (no store transcript) — served by the md fallback
    from bili_asr.storage import MetadataRepository

    connection = open_database(root)
    try:
        metadata = MetadataRepository(connection)
        with metadata.transaction():
            metadata.upsert_video(
                replace(make_video_record("BVB", aid=None, title="video B"), pubdate=2)
            )
            metadata.upsert_part(
                make_part_record(
                    "BVB", page_index=0, cid=2, title="B p0",
                    processing_status="metadata_collected",
                )
            )
        connection.commit()
    finally:
        connection.close()
    (root / "transcripts" / "BVB.p0").mkdir(parents=True, exist_ok=True)
    (root / "transcripts" / "BVB.p0" / "bundle.md").write_text(
        "00:00:00\nplain text body for BVB\n", encoding="utf-8"
    )

    index = _module_search_index_store.TranscriptSearchIndex(root)
    assert index.build() == 2  # A from the store, B from the published md

    probed: list[tuple[str, int]] = []
    original = _module_search_index_store.TranscriptSearchIndex._published_md_text_for

    def spy(self, bvid, page_index):
        probed.append((bvid, page_index))
        return original(self, bvid, page_index)

    monkeypatch.setattr(
        _module_search_index_store.TranscriptSearchIndex, "_published_md_text_for", spy
    )
    assert index.build() == 0  # nothing new: zero appended blocks
    # already-stamped parts (B from md, A excluded as having a store transcript)
    # must never reach the filesystem probe on a rebuild.
    assert probed == []



def test_pass_two_runs_only_when_kept_tokens_exist():
    """QC F2 regression: an empty kept list means pass 2 cannot change output."""
    from bili_asr import asr

    cfg = _module_asr_config.ASRConfig(model_name="m")
    runner = _module_asr_runner.ASRRunner(cfg)
    kept = runner.rebuild_hotwords_from_first_pass("一段没有任何热词的转写文本。")
    assert kept == []


def test_search_zero_hit_on_corrupt_store_is_still_defect(
    indexed_store: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero-hit query on a corrupt store stays defect-class (exit 1).

    The zero-hit early return must not sit ahead of the store-readability
    proof: a damaged ``videos`` table read as a clean empty result, where the
    pre-batching eager title read raised :class:`TranscriptStoreError`.
    """
    import sqlite3 as _sqlite3

    from bili_asr import search_index

    real = _sqlite3.connect(indexed_store / "archive.db")
    real.execute("DROP TABLE videos")
    real.commit()

    class _Connect:
        def connect(self):
            return real

    monkeypatch.setattr(
        _module_search_index_store.TranscriptSearchIndex, "_connect", lambda self: real
    )
    with pytest.raises(_module_search_index_errors.TranscriptStoreError):
        _module_search_index_store.TranscriptSearchIndex(indexed_store).search_blocks("zzzz-nothing")
    real.close()


def test_search_zero_hit_on_videos_schema_drift_is_still_defect(
    indexed_store: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The zero-hit probe must force the decode the title path performs.

    A ``bvid``-only probe is answered from the covering index, so a ``videos``
    shape defect (``title`` renamed/dropped — an ordinary schema-drift repair)
    stayed invisible on the zero-hit path: base exited 1, the narrow probe
    exited 0.  Reading both columns restores the count-independent defect class.
    """
    import sqlite3 as _sqlite3

    from bili_asr import search_index

    real = _sqlite3.connect(indexed_store / "archive.db")
    real.execute("ALTER TABLE videos RENAME COLUMN title TO title_x")
    real.commit()

    monkeypatch.setattr(
        _module_search_index_store.TranscriptSearchIndex, "_connect", lambda self: real
    )
    with pytest.raises(_module_search_index_errors.TranscriptStoreError):
        _module_search_index_store.TranscriptSearchIndex(indexed_store).search_blocks("zzzz-nothing")
    real.close()


def test_search_unusable_fts_with_damaged_videos_is_still_defect(
    indexed_store: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The FTS retry-exhaustion exit must also prove store readability.

    ``search_blocks`` has two clean-empty exits.  The FTS double-failure return
    sat ahead of the readability proof, so an unusable ``transcript_fts`` plus a
    damaged ``videos`` reported a clean empty result (exit 0) where base exited
    1 — the defect class must not depend on which failure mode was hit.
    """
    import sqlite3 as _sqlite3

    from bili_asr import search_index

    real = _sqlite3.connect(indexed_store / "archive.db")
    real.execute("DROP TABLE videos")
    # Make every MATCH raise: replace the FTS table with a plain (non-FTS) one
    # of the same name, so both the query and its plain-text retry fail.
    real.execute(f"DROP TABLE {_module_search_index_constants.STORE_FTS5_TABLE}")
    real.execute(
        f"CREATE TABLE {_module_search_index_constants.STORE_FTS5_TABLE} "
        "(block_key TEXT, bvid TEXT, page_index INT, start_ms INT, end_ms INT, "
        "pubdate INT, text TEXT, source TEXT, rank REAL)"
    )
    real.commit()

    monkeypatch.setattr(
        _module_search_index_store.TranscriptSearchIndex, "_connect", lambda self: real
    )
    try:
        with pytest.raises(_module_search_index_errors.TranscriptStoreError):
            _module_search_index_store.TranscriptSearchIndex(indexed_store).search_blocks("zzzz-nothing")
    finally:
        real.close()


def test_search_blocks_returns_every_hit_past_the_chunk_boundary(
    indexed_store: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More than one IN-list chunk must still decorate every hit.

    The chunking introduced with the batched reads (``_IN_CHUNK``) is what keeps
    the key lists away from SQLite's bound-variable ceiling; without a test
    driving past the boundary, raising/removing it leaves the suite green while
    silently reintroducing the ceiling.
    """
    import sqlite3 as _sqlite3

    from bili_asr import search_index

    real = _sqlite3.connect(indexed_store / "archive.db")
    total = _module_search_index_store.TranscriptSearchIndex._IN_CHUNK + 5
    for i in range(total):
        key = f"chunk{i}:0"
        bvid = f"BVchunk{i}"
        real.execute(
            f"INSERT INTO {_module_search_index_constants.STORE_FTS5_TABLE} VALUES "
            f"(?, ?, 0, 0, 10, 1, 'chunked needle', 'srt', 0.0)",
            (key, bvid),
        )
        real.execute(
            "INSERT INTO videos (bvid, mid, title, pubdate, created_at, updated_at) "
            "VALUES (?, 1, ?, 1, 1, 1)",
            (bvid, f"title{i}"),
        )
    real.commit()

    monkeypatch.setattr(
        _module_search_index_store.TranscriptSearchIndex, "_connect", lambda self: real
    )
    hits = _module_search_index_store.TranscriptSearchIndex(indexed_store).search_blocks(
        "needle", limit=None
    )
    real.close()
    assert len(hits) == total
    assert all(h.video_title and h.snippet for h in hits)
