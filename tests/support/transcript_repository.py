from __future__ import annotations
import builtins
from dataclasses import replace
import hashlib
import io
import json
import os
import re
import sqlite3
import pytest
from bili_asr.storage import (
    MAX_TIMELINE_MS,
    AcquisitionRunRecord,
    MetadataRepository,
    SchemaContractError,
    TranscriptRecord,
    TranscriptRepository,
    TranscriptSegmentRecord,
    TranscriptWriteResult,
    open_database,
)
from bili_asr.storage.database import _PUBDATE_CHUNK
from tests.fixtures.metadata_records import (
    make_part_record,
    make_user_record,
    make_video_record,
)
from tests.support.storage_schema import _write_pre_iteration_database


LEGACY_SIDECAR_PATHS = (
    os.path.join("manifest", "manifest.jsonl"),
    "meta-cursor.json",
    "run-ledger.jsonl",
)


BODY = ((0, 1_200, "第一句"), (1_200, 2_400, "第二句"))


CHANGED_BODY = ((0, 1_200, "第一句"), (1_200, 2_400, "改写后的第二句"))


def _video_with_parts(
    connection: sqlite3.Connection,
    bvid: str,
    cids: tuple[int, ...],
    *,
    processing_status: str = "metadata_collected",
    pubdate: int = 1_700_000_000,
) -> dict[int, int]:
    """Store one video with one part per page index; return ``page → part id``.

    ``cids`` is positional: the cid at position *i* belongs to page index *i*,
    so a selection test can name the part it expects by its cid.  ``pubdate`` is
    the video's stored publication second, which a read carrying it proves by
    two videos differing on it rather than by one constant.
    """
    metadata = MetadataRepository(connection)
    with metadata.transaction():
        metadata.upsert_user(make_user_record())
        # ``aid`` stays NULL: the schema keeps aids unique and these fixtures
        # only need the parts the transcript contract hangs from.
        metadata.upsert_video(
            replace(
                make_video_record(bvid, aid=None, title="字幕测试视频"), pubdate=pubdate
            )
        )
        for page_index, cid in enumerate(cids):
            metadata.upsert_part(
                make_part_record(
                    bvid,
                    page_index=page_index,
                    cid=cid,
                    title=f"第{page_index + 1}集",
                    processing_status=processing_status,
                )
            )
    return {
        int(row["page_index"]): int(row["video_part_id"])
        for row in connection.execute(
            "SELECT video_part_id, page_index FROM video_parts WHERE bvid = ?",
            (bvid,),
        ).fetchall()
    }


def _caption_run(
    run_id: str = "caption-run-1",
    *,
    selector_kind: str = "pending",
    selector_target: str | None = None,
    requested_limit: int | None = None,
    credential_present: bool = False,
    started_at: int = 101,
    outcome: str = "running",
    finished_at: int | None = None,
) -> AcquisitionRunRecord:
    """Build one acquisition run record for the caption path."""
    return AcquisitionRunRecord(
        run_id=run_id,
        kind="subtitle",
        selector_kind=selector_kind,
        selector_target=selector_target,
        requested_limit=requested_limit,
        credential_present=credential_present,
        started_at=started_at,
        outcome=outcome,
        finished_at=finished_at,
    )


def _run(repository: TranscriptRepository, index: int, **overrides) -> str:
    """Start one numbered acquisition run and return its id."""
    run_id = f"caption-run-{index}"
    fields: dict[str, object] = {"started_at": 100 + index}
    fields.update(overrides)
    repository.start_acquisition_run(_caption_run(run_id, **fields))
    return run_id


def _segments(body=BODY) -> tuple[TranscriptSegmentRecord, ...]:
    """Build the segment tuple for one caption body of ``(start, end, text)``."""
    return tuple(TranscriptSegmentRecord(*triple) for triple in body)


def _write_kwargs(video_part_id: int, *, body=BODY, **overrides) -> dict:
    """Build one valid ``record_acquired_transcript`` argument set."""
    kwargs: dict[str, object] = {
        "run_id": "caption-run-1",
        "video_part_id": video_part_id,
        "source_kind": "subtitle-cc",
        "language": "zh-CN",
        "segments": _segments(body),
        "started_at": 200,
        "finished_at": 300,
        "created_at": 400,
    }
    kwargs.update(overrides)
    return kwargs


def _record(
    repository: TranscriptRepository, video_part_id: int, **overrides
) -> TranscriptWriteResult:
    """Record one caption body and return what the write did."""
    return repository.record_acquired_transcript(
        **_write_kwargs(video_part_id, **overrides)
    )
