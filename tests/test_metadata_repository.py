"""Offline repository contract tests for normalized metadata persistence."""

from __future__ import annotations

from dataclasses import fields, replace
import os
import re
import sqlite3

import pytest

from bili_asr.storage.metadata import MetadataRepository
from bili_asr.storage.database import open_database
from bili_asr.storage.models import (
    UserRecord,
    VideoDetailRecord,
    VideoPartRecord,
    VideoTagRecord,
)
from tests.fixtures.metadata_records import (
    MID,
    make_cursor_record,
    make_discovery_record,
    make_page_record,
    make_run_record,
    make_part_record,
    make_user_record,
    make_video_record,
)


def _start_run(repository: MetadataRepository, run_id: str = "run-1") -> None:
    repository.upsert_user(make_user_record())
    repository.start_run(make_run_record(run_id))


def test_collaborating_owner_is_rolled_back_with_failed_page(tmp_root):
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        _start_run(repository)
        owner = UserRecord(MID + 1, "合作投稿者", 100, 100)
        with pytest.raises(sqlite3.IntegrityError):
            repository.record_page(
                make_page_record(), additional_users=[owner],
                videos=[replace(make_video_record(), mid=owner.mid)],
                parts=[make_part_record("BV-MISSING")],
            )
        assert connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM bilibili_users WHERE mid = ?", (owner.mid,)
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_models_validate_scalars_and_compute_work_id_without_persisting_it():
    part = make_part_record()
    assert part.work_id == "BV1SINGLE:p0"
    assert "work_id" not in {field.name for field in fields(VideoPartRecord)}

    with pytest.raises(ValueError):
        make_part_record(page_index=-1)
    with pytest.raises(ValueError):
        make_part_record(cid=0)
    with pytest.raises(ValueError):
        make_page_record(outcome="failed", error_code="raw traceback\n")
    with pytest.raises(ValueError):
        UserRecord(mid=MID, display_name="", created_at=1, updated_at=1)


def test_record_page_persists_single_and_multipart_entities_in_locked_order(tmp_root):
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        _start_run(repository)
        multipart = make_video_record("BV1MULTI", title="多集视频", aid=1002)
        page = make_page_record()
        repository.record_page(
            page,
            user=make_user_record(),
            videos=[make_video_record(), multipart],
            parts=[
                make_part_record(),
                make_part_record("BV1MULTI", page_index=0, cid=3001, title="上篇"),
                make_part_record("BV1MULTI", page_index=1, cid=3002, title="下篇"),
            ],
            discoveries=[
                make_discovery_record("BV1SINGLE"),
                make_discovery_record("BV1MULTI", source_position=1),
            ],
            cursor=make_cursor_record(),
        )

        assert connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM video_parts").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM ingestion_discoveries").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM ingestion_pages").fetchone()[0] == 1
        assert repository.read_cursor(MID) == make_cursor_record()

        pending = repository.list_pending_parts()
        assert [row["work_id"] for row in pending] == [
            "BV1MULTI:p0",
            "BV1MULTI:p1",
            "BV1SINGLE:p0",
        ]
        stats = repository.run_stats("run-1")
        assert stats is not None
        assert stats["page_count"] == 1
        assert stats["video_count"] == 2
    finally:
        connection.close()


def test_repeated_page_is_idempotent_but_a_new_run_keeps_page_evidence(tmp_root):
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        _start_run(repository)
        repository.record_page(
            make_page_record(),
            user=make_user_record(),
            videos=[make_video_record()],
            parts=[make_part_record()],
            discoveries=[make_discovery_record("BV1SINGLE")],
            cursor=make_cursor_record(),
        )

        repository.record_page(
            make_page_record(finished_at=120),
            user=make_user_record(display_name="未明子（更新）", updated_at=119),
            videos=[make_video_record(title="更新后的标题", updated_at=119)],
            parts=[make_part_record(title="更新后的分集标题", updated_at=119)],
            discoveries=[make_discovery_record("BV1SINGLE", discovered_at=119)],
            cursor=make_cursor_record(updated_at=120),
        )
        assert connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM video_parts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM ingestion_discoveries").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM ingestion_pages").fetchone()[0] == 1
        assert connection.execute("SELECT title FROM videos").fetchone()[0] == "更新后的标题"
        assert connection.execute("SELECT aid FROM videos").fetchone()[0] == 1001

        _start_run(repository, "run-2")
        repository.record_page(
            make_page_record("run-2"),
            videos=[make_video_record(title="同一视频的第二次发现")],
            parts=[make_part_record()],
            discoveries=[make_discovery_record("BV1SINGLE", run_id="run-2")],
            cursor=make_cursor_record(updated_at=130),
        )
        assert connection.execute("SELECT COUNT(*) FROM ingestion_pages").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM ingestion_discoveries").fetchone()[0] == 2
        assert repository.run_stats("run-1")["video_count"] == 1
        assert repository.run_stats("run-2")["video_count"] == 1
        assert connection.execute("SELECT title FROM videos").fetchone()[0] == "同一视频的第二次发现"
    finally:
        connection.close()


