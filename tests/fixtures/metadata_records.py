"""Deterministic record factories for the offline metadata contract tests.

Every builder returns a validated internal record with fixed literal field
values, so repeated runs observe identical rows.  Keyword arguments override
single fields; no wall-clock time, randomness, network data, third-party
response objects, or credentials are involved.
"""

from __future__ import annotations

from bili_asr.storage.models import (
    CursorRecord,
    CursorState,
    DiscoveryRecord,
    IngestionPageRecord,
    IngestionRunRecord,
    PageOutcome,
    ProcessingStatus,
    RunOutcome,
    UserRecord,
    VideoPartRecord,
    VideoRecord,
)


MID = 23191782


def make_user_record(
    *, display_name: str = "未明子", updated_at: int = 100
) -> UserRecord:
    """Build the archive owner's current user record."""
    return UserRecord(
        mid=MID,
        display_name=display_name,
        created_at=100,
        updated_at=updated_at,
    )


def make_run_record(
    run_id: str = "run-1",
    *,
    requested_start_page: int = 1,
    requested_page_limit: int | None = 3,
    started_at: int = 101,
    outcome: RunOutcome = "running",
    finished_at: int | None = None,
) -> IngestionRunRecord:
    """Build one metadata collection run opened against the archive owner."""
    return IngestionRunRecord(
        run_id=run_id,
        mid=MID,
        source_package="bilibili-api-python",
        source_version="17.4.2",
        requested_start_page=requested_start_page,
        requested_page_limit=requested_page_limit,
        started_at=started_at,
        outcome=outcome,
        finished_at=finished_at,
    )


def make_video_record(
    bvid: str = "BV1SINGLE",
    *,
    title: str = "单集视频",
    aid: int | None = 1001,
    updated_at: int = 102,
) -> VideoRecord:
    """Build a video owned by the archive owner."""
    return VideoRecord(
        bvid=bvid,
        aid=aid,
        mid=MID,
        title=title,
        pubdate=1_700_000_000,
        created_at=102,
        updated_at=updated_at,
    )


def make_part_record(
    bvid: str = "BV1SINGLE",
    *,
    page_index: int = 0,
    cid: int = 2001,
    title: str = "第一集",
    processing_status: ProcessingStatus = "discovered",
    updated_at: int = 103,
    video_part_id: int | None = None,
) -> VideoPartRecord:
    """Build one normalized part of a video."""
    return VideoPartRecord(
        bvid=bvid,
        page_index=page_index,
        cid=cid,
        title=title,
        duration_ms=1_234,
        processing_status=processing_status,
        created_at=103,
        updated_at=updated_at,
        video_part_id=video_part_id,
    )


def make_page_record(
    run_id: str = "run-1",
    *,
    page_number: int = 1,
    outcome: PageOutcome = "ok",
    error_code: str | None = None,
    started_at: int = 110,
    finished_at: int = 111,
) -> IngestionPageRecord:
    """Build one page-outcome record for a run."""
    return IngestionPageRecord(
        run_id=run_id,
        page_number=page_number,
        outcome=outcome,
        error_code=error_code,
        started_at=started_at,
        finished_at=finished_at,
    )


def make_cursor_record(
    *,
    next_page: int = 2,
    observed_total: int | None = 2,
    state: CursorState = "ready",
    last_error_code: str | None = None,
    updated_at: int = 112,
) -> CursorRecord:
    """Build the resumable cursor state for the archive owner."""
    return CursorRecord(
        mid=MID,
        next_page=next_page,
        observed_total=observed_total,
        state=state,
        last_error_code=last_error_code,
        updated_at=updated_at,
    )


def make_discovery_record(
    bvid: str,
    *,
    run_id: str = "run-1",
    page_number: int = 1,
    source_position: int | None = 0,
    discovered_at: int = 104,
) -> DiscoveryRecord:
    """Build one run/page/video discovery relationship."""
    return DiscoveryRecord(
        run_id=run_id,
        page_number=page_number,
        bvid=bvid,
        source_position=source_position,
        discovered_at=discovered_at,
    )


__all__ = [
    "MID",
    "make_cursor_record",
    "make_discovery_record",
    "make_page_record",
    "make_run_record",
    "make_part_record",
    "make_user_record",
    "make_video_record",
]
