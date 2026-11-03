from __future__ import annotations
import bili_asr.storage.database as _module_storage_database
from importlib import resources
import os
from pathlib import Path
import re
import sqlite3
import tomllib
from typing import get_args
import pytest
from bili_asr.storage import (
    MetadataRepository,
    TranscriptRepository,
    SchemaContractError,
    duration_to_ms,
    normalize_page_index,
    open_database,
    refresh_shipped_views,
    require_subtitle_schema,
)
from bili_asr.storage.database import (
    _normalize_view_sql,
    _shipped_view_bodies,
    _statement_view_name,
    _strip_sql_comments,
)
from bili_asr.storage.models import (
    ALLOWED_ACQUISITION_KINDS,
    ALLOWED_ACQUISITION_OUTCOMES,
    ALLOWED_ATTEMPT_OUTCOMES,
    ALLOWED_CURSOR_STATES,
    ALLOWED_PAGE_OUTCOMES,
    ALLOWED_PROCESSING_STATUS,
    ALLOWED_RUN_OUTCOMES,
    ALLOWED_SOURCE_KINDS,
    AcquisitionKind,
    AcquisitionOutcome,
    AcquisitionRunRecord,
    AttemptOutcome,
    CursorRecord,
    CursorState,
    PageOutcome,
    ProcessingStatus,
    RunOutcome,
    SourceKind,
    TranscriptSegmentRecord,
    TranscriptWriteResult,
)


LEGACY_TRANSCRIPT_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS transcripts (
    transcript_id INTEGER PRIMARY KEY,
    video_part_id INTEGER NOT NULL,
    source_kind TEXT NOT NULL CHECK (
        source_kind IN ('subtitle-ai', 'subtitle-cc', 'asr-local')
    ),
    model_id INTEGER,
    version INTEGER NOT NULL CHECK (version > 0),
    created_at INTEGER NOT NULL,
    UNIQUE (video_part_id, source_kind, version),
    FOREIGN KEY (video_part_id) REFERENCES video_parts(video_part_id) ON DELETE RESTRICT,
    FOREIGN KEY (model_id) REFERENCES asr_models(model_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS transcript_segments (
    transcript_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK (end_ms > start_ms),
    text TEXT NOT NULL,
    PRIMARY KEY (transcript_id, ordinal),
    FOREIGN KEY (transcript_id) REFERENCES transcripts(transcript_id) ON DELETE RESTRICT
);
"""


def _insert_user_video_part(connection: sqlite3.Connection) -> int:
    connection.execute(
        "INSERT INTO bilibili_users(mid, display_name, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (23191782, "未明子", 100, 100),
    )
    connection.execute(
        "INSERT INTO videos(bvid, aid, mid, title, pubdate, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("BV1TEST", 1001, 23191782, "视频", 1_700_000_000, 101, 101),
    )
    cursor = connection.execute(
        """
        INSERT INTO video_parts(
            bvid, page_index, cid, title, duration_ms, processing_status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("BV1TEST", 0, 2001, "第一段", 1_234, "discovered", 102, 102),
    )
    return int(cursor.lastrowid)


def _stored_ddl(connection: sqlite3.Connection, name: str) -> str:
    """Return the DDL SQLite recorded for one table or view."""
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
    ).fetchone()
    assert row is not None, name
    return str(row[0])


def _write_pre_iteration_database(database_path: str) -> dict[str, str]:
    """Build the previous iteration's database and return its transcript DDL.

    The metadata script is the shipped one; the transcript block is the shape
    iteration ``20260909-structured-metadata-schema`` created, which
    ``CREATE TABLE IF NOT EXISTS`` cannot widen.
    """
    metadata_schema = (
        resources.files("bili_asr.storage").joinpath("schema.sql").read_text("utf-8")
    )
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(metadata_schema)
        connection.executescript(LEGACY_TRANSCRIPT_TABLES_DDL)
        part_id = _insert_user_video_part(connection)
        connection.execute(
            """
            INSERT INTO transcripts(
                transcript_id, video_part_id, source_kind, model_id, version,
                created_at
            ) VALUES (1, ?, 'subtitle-ai', NULL, 1, 500)
            """,
            (part_id,),
        )
        connection.execute(
            """
            INSERT INTO transcript_segments(
                transcript_id, ordinal, start_ms, end_ms, text
            ) VALUES (1, 0, 0, 1200, '旧字幕')
            """
        )
        connection.commit()
        return {
            name: _stored_ddl(connection, name)
            for name in ("transcripts", "transcript_segments")
        }
    finally:
        connection.close()