def test_ok_page_write_failure_rolls_back_and_caller_records_failure(tmp_root):
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        _start_run(repository)
        repository.record_page(
            make_page_record(),
            user=make_user_record(),
            videos=[make_video_record()],
            parts=[make_part_record()],
            discoveries=[make_discovery_record("BV1SINGLE")],
            cursor=make_cursor_record(),
        )
        ok_page = make_page_record(page_number=2, started_at=200, finished_at=201)
        with pytest.raises(sqlite3.IntegrityError):
            repository.record_page(
                ok_page,
                user=make_user_record(display_name="must roll back", updated_at=200),
                videos=[make_video_record(title="must roll back", updated_at=200)],
                parts=[make_part_record("BV-MISSING", title="orphan")],
                discoveries=[
                    make_discovery_record("BV1SINGLE", page_number=2, discovered_at=200)
                ],
                cursor=make_cursor_record(next_page=3, updated_at=201),
            )

        assert repository.read_cursor(MID) == make_cursor_record()
        # The transaction head rolled back: the user label from page 1
        # survives, and the failing call's user write is absent.
        assert (
            connection.execute("SELECT display_name FROM bilibili_users").fetchone()[0]
            == "未明子"
        )
        assert connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM video_parts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM ingestion_pages").fetchone()[0] == 1
        assert tuple(
            connection.execute(
                "SELECT outcome, finished_at FROM ingestion_runs WHERE run_id = 'run-1'"
            ).fetchone()
        ) == ("running", None)

        # The caller owns failure recording: a fresh failed page record with a
        # bounded scalar error code, replayed with no payload arguments.
        failure_evidence = make_page_record(
            page_number=2,
            outcome="failed",
            error_code="foreign_key",
            started_at=200,
            finished_at=201,
        )
        repository.record_page(failure_evidence)

        page_row = connection.execute(
            "SELECT outcome, error_code FROM ingestion_pages WHERE run_id = 'run-1' AND page_number = 2"
        ).fetchone()
        assert tuple(page_row) == ("failed", "foreign_key")
        assert tuple(
            connection.execute(
                "SELECT outcome, finished_at FROM ingestion_runs WHERE run_id = 'run-1'"
            ).fetchone()
        ) == ("failed", 201)
    finally:
        connection.close()


