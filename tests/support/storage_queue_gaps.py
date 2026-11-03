from __future__ import annotations
import sqlite3
from typing import get_args
import pytest
from bili_asr.storage import (
    ALLOWED_QUEUE_GAPS,
    AcquisitionRunRecord,
    MediaQueueRepository,
    MetadataRepository,
    QueueGap,
    QueueGapItem,
    SchemaContractError,
    TranscriptRepository,
    TranscriptSegmentRecord,
    UserRecord,
    VideoPartRecord,
    VideoRecord,
    open_database,
)
from bili_asr.services.queue_source import entry_for_item


_MID = 23191782


_VIDEOS = (
    ("BV1AAA", 3_000, "三秒的视频"),
    ("BV1BBB", 2_000, "两秒的视频"),
    ("BV1CCC", 1_000, "一秒的视频"),
    ("BV1DDD", 500, "下架的视频"),
    ("BV1EEE", 3_000, "另一个三秒的视频"),
    ("BV1FFF", 1_500, "一秒半的视频"),
)


_PARTS = (
    ("BV1AAA", 0, "discovered"),
    ("BV1AAA", 1, "discovered"),
    ("BV1BBB", 0, "discovered"),
    ("BV1CCC", 0, "discovered"),
    ("BV1DDD", 0, "gone"),
    ("BV1EEE", 0, "discovered"),
    ("BV1FFF", 0, "discovered"),
)


def _open_run(
    transcripts: TranscriptRepository, run_id: str, kind: str,
    *, credential_present: bool = True,
) -> None:
    """Open one parent run of ``kind`` so attempt rows have their foreign key."""
    transcripts.start_acquisition_run(
        AcquisitionRunRecord(
            run_id=run_id,
            kind=kind,
            selector_kind="pending",
            selector_target=None,
            requested_limit=None,
            credential_present=credential_present,
            started_at=200,
        )
    )


def _audio_attempt(
    connection: sqlite3.Connection, run_id: str, video_part_id: int
) -> None:
    """Insert one failed audio attempt row directly.

    This is *not* audio evidence: the attempt contract's ``kind='audio'`` shape
    is a ``'failed'`` outcome with a bounded ``error_code`` and no transcript
    (``stored`` / ``unchanged`` require a ``transcript_id`` an audio download
    never produces), so a row here records a download that produced nothing.
    """
    connection.execute(
        """
        INSERT INTO acquisition_attempts(
            run_id, video_part_id, outcome, error_code, transcript_id,
            started_at, finished_at
        ) VALUES (?, ?, 'failed', 'audio_unavailable', NULL, 700, 800)
        """,
        (run_id, video_part_id),
    )
    connection.commit()


def _archive_audio(
    connection: sqlite3.Connection,
    *,
    audio_id: int,
    video_part_id: int,
    sha256: str,
    storage_key: str,
) -> None:
    """Insert the positive audio evidence: one object row and its part link."""
    connection.execute(
        """
        INSERT INTO audio_objects(
            audio_id, sha256, byte_size, format, duration_ms, storage_key, created_at
        ) VALUES (?, ?, 4096, 'm4a', 1234, ?, 650)
        """,
        (audio_id, sha256, storage_key),
    )
    connection.execute(
        """
        INSERT INTO part_audio_objects(
            video_part_id, audio_id, acquired_at, acquisition_source
        ) VALUES (?, ?, 650, 'download')
        """,
        (video_part_id, audio_id),
    )
    connection.commit()


