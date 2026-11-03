from __future__ import annotations
from contextlib import contextmanager
from dataclasses import replace
import os
import sqlite3
import threading
import time
from bili_asr.cli.main import main
from bili_asr.config import ARCHIVE_DATABASE_NAME
from bili_asr.pipeline.locks import ARCHIVE_WRITER_LOCK
from bili_asr.manifest import JOURNAL_NAME, ManifestStore
from bili_asr.persistence import file_lock
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


def _seed_archive(
    root: str,
    parts: tuple[tuple[str, int, int, int, str], ...],
    *,
    captioned: tuple[tuple[str, int], ...] = (),
    pubdates: dict[str, int] | None = None,
) -> None:
    """Create ``archive.db`` with one video per bvid and exactly these parts.

    ``parts`` is ``(bvid, page_index, cid, duration_ms, processing_status)`` and
    ``captioned`` names the ``(bvid, page_index)`` parts that hold a stored
    ``subtitle-ai`` transcript, so each test scripts which parts the store's own
    queue relation holds.  ``pubdates`` overrides the factory's fixed publication
    second for the named bvids — the one field a case varies here, because it is
    the field the derived row renders as a date.
    """
    connection = open_database(root)
    try:
        metadata = MetadataRepository(connection)
        with metadata.transaction():
            metadata.upsert_user(make_user_record())
            for bvid in dict.fromkeys(
                bvid for bvid, _page, _cid, _ms, _status in parts
            ):
                video = make_video_record(bvid, aid=None, title="队列测试视频")
                if pubdates and bvid in pubdates:
                    video = replace(video, pubdate=pubdates[bvid])
                metadata.upsert_video(video)
            for bvid, page_index, cid, duration_ms, status in parts:
                metadata.upsert_part(
                    replace(
                        make_part_record(
                            bvid,
                            page_index=page_index,
                            cid=cid,
                            title=f"第{page_index + 1}集",
                            processing_status=status,
                        ),
                        duration_ms=duration_ms,
                    )
                )
    finally:
        connection.close()
    for bvid, page_index in captioned:
        _record_caption(root, bvid, page_index)


def _record_caption(root: str, bvid: str, page_index: int) -> None:
    """Store one acquired ``subtitle-ai`` transcript through the repository."""
    connection = open_database(root)
    try:
        repository = TranscriptRepository(connection)
        part_id = int(
            connection.execute(
                "SELECT video_part_id FROM video_parts "
                "WHERE bvid = ? AND page_index = ?",
                (bvid, page_index),
            ).fetchone()["video_part_id"]
        )
        run_id = f"caption-run-{bvid}-p{page_index}"
        repository.start_acquisition_run(
            AcquisitionRunRecord(
                run_id=run_id,
                kind="subtitle",
                selector_kind="pending",
                selector_target=None,
                requested_limit=None,
                credential_present=False,
                started_at=101,
            )
        )
        repository.record_acquired_transcript(
            run_id=run_id,
            video_part_id=part_id,
            source_kind="subtitle-ai",
            language="zh-CN",
            segments=(TranscriptSegmentRecord(0, 1_200, "第一句"),),
            started_at=200,
            finished_at=300,
            created_at=400,
        )
    finally:
        connection.close()


@contextmanager
def _archive_connection(root: str):
    """Read the archive database directly, without creating or migrating it."""
    connection = sqlite3.connect(os.path.join(root, ARCHIVE_DATABASE_NAME))
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _exit_code(argv: list[str]) -> int:
    """Return the command's exit code, argparse usage errors included."""
    try:
        return main(argv)
    except SystemExit as exit_signal:
        return int(exit_signal.code)


def _derive(root: str) -> int:
    """Run the command the way an operator does, and return its exit code."""
    return _exit_code(["derive-manifest", "--archive-root", root])