def test_record_page_rejects_payloads_on_a_failed_page(tmp_root):
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        _start_run(repository)
        failed_page = make_page_record(
            page_number=2,
            outcome="failed",
            error_code="foreign_key",
            started_at=200,
            finished_at=201,
        )
        with pytest.raises(ValueError):
            repository.record_page(
                failed_page,
                videos=[make_video_record()],
                parts=[make_part_record()],
                cursor=make_cursor_record(next_page=3),
            )

        assert tuple(
            connection.execute(
                "SELECT outcome, finished_at FROM ingestion_runs WHERE run_id = 'run-1'"
            ).fetchone()
        ) == ("running", None)
        assert connection.execute(
            "SELECT COUNT(*) FROM ingestion_pages WHERE page_number = 2"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0
    finally:
        connection.close()


def test_late_failed_page_after_terminal_run_keeps_run_outcome_unchanged(tmp_root):
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        _start_run(repository)
        repository.record_page(
            make_page_record(),
            videos=[make_video_record()],
            parts=[make_part_record()],
            discoveries=[make_discovery_record("BV1SINGLE")],
        )
        repository.finish_run(make_run_record(outcome="complete", finished_at=300))

        late_failure = make_page_record(
            page_number=2,
            outcome="failed",
            error_code="stale_page_result",
            started_at=400,
            finished_at=401,
        )
        repository.record_page(late_failure)

        page_row = connection.execute(
            "SELECT outcome, error_code FROM ingestion_pages "
            "WHERE run_id = 'run-1' AND page_number = 2"
        ).fetchone()
        assert tuple(page_row) == ("failed", "stale_page_result")
        assert tuple(
            connection.execute(
                "SELECT outcome, finished_at FROM ingestion_runs WHERE run_id = 'run-1'"
            ).fetchone()
        ) == ("complete", 300)
    finally:
        connection.close()


def test_failed_page_clock_before_running_run_start_is_rejected_and_nothing_persisted(tmp_root):
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        _start_run(repository)

        # The run's stored started_at (101) is the failure transition's
        # ordering baseline, exactly as it is for finish_run: a valid page
        # record whose clock lies below it is rejected and nothing persists.
        stale_failure = make_page_record(
            page_number=1,
            outcome="failed",
            error_code="stale_page_result",
            started_at=50,
            finished_at=51,
        )
        with pytest.raises(ValueError):
            repository.record_page(stale_failure)

        assert connection.execute(
            "SELECT COUNT(*) FROM ingestion_pages WHERE run_id = 'run-1'"
        ).fetchone()[0] == 0
        assert tuple(
            connection.execute(
                "SELECT outcome, finished_at FROM ingestion_runs WHERE run_id = 'run-1'"
            ).fetchone()
        ) == ("running", None)
    finally:
        connection.close()


def test_finish_run_rejects_refinishing_a_terminal_run(tmp_root):
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        _start_run(repository)
        repository.finish_run(make_run_record(outcome="complete", finished_at=300))
        with pytest.raises(sqlite3.IntegrityError):
            repository.finish_run(make_run_record(outcome="failed", finished_at=310))
        assert tuple(
            connection.execute(
                "SELECT outcome, finished_at FROM ingestion_runs WHERE run_id = 'run-1'"
            ).fetchone()
        ) == ("complete", 300)
    finally:
        connection.close()


def test_finish_run_record_form_validates_against_database_started_at(tmp_root):
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        _start_run(repository)

        # The stored started_at (101) is the ordering baseline, not the
        # caller-supplied record's own started_at field (50).
        stale_clock_record = make_run_record(
            started_at=50, outcome="complete", finished_at=60
        )
        with pytest.raises(ValueError):
            repository.finish_run(stale_clock_record)

        with pytest.raises(ValueError):
            repository.finish_run(make_run_record(outcome="running", finished_at=120))
        with pytest.raises(ValueError):
            repository.finish_run(make_run_record(outcome="complete", finished_at=None))

        repository.finish_run(make_run_record(outcome="limited", finished_at=300))
        stats = repository.run_stats("run-1")
        assert stats is not None
        assert stats["outcome"] == "limited"
        assert stats["finished_at"] == 300
    finally:
        connection.close()


def test_ensure_user_establishes_a_row_without_rewriting_an_existing_one(tmp_root):
    """The two user writes differ in exactly one way: the update.

    ``ensure_user`` exists for the run-start write, which has to satisfy the
    run and cursor foreign keys before any page is fetched — and therefore has
    observed no label.  It must create the row and then leave it alone;
    ``upsert_user`` stays the refreshing write a page's observed name uses.
    """

    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        # No row yet: ``ensure_user`` creates it, so a first-ever run keeps
        # its foreign-key parent.
        with repository.transaction():
            repository.ensure_user(make_user_record())
        assert (
            connection.execute("SELECT display_name FROM bilibili_users").fetchone()[0]
            == "未明子"
        )

        # An existing row is untouched — value and stamp both.
        with repository.transaction():
            repository.ensure_user(
                make_user_record(display_name=str(MID), updated_at=999)
            )
        assert tuple(
            connection.execute(
                "SELECT display_name, updated_at FROM bilibili_users"
            ).fetchone()
        ) == ("未明子", 100)

        # ``upsert_user`` still overwrites: that is how an observed name lands.
        with repository.transaction():
            repository.upsert_user(
                make_user_record(display_name="未明子（新）", updated_at=200)
            )
        assert tuple(
            connection.execute(
                "SELECT display_name, updated_at FROM bilibili_users"
            ).fetchone()
        ) == ("未明子（新）", 200)
    finally:
        connection.close()


def test_fk_rejection_and_delete_restriction_apply_to_repository_writes(tmp_root):
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with repository.transaction():
                repository.upsert_video(make_video_record("BV-ORPHAN"))
        with pytest.raises(sqlite3.IntegrityError):
            with repository.transaction():
                repository.upsert_part(make_part_record("BV-MISSING"))
        with pytest.raises(sqlite3.IntegrityError):
            with repository.transaction():
                repository.write_cursor(make_cursor_record())

        with repository.transaction():
            repository.upsert_user(make_user_record())
            repository.upsert_video(make_video_record())
            repository.upsert_part(make_part_record())

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM bilibili_users WHERE mid = ?", (MID,))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM videos WHERE bvid = 'BV1SINGLE'")
    finally:
        connection.close()