def _seed(
    connection: sqlite3.Connection,
) -> tuple[dict[tuple[str, int], int], MediaQueueRepository]:
    """Store the fixture's videos, parts, runs and attempts; return the ids."""
    metadata = MetadataRepository(connection)
    transcripts = TranscriptRepository(connection)
    with metadata.transaction():
        metadata.upsert_user(
            UserRecord(mid=_MID, display_name="未明子", created_at=100, updated_at=100)
        )
        for bvid, pubdate, title in _VIDEOS:
            metadata.upsert_video(
                VideoRecord(
                    bvid=bvid,
                    aid=None,
                    mid=_MID,
                    title=title,
                    pubdate=pubdate,
                    created_at=101,
                    updated_at=101,
                )
            )
        parts = {
            (bvid, page_index): metadata.upsert_part(
                VideoPartRecord(
                    bvid=bvid,
                    page_index=page_index,
                    cid=2_000 + index,
                    title=f"{bvid} 第 {page_index} 段",
                    duration_ms=1_000 + index,
                    processing_status=status,
                    created_at=102,
                    updated_at=102,
                )
            )
            for index, (bvid, page_index, status) in enumerate(_PARTS, start=1)
        }

    # BV1AAA:p1 — three subtitle attempts; the two newest independently listed
    # nothing.  Exhaustion is attested, not inferred from one look: an empty
    # inventory carrying no error code is an *indefinite* negative (this
    # credential may simply not have seen the part), so it admits only once two
    # distinct runs have observed it empty.  Hence two runs here, not one.
    _open_run(transcripts, "run-sub-old", "subtitle")
    transcripts.record_subtitle_attempt(
        run_id="run-sub-old",
        video_part_id=parts[("BV1AAA", 1)],
        outcome="failed",
        error_code="upstream_timeout",
        started_at=300,
        finished_at=400,
    )
    _open_run(transcripts, "run-sub-new", "subtitle")
    transcripts.record_subtitle_attempt(
        run_id="run-sub-new",
        video_part_id=parts[("BV1AAA", 1)],
        outcome="no-subtitle",
        error_code=None,
        started_at=500,
        finished_at=600,
        credential_verified=True,
    )
    _open_run(transcripts, "run-sub-confirm", "subtitle")
    transcripts.record_subtitle_attempt(
        run_id="run-sub-confirm",
        video_part_id=parts[("BV1AAA", 1)],
        outcome="no-subtitle",
        error_code=None,
        started_at=700,
        finished_at=800,
        credential_verified=True,
    )

    # BV1BBB:p0 — audio was attempted (and the attempt failed), then archived:
    # the object link is the evidence, the attempt row is history.
    _open_run(transcripts, "run-audio-bbb", "audio")
    _audio_attempt(connection, "run-audio-bbb", parts[("BV1BBB", 0)])
    _archive_audio(
        connection,
        audio_id=1,
        video_part_id=parts[("BV1BBB", 0)],
        sha256="a" * 64,
        storage_key="audio/BV1BBB-p0.m4a",
    )

    # BV1CCC:p0 — a stored transcript leaves every queue.
    _open_run(transcripts, "run-sub-ccc", "subtitle")
    transcripts.record_acquired_transcript(
        run_id="run-sub-ccc",
        video_part_id=parts[("BV1CCC", 0)],
        source_kind="subtitle-ai",
        language="zh-CN",
        segments=(TranscriptSegmentRecord(start_ms=0, end_ms=1_000, text="第一句"),),
        started_at=900,
        finished_at=1_000,
        created_at=1_001,
    )

    # BV1DDD:p0 — gone, so only the view that does not filter status holds it.
    # Its audio attempt also failed; only the archived object counts.
    _open_run(transcripts, "run-audio-ddd", "audio")
    _audio_attempt(connection, "run-audio-ddd", parts[("BV1DDD", 0)])
    _archive_audio(
        connection,
        audio_id=2,
        video_part_id=parts[("BV1DDD", 0)],
        sha256="b" * 64,
        storage_key="audio/BV1DDD-p0.m4a",
    )

    # BV1FFF:p0 — one definite subtitle absence, with bounded not-found evidence.
    # Its audio download
    # also failed and archived nothing, so the part must *stay* in the audio
    # queue: the attempt row is rotation history, never a claim that bytes
    # exist.  (Pre-fix this row read as audio evidence and pushed the part into
    # the transcript queue with no audio on disk.)
    _open_run(transcripts, "run-sub-fff", "subtitle")
    transcripts.record_subtitle_attempt(
        run_id="run-sub-fff",
        video_part_id=parts[("BV1FFF", 0)],
        outcome="no-subtitle",
        error_code="not_found",
        absence_verified=True,
        started_at=1_100,
        finished_at=1_200,
    )
    _open_run(transcripts, "run-audio-fff", "audio")
    _audio_attempt(connection, "run-audio-fff", parts[("BV1FFF", 0)])
    return parts, MediaQueueRepository(connection)
