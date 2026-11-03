"""Metadata scope and mixed-source search contracts, with no live services."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from bili_asr.cli.main import main
from bili_asr.cli.parser import build_parser
from bili_asr.search_index import (
    MetadataSearchIndex, TranscriptSearchIndex, check_fts5_available, search_archive,
)
from bili_asr.search_index.errors import FTS5UnavailableError, TranscriptStoreError
from bili_asr.storage import (
    AcquisitionRunRecord, MetadataRepository, TranscriptRepository,
    TranscriptSegmentRecord, open_database,
)
from tests.fixtures.metadata_records import make_part_record, make_user_record, make_video_record


def _video(
    root: Path,
    bvid: str,
    *,
    title: str = "unrelated title",
    description: str | None = None,
    tags: tuple[str, ...] = (),
    pages: tuple[int, ...] = (0,),
    pubdate: int = 1_700_000_000,
) -> None:
    connection = open_database(root)
    try:
        metadata = MetadataRepository(connection)
        with metadata.transaction():
            metadata.upsert_user(make_user_record())
            metadata.upsert_video(replace(make_video_record(bvid, aid=None, title=title), pubdate=pubdate))
            for page in pages:
                metadata.upsert_part(make_part_record(bvid, page_index=page, cid=page + 1))
        if description is not None:
            connection.execute(
                'INSERT INTO video_details (bvid, "desc", observed_at) VALUES (?, ?, 1)',
                (bvid, description),
            )
        connection.executemany(
            "INSERT INTO video_tags (bvid, tag_id, tag_name, tag_type) VALUES (?, ?, ?, 'normal')",
            [(bvid, index, tag) for index, tag in enumerate(tags, 1)],
        )
        connection.commit()
    finally:
        connection.close()


def _transcript(root: Path, bvid: str, text: str) -> None:
    connection = open_database(root)
    try:
        repository = TranscriptRepository(connection)
        run_id = f"search:{bvid}"
        repository.start_acquisition_run(AcquisitionRunRecord(
            run_id=run_id, kind="subtitle", selector_kind="pending", selector_target=None,
            requested_limit=None, credential_present=False, started_at=1, outcome="running", finished_at=None,
        ))
        part_id = int(connection.execute("SELECT video_part_id FROM video_parts WHERE bvid = ?", (bvid,)).fetchone()[0])
        repository.record_acquired_transcript(
            run_id=run_id, video_part_id=part_id, source_kind="subtitle-ai", language="zh",
            segments=(TranscriptSegmentRecord(1200, 2400, text),), started_at=2, finished_at=3, created_at=4,
        )
    finally:
        connection.close()


def _argv(root: Path, query: str = "needle", *, scope: str = "metadata", extra: tuple[str, ...] = ()) -> list[str]:
    return ["search", query, "--archive-root", str(root), "--scope", scope, *extra]


def test_search_help_and_parser_document_current_scopes(capsys) -> None:
    parser = build_parser()
    assert parser.parse_args(["search", "needle"]).scope == "transcripts"
    for scope in ("transcripts", "metadata", "all"):
        assert parser.parse_args(["search", "needle", "--scope", scope]).scope == scope
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["search", "--help"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "--scope {transcripts,metadata,all}" in output and "metadata first" in output


@pytest.mark.parametrize("field", ["title", "description", "tags"])
def test_metadata_fields_without_transcripts_or_index(tmp_path: Path, field: str) -> None:
    kwargs = {field: "关键词所在字段" if field != "tags" else ("关键词所在字段",)}
    _video(tmp_path, "BVfields", **kwargs)
    hits = MetadataSearchIndex(tmp_path).search_metadata("关键词")
    assert len(hits) == 1
    assert hits[0].matched_fields == (field,)
    assert hits[0].bvid == "BVfields"
    assert hits[0].page_index == 0
    assert hits[0].start_ms is hits[0].end_ms is hits[0].rank is None
    connection = sqlite3.connect(tmp_path / "archive.db")
    assert connection.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM sqlite_master WHERE name='transcript_fts'").fetchone()[0] == 0
    connection.close()


def test_metadata_merges_fields_and_tags_per_part_with_stable_priority(tmp_path: Path) -> None:
    _video(tmp_path, "BVtitle", title="needle", description="needle", tags=("needle-a", "needle-b"), pages=(2, 0))
    _video(tmp_path, "BVtag", tags=("needle",), pubdate=1_800_000_000)
    _video(tmp_path, "BVdesc", description="needle", pubdate=1_900_000_000)
    hits = MetadataSearchIndex(tmp_path).search_metadata("needle")
    assert [(hit.bvid, hit.page_index) for hit in hits] == [
        ("BVtitle", 0), ("BVtitle", 2), ("BVtag", 0), ("BVdesc", 0),
    ]
    assert hits[0].matched_fields == ("title", "tags", "description")
    assert hits[0].snippet.startswith("title: ")
    assert len({hit.block_key for hit in hits}) == 4


def test_metadata_video_without_parts_and_without_details(tmp_path: Path, capsys) -> None:
    _video(tmp_path, "BVwhole", title="needle", pages=())
    assert main(_argv(tmp_path, extra=("--format", "json"))) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["page_index"] is None
    assert row["block_key"] == "metadata:BVwhole:video"
    assert row["matched_fields"] == ["title"]
    assert row["hit_type"] == row["source"] == "metadata"
    assert row["start_ms"] is row["end_ms"] is row["rank"] is None
    assert main(_argv(tmp_path)) == 0
    output = capsys.readouterr().out
    assert "整视频" in output and "[metadata]" in output and "[—]" in output
    assert "00:00" not in output


@pytest.mark.parametrize("query", ["%", "_", "\\", "'", '"', "%' OR 1=1 --", "中文"])
def test_metadata_query_is_literal_and_bound(tmp_path: Path, query: str) -> None:
    _video(tmp_path, "BVliteral", title=f"prefix {query} suffix")
    _video(tmp_path, "BVother", title="plain content")
    assert [hit.bvid for hit in MetadataSearchIndex(tmp_path).search_metadata(query)] == ["BVliteral"]


def test_metadata_ascii_case_folding_and_empty_queries(tmp_path: Path, capsys) -> None:
    _video(tmp_path, "BVcase", title="NeEdLe Ä")
    index = MetadataSearchIndex(tmp_path)
    assert len(index.search_metadata("needle")) == 1
    assert index.search_metadata("ä") == []
    assert index.search_metadata("   ") == []
    assert main(_argv(tmp_path, "not present", extra=("--format", "json"))) == 0
    assert json.loads(capsys.readouterr().out) == []


def _utc(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())


def test_metadata_date_bounds_are_inclusive_utc_days(tmp_path: Path, capsys) -> None:
    for bvid, date in (
        ("BVbefore", "2025-12-31T23:59:59"), ("BVstart", "2026-01-01T00:00:00"),
        ("BVend", "2026-01-01T23:59:59"), ("BVafter", "2026-01-02T00:00:00"),
    ):
        _video(tmp_path, bvid, title="needle", pubdate=_utc(date))
    assert main(_argv(tmp_path, extra=("--from", "2026-01-01", "--to", "2026-01-01", "--format", "json"))) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["bvid"] for row in rows] == ["BVend", "BVstart"]


def test_metadata_does_not_resolve_artifacts_or_touch_transcript_search(tmp_path: Path, monkeypatch, capsys) -> None:
    import bili_asr.artifact_root as artifact_root

    _video(tmp_path, "BVonly", title="needle")
    monkeypatch.setattr(artifact_root, "roots_for", lambda *a, **k: pytest.fail("metadata resolves artifacts"))
    monkeypatch.setattr(TranscriptSearchIndex, "search_blocks", lambda *a, **k: pytest.fail("metadata queries FTS"))
    assert main(_argv(tmp_path, extra=("--format", "json", "--artifact-root", str(tmp_path / "not-an-artifact-root")))) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1


def test_metadata_connection_is_read_only_and_updates_need_no_rebuild(tmp_path: Path) -> None:
    _video(tmp_path, "BVonly", title="old title")
    index = MetadataSearchIndex(tmp_path)
    connection = index._connect()
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("UPDATE videos SET title = 'needle'")
    finally:
        connection.close()
    writer = sqlite3.connect(tmp_path / "archive.db")
    writer.execute("UPDATE videos SET title = 'needle' WHERE bvid = 'BVonly'")
    writer.commit()
    writer.close()
    assert len(index.search_metadata("needle")) == 1


def test_metadata_description_snippet_is_bounded_around_match(tmp_path: Path) -> None:
    _video(tmp_path, "BVlong", description="开头" * 300 + "needle" + "结尾" * 300)
    hit = MetadataSearchIndex(tmp_path).search_metadata("needle")[0]
    assert "needle" in hit.snippet
    assert len(hit.snippet) <= 200
    assert hit.snippet.startswith("description: …") and hit.snippet.endswith("…")


def test_missing_store_never_creates_files_and_json_stays_parseable(tmp_path: Path, capsys) -> None:
    root = tmp_path / "does-not-exist"
    for scope in ("metadata", "transcripts", "all"):
        assert main(_argv(root, scope=scope, extra=("--format", "json"))) == 0
        output = capsys.readouterr()
        assert json.loads(output.out) == []
        assert "missing" in output.err
        assert not root.exists()


def test_metadata_schema_defect_is_not_absent_description(tmp_path: Path, capsys) -> None:
    _video(tmp_path, "BVhealthy", title="needle")
    connection = sqlite3.connect(tmp_path / "archive.db")
    connection.execute("DROP TABLE video_details")
    connection.commit()
    connection.close()
    assert main(_argv(tmp_path, extra=("--format", "json"))) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "metadata schema incompatible" in output.err and "video_details" in output.err


def test_corrupt_database_is_a_defect_for_metadata_and_all(tmp_path: Path, capsys) -> None:
    (tmp_path / "archive.db").write_bytes(b"not sqlite")
    for scope in ("metadata", "all"):
        assert main(_argv(tmp_path, scope=scope, extra=("--format", "json"))) == 1
        output = capsys.readouterr()
        assert output.out == "" and "corrupt" in output.err


def test_all_without_index_keeps_metadata_and_reports_backlog_on_stderr(tmp_path: Path, capsys) -> None:
    _video(tmp_path, "BVonly", title="needle")
    assert main(_argv(tmp_path, scope="all", extra=("--format", "json"))) == 0
    output = capsys.readouterr()
    assert len(json.loads(output.out)) == 1
    assert "index missing" in output.err


def test_all_without_fts5_keeps_metadata_but_transcripts_returns_defect(tmp_path: Path, monkeypatch, capsys) -> None:
    _video(tmp_path, "BVonly", title="needle")
    def unavailable(*args, **kwargs):
        raise FTS5UnavailableError("unavailable test runtime")
    monkeypatch.setattr(TranscriptSearchIndex, "search_blocks", unavailable)
    assert main(_argv(tmp_path, scope="all", extra=("--format", "json"))) == 0
    output = capsys.readouterr()
    assert len(json.loads(output.out)) == 1 and "unavailable" in output.err
    assert main(_argv(tmp_path, scope="transcripts")) == 1
    assert "unavailable" in capsys.readouterr().err


@pytest.mark.skipif(not check_fts5_available(), reason="FTS5 unavailable")
def test_mixed_hits_keep_transcript_contract_rank_and_one_total_limit(tmp_path: Path, capsys) -> None:
    _video(tmp_path, "BVboth", title="needle")
    _transcript(tmp_path, "BVboth", "needle in transcript")
    _video(tmp_path, "BVtext", title="unrelated")
    _transcript(tmp_path, "BVtext", "needle")
    assert TranscriptSearchIndex(tmp_path).build() == 2
    original = TranscriptSearchIndex(tmp_path).search_blocks("needle")
    result = search_archive(tmp_path, "needle", scope="all", limit=3)
    assert [hit.hit_type for hit in result.hits] == ["metadata", "transcript", "transcript"]
    assert list(result.hits[1:]) == original
    assert len(search_archive(tmp_path, "needle", scope="all", limit=1).hits) == 1
    assert main(_argv(tmp_path, scope="all", extra=("--format", "json", "--limit", "2"))) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2 and rows[0]["hit_type"] == "metadata" and rows[1]["hit_type"] == "transcript"
    assert rows[1]["start_ms"] == 1200 and rows[1]["end_ms"] == 2400
    assert rows[1]["source"] == "store" and rows[1]["rank"] is not None
    # The omitted --scope default retains transcript-only results.
    assert main(["search", "needle", "--archive-root", str(tmp_path), "--format", "json"]) == 0
    assert all(row["hit_type"] == "transcript" for row in json.loads(capsys.readouterr().out))


@pytest.mark.skipif(not check_fts5_available(), reason="FTS5 unavailable")
def test_mixed_scope_applies_the_same_date_window_to_both_sources(tmp_path: Path, capsys) -> None:
    for bvid, date in (("BVbefore", "2025-12-31T23:59:59"), ("BVinside", "2026-01-01T23:59:59")):
        _video(tmp_path, bvid, title="needle", pubdate=_utc(date))
        _transcript(tmp_path, bvid, "needle")
    TranscriptSearchIndex(tmp_path).build()
    assert main(_argv(tmp_path, scope="all", extra=("--from", "2026-01-01", "--to", "2026-01-01", "--format", "json"))) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(row["bvid"], row["hit_type"]) for row in rows] == [("BVinside", "metadata"), ("BVinside", "transcript")]


@pytest.mark.skipif(not check_fts5_available(), reason="FTS5 unavailable")
def test_all_does_not_hide_a_defective_fts_even_when_metadata_fills_limit(tmp_path: Path, capsys) -> None:
    _video(tmp_path, "BVonly", title="needle")
    connection = sqlite3.connect(tmp_path / "archive.db")
    connection.execute("CREATE TABLE transcript_fts (block_key TEXT, bvid TEXT)")
    connection.commit()
    connection.close()
    assert main(_argv(tmp_path, scope="all", extra=("--format", "json", "--limit", "1"))) == 1
    output = capsys.readouterr()
    assert output.out == "" and "expected an FTS5 table" in output.err


@pytest.mark.skipif(not check_fts5_available(), reason="FTS5 unavailable")
def test_transcript_query_syntax_falls_back_but_fts_page_damage_propagates(tmp_path: Path) -> None:
    _video(tmp_path, "BVtext")
    _transcript(tmp_path, "BVtext", "the word NOT is present")
    index = TranscriptSearchIndex(tmp_path)
    index.build()
    assert len(index.search_blocks("NOT")) == 1
    connection = sqlite3.connect(tmp_path / "archive.db")
    connection.execute("DELETE FROM transcript_fts_data")
    connection.commit()
    connection.close()
    with pytest.raises(TranscriptStoreError):
        search_archive(tmp_path, "NOT", scope="all")


def test_rebuild_metadata_is_usage_error_and_failure_is_not_ignored(tmp_path: Path, monkeypatch, capsys) -> None:
    _video(tmp_path, "BVonly", title="needle")
    with pytest.raises(SystemExit) as error:
        main(_argv(tmp_path, extra=("--rebuild",)))
    assert error.value.code == 2
    assert "requires --scope" in capsys.readouterr().err
    import bili_asr.cli.search as command_module
    monkeypatch.setattr(command_module, "_cmd_search_index", lambda args: 1)
    assert main(_argv(tmp_path, scope="all", extra=("--rebuild", "--format", "json"))) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.skipif(not check_fts5_available(), reason="FTS5 unavailable")
def test_rebuild_json_output_has_no_index_progress(tmp_path: Path, capsys) -> None:
    _video(tmp_path, "BVboth", title="needle")
    _transcript(tmp_path, "BVboth", "needle")
    assert main(_argv(tmp_path, scope="all", extra=("--rebuild", "--format", "json"))) == 0
    output = capsys.readouterr()
    assert [row["hit_type"] for row in json.loads(output.out)] == ["metadata", "transcript"]
    assert "indexed 1" in output.err


@pytest.mark.parametrize("extra", [
    ("--limit", "0"), ("--from", "2026-02-31"), ("--from", "2026-02-02", "--to", "2026-02-01"),
])
def test_metadata_usage_errors_match_transcript_contract(tmp_path: Path, extra: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as error:
        main(_argv(tmp_path, extra=extra))
    assert error.value.code == 2
    assert not (tmp_path / "archive.db").exists()


def test_metadata_large_fixture_has_one_match_query_and_bounded_results(tmp_path: Path, monkeypatch) -> None:
    _video(tmp_path, "BVseed", title="unrelated", pages=())
    connection = sqlite3.connect(tmp_path / "archive.db")
    count = 3000
    connection.executemany(
        "INSERT INTO videos (bvid,mid,title,pubdate,created_at,updated_at) VALUES (?,23191782,?,1,1,1)",
        [(f"BVbulk{index:05}", "needle" if index % 2 else "plain") for index in range(count)],
    )
    connection.executemany(
        "INSERT INTO video_tags (bvid,tag_id,tag_name,tag_type) VALUES (?,1,'needle tag','normal')",
        [(f"BVbulk{index:05}",) for index in range(count)],
    )
    connection.commit()
    connection.close()
    index = MetadataSearchIndex(tmp_path)
    original_connect = index._connect
    queries: list[str] = []
    def traced():
        connection = original_connect()
        connection.set_trace_callback(queries.append)
        return connection
    monkeypatch.setattr(index, "_connect", traced)
    hits = index.search_metadata("needle", limit=25)
    assert len(hits) == 25
    assert all(hit.matched_fields == ("title", "tags") for hit in hits)
    assert len([sql for sql in queries if sql.lstrip().startswith("WITH")]) == 1
    assert len(queries) == 5  # Four schema probes and one matching query, independent of hits.