def test_repository_end_to_end_records_two_runs_with_cursor_transitions(tmp_root):
    """Full page flow: user, single-part video, multipart video, two runs."""
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        assert repository.read_cursor(MID) is None

        _start_run(repository)

        # Page 1 applies the locked order in one transaction:
        # user -> video -> parts -> discoveries -> cursor -> page outcome.
        repository.record_page(
            make_page_record(),
            user=make_user_record(),
            videos=[make_video_record()],
            parts=[make_part_record()],
            discoveries=[make_discovery_record("BV1SINGLE")],
            cursor=make_cursor_record(),
        )
        assert repository.read_cursor(MID) == make_cursor_record()

        repository.start_run(
            make_run_record("run-2", requested_start_page=2, started_at=200)
        )
        repository.record_page(
            make_page_record(
                "run-2", page_number=2, started_at=201, finished_at=202
            ),
            videos=[
                make_video_record("BV1MULTI", title="多集视频", aid=1002, updated_at=203)
            ],
            parts=[
                make_part_record(
                    "BV1MULTI", page_index=0, cid=3001, title="上篇", updated_at=204
                ),
                make_part_record(
                    "BV1MULTI", page_index=1, cid=3002, title="下篇", updated_at=204
                ),
            ],
            discoveries=[
                make_discovery_record(
                    "BV1MULTI", run_id="run-2", page_number=2, discovered_at=205
                )
            ],
            cursor=make_cursor_record(next_page=3, updated_at=206),
        )

        # Page 3 is empty: no payload rows, only the cursor state transition.
        repository.record_page(
            make_page_record(
                "run-2",
                page_number=3,
                outcome="empty",
                started_at=210,
                finished_at=211,
            ),
            cursor=make_cursor_record(
                next_page=3, state="complete", updated_at=211
            ),
        )

        repository.finish_run(make_run_record(outcome="complete", finished_at=300))
        repository.finish_run(
            make_run_record("run-2", started_at=200, outcome="complete", finished_at=301)
        )

        assert connection.execute("SELECT COUNT(*) FROM bilibili_users").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM video_parts").fetchone()[0] == 3
        page_rows = connection.execute(
            "SELECT run_id, page_number, outcome, error_code FROM ingestion_pages "
            "ORDER BY run_id, page_number"
        ).fetchall()
        assert [tuple(row) for row in page_rows] == [
            ("run-1", 1, "ok", None),
            ("run-2", 2, "ok", None),
            ("run-2", 3, "empty", None),
        ]
        assert repository.read_cursor(MID) == make_cursor_record(
            next_page=3, state="complete", updated_at=211
        )

        pending = repository.list_pending_parts()
        assert [row["work_id"] for row in pending] == [
            "BV1MULTI:p0",
            "BV1MULTI:p1",
            "BV1SINGLE:p0",
        ]

        stats = repository.run_stats()
        assert [row["run_id"] for row in stats] == ["run-1", "run-2"]
        assert (stats[0]["page_count"], stats[0]["video_count"]) == (1, 1)
        assert (stats[1]["page_count"], stats[1]["video_count"]) == (2, 1)
        assert all(row["outcome"] == "complete" for row in stats)

        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_error_fields_persist_only_bounded_scalar_codes(tmp_root):
    """The repository accepts bounded scalar codes and never secret material."""
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        repository.upsert_user(make_user_record())
        repository.start_run(make_run_record())
        repository.write_cursor(
            make_cursor_record(state="limited", last_error_code="rate_limited")
        )
        repository.record_page(
            make_page_record(
                page_number=2,
                outcome="failed",
                error_code="http_412",
                started_at=120,
                finished_at=121,
            )
        )
        assert repository.read_cursor(MID).last_error_code == "rate_limited"
        stored_page = connection.execute(
            "SELECT outcome, error_code FROM ingestion_pages "
            "WHERE run_id = 'run-1' AND page_number = 2"
        ).fetchone()
        assert tuple(stored_page) == ("failed", "http_412")

        forbidden_codes = [
            "SESSDATA=abc123; bili_jct=def456",  # cookie material
            "https://upos.example.com/signed-url?token",  # signed URL
            '{"code": -403, "message": "raw"}',  # raw JSON document
            "Traceback (most recent call last):",  # exception text
            "e" * 65,  # exceeds the 64-character bound
        ]
        for code in forbidden_codes:
            with pytest.raises(ValueError):
                make_cursor_record(state="limited", last_error_code=code)
            with pytest.raises(ValueError):
                make_page_record(outcome="failed", error_code=code)

        expected_codes = {
            ("ingestion_cursors", "last_error_code"): "rate_limited",
            ("ingestion_pages", "error_code"): "http_412",
        }
        for table, column in (
            ("ingestion_cursors", "last_error_code"),
            ("ingestion_pages", "error_code"),
        ):
            values = connection.execute(
                f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"
            ).fetchall()
            assert [row[0] for row in values] == [expected_codes[(table, column)]]
            for (value,) in values:
                assert len(value) <= 64
                assert re.fullmatch(r"[A-Za-z0-9_.:-]+", value)
    finally:
        connection.close()


