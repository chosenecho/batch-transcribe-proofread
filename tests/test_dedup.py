"""Contract tests for the read-only exact-duplicate inventory."""

from __future__ import annotations

import json

from bili_asr.cli.main import main
from bili_asr.dedup import build_report
from bili_asr.storage import open_database


def _seed_archive(connection) -> None:
    with connection:
        connection.execute(
            "INSERT INTO bilibili_users(mid, display_name, created_at, updated_at) "
            "VALUES (1, 'test', 1, 1)"
        )
        for bvid, title, part_id in (("BVone", "one", 1), ("BVtwo", "two", 2)):
            connection.execute(
                """INSERT INTO videos(bvid, aid, mid, title, pubdate, created_at, updated_at)
                   VALUES (?, ?, 1, ?, 1, 1, 1)""",
                (bvid, part_id, title),
            )
            connection.execute(
                """INSERT INTO video_parts(
                       video_part_id, bvid, page_index, cid, title, duration_ms,
                       processing_status, created_at, updated_at
                   ) VALUES (?, ?, 0, ?, 'part', 1000, 'metadata_collected', 1, 1)""",
                (part_id, bvid, part_id),
            )
        connection.execute(
            """INSERT INTO audio_objects(
                   audio_id, sha256, byte_size, format, duration_ms, storage_key, created_at
               ) VALUES (1, ?, 4, 'm4a', 1000, 'audio/shared.m4a', 1)""",
            ("a" * 64,),
        )
        connection.executemany(
            """INSERT INTO part_audio_objects(
                   video_part_id, audio_id, acquired_at, acquisition_source
               ) VALUES (?, 1, 1, 'test')""",
            ((1,), (2,)),
        )
        for transcript_id, part_id in ((1, 1), (2, 2)):
            connection.execute(
                """INSERT INTO transcripts(
                       transcript_id, video_part_id, source_kind, language, model_id,
                       version, content_sha256, created_at
                   ) VALUES (?, ?, 'subtitle-ai', 'zh-CN', NULL, 1, ?, 1)""",
                (transcript_id, part_id, "b" * 64),
            )
            connection.execute(
                """INSERT INTO transcript_segments(transcript_id, ordinal, start_ms, end_ms, text)
                   VALUES (?, 0, 0, 1000, 'same')""",
                (transcript_id,),
            )


def test_exact_reuse_report_counts_cross_part_audio_and_transcript_reuse(tmp_path) -> None:
    connection = open_database(tmp_path)
    try:
        _seed_archive(connection)
        report = build_report(connection, limit=10)
    finally:
        connection.close()

    assert report["audio"] == {
        "objects": 1,
        "links": 2,
        "reused_objects": 1,
        "reused_parts": 1,
        "groups": [{"audio_id": 1, "sha256": "a" * 64, "parts": 2}],
    }
    assert report["transcripts"]["total"] == 2
    assert report["transcripts"]["cross_part_groups"] == 1
    assert report["transcripts"]["cross_part_rows"] == 2
    assert report["transcripts"]["groups"] == [
        {
            "content_sha256": "b" * 64,
            "source_kind": "subtitle-ai",
            "language": "zh-CN",
            "parts": 2,
            "rows": 2,
        }
    ]


def test_dedup_cli_emits_json_without_writing_archive(tmp_path, capsys) -> None:
    connection = open_database(tmp_path)
    try:
        _seed_archive(connection)
    finally:
        connection.close()

    before = (tmp_path / "archive.db").stat().st_mtime_ns
    assert main(["dedup", "report", "--archive-root", str(tmp_path), "--format", "json"]) == 0
    output = json.loads(capsys.readouterr().out)
    after = (tmp_path / "archive.db").stat().st_mtime_ns

    assert output["audio"]["reused_objects"] == 1
    assert output["transcripts"]["cross_part_groups"] == 1
    assert after == before