def test_re_upsert_backfills_missing_aid_and_keeps_existing_aid(tmp_root):
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        repository.upsert_user(make_user_record())
        repository.upsert_video(make_video_record(aid=None))
        assert connection.execute("SELECT aid FROM videos").fetchone()[0] is None

        repository.upsert_video(make_video_record())
        assert connection.execute("SELECT aid FROM videos").fetchone()[0] == 1001

        repository.upsert_video(make_video_record(aid=9999))
        assert connection.execute("SELECT aid FROM videos").fetchone()[0] == 1001
    finally:
        connection.close()


def test_cursor_resume_and_pending_limit(tmp_root):
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        _start_run(repository)
        repository.record_page(
            make_page_record(),
            videos=[make_video_record()],
            parts=[make_part_record(), make_part_record(page_index=1, cid=2002, title="第二集")],
            discoveries=[make_discovery_record("BV1SINGLE")],
            cursor=make_cursor_record(next_page=2),
        )
        repository.write_cursor(make_cursor_record(next_page=3, updated_at=130))
        assert repository.read_cursor(MID).next_page == 3
        assert [row["page_index"] for row in repository.list_pending_parts(limit=1)] == [0]

        with repository.transaction():
            repository.upsert_part(
                make_part_record(
                    page_index=1, cid=2002, title="第二集", processing_status="metadata_collected"
                )
            )
        assert [row["page_index"] for row in repository.list_pending_parts()] == [0]
    finally:
        connection.close()


def test_finish_run_and_all_run_stats_are_derived_from_normalized_rows(tmp_root):
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        _start_run(repository)
        repository.record_page(
            make_page_record(),
            videos=[make_video_record()],
            parts=[make_part_record()],
            discoveries=[make_discovery_record("BV1SINGLE")],
        )
        repository.finish_run(make_run_record(outcome="complete", finished_at=300))
        stats = repository.run_stats()
        assert len(stats) == 1
        assert stats[0]["outcome"] == "complete"
        assert stats[0]["page_count"] == 1
        assert stats[0]["video_count"] == 1
    finally:
        connection.close()


def test_read_path_validation_splits_type_errors_from_value_errors(tmp_root):
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        with pytest.raises(TypeError):
            repository.read_cursor(True)
        with pytest.raises(TypeError):
            repository.read_cursor("23191782")
        with pytest.raises(ValueError):
            repository.read_cursor(0)

        with pytest.raises(TypeError):
            repository.run_stats(23191782)
        with pytest.raises(ValueError):
            repository.run_stats("   ")

        with pytest.raises(TypeError):
            repository.list_pending_parts(limit="1")
        with pytest.raises(TypeError):
            repository.list_pending_parts(limit=True)
        with pytest.raises(ValueError):
            repository.list_pending_parts(limit=0)
    finally:
        connection.close()


def test_start_run_requires_a_foreign_key_parent(tmp_root):
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            repository.start_run(make_run_record())
    finally:
        connection.close()


def test_start_run_rejects_a_duplicate_run_id(tmp_root):
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        _start_run(repository)
        with pytest.raises(sqlite3.IntegrityError):
            repository.start_run(make_run_record("run-1"))
    finally:
        connection.close()


def test_upsert_part_rejects_an_explicit_video_part_id(tmp_root):
    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        with repository.transaction():
            repository.upsert_user(make_user_record())
            repository.upsert_video(make_video_record())
            with pytest.raises(ValueError):
                repository.upsert_part(make_part_record(video_part_id=7))
    finally:
        connection.close()


# ------------------------------------------------------------ video tags


def _tag(tag_id: int = 943, name: str = "爱情", bvid: str = "BV1SINGLE"):
    """Build one tag record for the fixture video."""

    return VideoTagRecord(
        bvid=bvid, tag_id=tag_id, tag_name=name, tag_type="old_channel"
    )


def _stored_tags(connection, bvid: str = "BV1SINGLE") -> list[tuple]:
    rows = connection.execute(
        "SELECT tag_id, tag_name, tag_type FROM video_tags WHERE bvid = ?"
        " ORDER BY tag_id",
        (bvid,),
    ).fetchall()
    return [tuple(row) for row in rows]


def test_upsert_video_tags_requires_the_video_row(tmp_root):
    """The tag row's foreign key is enforced, not implied."""

    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with repository.transaction():
                repository.upsert_video_tags("BV1SINGLE", [_tag()])
    finally:
        connection.close()


def test_upsert_video_tags_replaces_instead_of_appending(tmp_root):
    """A second call converges on the new set; it does not accumulate."""

    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        with repository.transaction():
            repository.upsert_user(make_user_record())
            repository.upsert_video(make_video_record())
            repository.upsert_video_tags("BV1SINGLE", [_tag(1, "一"), _tag(2, "二")])
        assert _stored_tags(connection) == [
            (1, "一", "old_channel"),
            (2, "二", "old_channel"),
        ]

        with repository.transaction():
            repository.upsert_video_tags("BV1SINGLE", [_tag(1, "改"), _tag(3, "三")])

        assert _stored_tags(connection) == [
            (1, "改", "old_channel"),
            (3, "三", "old_channel"),
        ]
    finally:
        connection.close()


def test_upsert_video_tags_with_no_records_clears_the_set(tmp_root):
    """An empty observation is how a set is emptied."""

    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        with repository.transaction():
            repository.upsert_user(make_user_record())
            repository.upsert_video(make_video_record())
            repository.upsert_video_tags("BV1SINGLE", [_tag()])
        with repository.transaction():
            repository.upsert_video_tags("BV1SINGLE")

        assert _stored_tags(connection) == []
        # The call is idempotent: clearing an already-empty set is not an error.
        with repository.transaction():
            repository.upsert_video_tags("BV1SINGLE")
    finally:
        connection.close()


def test_upsert_video_tags_leaves_other_videos_alone(tmp_root):
    """Replacing one video's set never touches another's rows."""

    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        with repository.transaction():
            repository.upsert_user(make_user_record())
            repository.upsert_video(make_video_record())
            repository.upsert_video(make_video_record(bvid="BV1OTHER", aid=2002))
            repository.upsert_video_tags("BV1SINGLE", [_tag(1, "一")])
            repository.upsert_video_tags(
                "BV1OTHER", [_tag(1, "一", "BV1OTHER"), _tag(2, "二", "BV1OTHER")]
            )

        with repository.transaction():
            repository.upsert_video_tags("BV1SINGLE", [_tag(9, "九")])

        assert _stored_tags(connection) == [(9, "九", "old_channel")]
        assert _stored_tags(connection, "BV1OTHER") == [
            (1, "一", "old_channel"),
            (2, "二", "old_channel"),
        ]
    finally:
        connection.close()


def test_upsert_video_tags_refuses_a_record_for_another_video(tmp_root):
    """A mismatched record is refused rather than written under another key."""

    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        with repository.transaction():
            repository.upsert_user(make_user_record())
            repository.upsert_video(make_video_record())
            with pytest.raises(ValueError):
                repository.upsert_video_tags("BV1SINGLE", [_tag(bvid="BV1OTHER")])
            with pytest.raises(TypeError):
                repository.upsert_video_tags("BV1SINGLE", [("not", "a", "record")])
    finally:
        connection.close()


def test_upsert_video_tags_rolls_back_with_its_transaction(tmp_root):
    """A failed group leaves no tag half-state behind."""

    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        with repository.transaction():
            repository.upsert_user(make_user_record())
            repository.upsert_video(make_video_record())
            repository.upsert_video_tags("BV1SINGLE", [_tag(1, "一")])

        with pytest.raises(RuntimeError):
            with repository.transaction():
                repository.upsert_video_tags("BV1SINGLE", [_tag(2, "二")])
                raise RuntimeError("later step failed")

        assert _stored_tags(connection) == [(1, "一", "old_channel")]
    finally:
        connection.close()


def test_record_page_absent_tag_key_is_no_news_for_a_stored_set(tmp_root):
    """``record_page`` omits a video's tag writes when its key is absent.

    This is the storage half of compass **D16**, asserted at the source level
    rather than through the ingestor that happens to pass ``None``: an absent
    bvid must leave whatever rows exist for that video alone.  ``tags=None``
    (no tag sets at all) and ``tags={}`` (an empty mapping) are both "no news"
    for the video, which is precisely why the ingestor can encode a degraded
    fetch as a missing key instead of an empty list.
    """

    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        _start_run(repository)
        with repository.transaction():
            repository.upsert_video(make_video_record())
            repository.upsert_video_tags("BV1SINGLE", [_tag(1, "一"), _tag(2, "二")])

        # A page that recorded no tag sets at all, then one whose mapping is
        # empty: neither may touch the stored set.
        repository.record_page(make_page_record(page_number=2), tags=None)
        repository.record_page(make_page_record(page_number=3), tags={})

        assert _stored_tags(connection) == [
            (1, "一", "old_channel"),
            (2, "二", "old_channel"),
        ]
    finally:
        connection.close()


def test_record_page_present_empty_tag_set_clears_the_video(tmp_root):
    """A key present with an empty iterable *is* the observation that clears.

    The other half of D16, and the reason the distinction cannot be moved into
    ``upsert_video_tags``: the absent key above and this present-but-empty key
    drive opposite outcomes from the same payload channel, so a conditional
    delete inside ``upsert_video_tags`` would destroy this arm while trying to
    protect that one.
    """

    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        _start_run(repository)
        with repository.transaction():
            repository.upsert_video(make_video_record())
            repository.upsert_video_tags("BV1SINGLE", [_tag(1, "一"), _tag(2, "二")])

        repository.record_page(make_page_record(page_number=2), tags={"BV1SINGLE": []})

        assert _stored_tags(connection) == []
    finally:
        connection.close()


def test_record_page_tag_write_alone_is_a_payload(tmp_root):
    """A page whose only payload is tags still takes the payload path.

    ``has_payload`` gained ``or tag_sets is not None`` for this case, and
    without it a tags-only page would fall into the no-payload branch and
    silently drop the tag write.  The other arguments are left at their
    defaults deliberately: this is the one caller shape that distinguishes the
    clause from the surrounding disjunction.
    """

    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        _start_run(repository)
        with repository.transaction():
            repository.upsert_video(make_video_record())

        repository.record_page(
            make_page_record(page_number=2),
            tags={"BV1SINGLE": [_tag(1, "一")]},
        )

        assert _stored_tags(connection) == [(1, "一", "old_channel")]
        # The page row landed too, so the payload transaction committed rather
        # than the no-payload single-write path having been taken.
        assert tuple(
            connection.execute(
                "SELECT outcome FROM ingestion_pages WHERE page_number = 2"
            ).fetchone()
        ) == ("ok",)
    finally:
        connection.close()


def test_record_page_tag_foreign_key_is_enforced_within_the_payload(tmp_root):
    """A tag set for a video the archive does not hold fails the transaction.

    ``record_page`` writes tags after the video upserts and lets the foreign
    key decide rather than silently dropping the observation; the whole page
    rolls back.  This is the payload-path variant of the FK pin that
    ``upsert_video_tags`` already carries.
    """

    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        _start_run(repository)
        with pytest.raises(sqlite3.IntegrityError):
            repository.record_page(
                make_page_record(),
                tags={"BV-MISSING": [_tag(1, "一", "BV-MISSING")]},
            )

        assert connection.execute("SELECT COUNT(*) FROM ingestion_pages").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM video_tags").fetchone()[0] == 0
    finally:
        connection.close()


# --------------------------------------------------------- video details


def _details(
    bvid: str = "BV1SINGLE",
    *,
    pic: str | None = "http://i1.hdslb.com/bfs/archive/cover.jpg",
    desc: str | None = "哲学讲座简介",
    tid: int | None = 124,
    observed_at: int = 200,
) -> VideoDetailRecord:
    """Build one details record for the fixture video."""

    return VideoDetailRecord(
        bvid=bvid, pic=pic, desc=desc, tid=tid, observed_at=observed_at
    )


def _stored_details(connection, bvid: str = "BV1SINGLE") -> tuple | None:
    row = connection.execute(
        'SELECT pic, "desc", tid, observed_at FROM video_details WHERE bvid = ?',
        (bvid,),
    ).fetchone()
    return None if row is None else tuple(row)


def test_upsert_video_details_requires_the_video_row(tmp_root):
    """The details row's foreign key is enforced, not implied."""

    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with repository.transaction():
                repository.upsert_video_details(_details())
    finally:
        connection.close()


def test_upsert_video_details_refreshes_one_row_instead_of_appending(tmp_root):
    """D11 at the write: the row is the current value, not a dated series."""

    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        with repository.transaction():
            repository.upsert_user(make_user_record())
            repository.upsert_video(make_video_record())
            repository.upsert_video_details(_details(observed_at=200))

        with repository.transaction():
            repository.upsert_video_details(
                _details(pic="http://i1.hdslb.com/bfs/archive/new.jpg",
                         desc=None, tid=None, observed_at=300)
            )

        assert (
            connection.execute(
                "SELECT COUNT(*) FROM video_details WHERE bvid = 'BV1SINGLE'"
            ).fetchone()[0]
            == 1
        )
        assert _stored_details(connection) == (
            "http://i1.hdslb.com/bfs/archive/new.jpg",
            None,
            None,
            300,
        )
    finally:
        connection.close()


def test_upsert_video_details_all_null_observation_touches_nothing(tmp_root):
    """D15 at the write: a collection that observed nothing moves nothing.

    Asserted on the whole row — the three values **and** ``observed_at`` —
    because the defect D15 rules on is precisely an unconditional
    ``DO UPDATE SET`` that blanks the values and stamps them fresh.
    """

    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        with repository.transaction():
            repository.upsert_user(make_user_record())
            repository.upsert_video(make_video_record())
            repository.upsert_video_details(_details(observed_at=200))
        observed = _stored_details(connection)

        with repository.transaction():
            repository.upsert_video_details(
                _details(pic=None, desc=None, tid=None, observed_at=999)
            )

        assert observed is not None
        assert _stored_details(connection) == observed
    finally:
        connection.close()


def test_upsert_video_details_advances_for_a_desc_only_observation(tmp_root):
    """D15's "at least one of three", pinned where a `pic`-keyed guard fails.

    The rule is "advances when >=1 of ``pic``/``desc``/``tid`` was observed".
    A guard keyed on ``pic`` alone satisfies every other case in this file --
    measured: replacing the three-term guard with ``if details.pic is None:``
    left the whole four-file gate green (481 passed, 1 skipped). This arm is
    the one that separates the rule from that mutant: ``pic`` is ``None`` here
    while ``desc`` is not, so a ``pic``-only guard returns early and the stamp
    never moves.
    """

    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        with repository.transaction():
            repository.upsert_user(make_user_record())
            repository.upsert_video(make_video_record())
            repository.upsert_video_details(_details(observed_at=200))
        first = _stored_details(connection)

        with repository.transaction():
            repository.upsert_video_details(
                _details(pic=None, desc="只观测到简介", tid=None, observed_at=777)
            )
        stored = _stored_details(connection)

        # The observation was real, so the row is refreshed and the stamp moves.
        assert stored is not None
        assert first is not None
        assert stored[1] == "只观测到简介"
        assert stored[3] == 777 and stored[3] > first[3]
    finally:
        connection.close()


def test_upsert_video_details_all_null_observation_writes_no_row(tmp_root):
    """A video whose only observation is all-``NULL`` has nothing written.

    The second half of D15: without it, an implementation that inserts an
    all-``NULL`` row on the first observation and then guards every update
    would satisfy "leaves the row untouched" while writing the very row the
    ruling says the table must not offer.
    """

    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        with repository.transaction():
            repository.upsert_user(make_user_record())
            repository.upsert_video(make_video_record())
            repository.upsert_video_details(
                _details(pic=None, desc=None, tid=None, observed_at=999)
            )

        assert _stored_details(connection) is None
    finally:
        connection.close()


def test_upsert_video_details_rolls_back_with_its_transaction(tmp_root):
    """The write is the caller's transaction's, like every sibling upsert."""

    connection = open_database(tmp_root)
    repository = MetadataRepository(connection)
    try:
        with repository.transaction():
            repository.upsert_user(make_user_record())
            repository.upsert_video(make_video_record())

        with pytest.raises(RuntimeError):
            with repository.transaction():
                repository.upsert_video_details(_details(observed_at=200))
                raise RuntimeError("payload failure")

        assert _stored_details(connection) is None
    finally:
        connection.close()


def test_constructor_rejects_a_connection_without_row_factory(tmp_root):
    connection = sqlite3.connect(os.path.join(tmp_root, "bare.db"))
    try:
        with pytest.raises(TypeError):
            MetadataRepository(connection)
    finally:
        connection.close()


def test_constructor_rejects_a_connection_with_foreign_keys_disabled(tmp_root):
    connection = sqlite3.connect(os.path.join(tmp_root, "fk-off.db"))
    connection.row_factory = sqlite3.Row
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        with pytest.raises(ValueError):
            MetadataRepository(connection)
    finally:
        connection.close()
