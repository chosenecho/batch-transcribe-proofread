"""Offline contract tests for the normalized SQLite storage schema."""

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
from tests.support.storage_schema import LEGACY_TRANSCRIPT_TABLES_DDL, _insert_user_video_part, _stored_ddl, _write_pre_iteration_database


BASE_TABLES = {
    "bilibili_users",
    "videos",
    "video_parts",
    "video_tags",
    "video_details",
    "ingestion_runs",
    "ingestion_cursors",
    "ingestion_pages",
    "ingestion_discoveries",
    "audio_objects",
    "part_audio_objects",
    "asr_models",
    "transcripts",
    "transcript_segments",
    "acquisition_runs",
    "acquisition_attempts",
    "transcript_coverage_attestations",
}
VIEWS = {
    "v_video_parts",
    "v_ingestion_run_stats",
    "v_pending_metadata",
    "v_pending_subtitles",
    "v_missing_subtitle",
    "v_missing_audio",
    "v_missing_transcript",
    "v_part_pipeline",
}
WORKFLOW_TABLES = {
    "workflow_asr_profiles", "workflow_jobs", "workflow_job_dependencies",
    "workflow_attempts", "workflow_quality_assessments", "workflow_publications",
}
EDITORIAL_TABLES = {
    "editorial_inputs", "editorial_job_inputs", "editorial_model_calls",
    "editorial_chunk_results", "editorial_revisions", "document_artifacts",
    "manuscript_contract", "publication_editions", "publication_edition_reviews",
    "publication_releases", "publication_heads", "publication_events",
}
EXPECTED_TABLE_COLUMNS = {
    "bilibili_users": ["mid", "display_name", "created_at", "updated_at"],
    "video_tags": ["bvid", "tag_id", "tag_name", "tag_type"],
    # Unquoted ``desc``: the DDL quotes the SQL keyword, ``PRAGMA table_info``
    # reports the bare name either way.
    "video_details": ["bvid", "pic", "desc", "tid", "observed_at"],
    "videos": [
        "bvid",
        "aid",
        "mid",
        "title",
        "pubdate",
        "created_at",
        "updated_at",
    ],
    "video_parts": [
        "video_part_id",
        "bvid",
        "page_index",
        "cid",
        "title",
        "duration_ms",
        "processing_status",
        "created_at",
        "updated_at",
    ],
    "ingestion_runs": [
        "run_id",
        "mid",
        "source_package",
        "source_version",
        "requested_start_page",
        "requested_page_limit",
        "started_at",
        "finished_at",
        "outcome",
    ],
    "ingestion_cursors": [
        "mid",
        "next_page",
        "observed_total",
        "state",
        "last_error_code",
        "updated_at",
    ],
    "ingestion_pages": [
        "run_id",
        "page_number",
        "outcome",
        "error_code",
        "started_at",
        "finished_at",
    ],
    "ingestion_discoveries": [
        "run_id",
        "page_number",
        "bvid",
        "source_position",
        "discovered_at",
    ],
    "audio_objects": [
        "audio_id",
        "sha256",
        "byte_size",
        "format",
        "duration_ms",
        "storage_key",
        "created_at",
    ],
    "part_audio_objects": [
        "video_part_id",
        "audio_id",
        "acquired_at",
        "acquisition_source",
    ],
    "asr_models": ["model_id", "model_name", "revision", "created_at"],
    "transcripts": [
        "transcript_id",
        "video_part_id",
        "source_kind",
        "language",
        "model_id",
        "version",
        "content_sha256",
        "created_at",
    ],
    "transcript_segments": ["transcript_id", "ordinal", "start_ms", "end_ms", "text"],
    "acquisition_runs": [
        "run_id",
        "kind",
        "selector_kind",
        "selector_target",
        "requested_limit",
        "credential_present",
        "started_at",
        "finished_at",
        "outcome",
    ],
    "acquisition_attempts": [
        "run_id",
        "video_part_id",
        "outcome",
        "error_code",
        "transcript_id",
        "started_at",
        "finished_at",
        "credential_verified",
        "absence_verified",
    ],
    "transcript_coverage_attestations": [
        "run_id", "video_part_id", "transcript_id", "decoded_s", "produced_s",
        "coverage", "coverage_min", "coverage_short",
    ],
}
EXPECTED_FOREIGN_KEYS = {
    "videos": (("mid", "bilibili_users", "mid"),),
    "video_parts": (("bvid", "videos", "bvid"),),
    # The tag row's parent is the video it tags: a tag cannot outlive, or
    # precede, the video row it belongs to.
    "video_tags": (("bvid", "videos", "bvid"),),
    # Same parent as ``video_tags``: a details row cannot outlive, or precede,
    # the video it describes.
    "video_details": (("bvid", "videos", "bvid"),),
    "ingestion_runs": (("mid", "bilibili_users", "mid"),),
    "ingestion_cursors": (("mid", "bilibili_users", "mid"),),
    "ingestion_pages": (("run_id", "ingestion_runs", "run_id"),),
    "ingestion_discoveries": (
        ("run_id", "ingestion_runs", "run_id"),
        ("bvid", "videos", "bvid"),
    ),
    "part_audio_objects": (
        ("video_part_id", "video_parts", "video_part_id"),
        ("audio_id", "audio_objects", "audio_id"),
    ),
    "transcripts": (
        ("video_part_id", "video_parts", "video_part_id"),
        ("model_id", "asr_models", "model_id"),
    ),
    "transcript_segments": (("transcript_id", "transcripts", "transcript_id"),),
    "acquisition_attempts": (
        ("run_id", "acquisition_runs", "run_id"),
        ("video_part_id", "video_parts", "video_part_id"),
        ("transcript_id", "transcripts", "transcript_id"),
    ),
    "transcript_coverage_attestations": (
        ("run_id", "acquisition_attempts", "run_id"),
        ("video_part_id", "acquisition_attempts", "video_part_id"),
        ("transcript_id", "transcripts", "transcript_id"),
    ),
}
EXPECTED_UNIQUE_CONSTRAINTS = {
    "videos": (("aid",),),
    "video_parts": (("bvid", "page_index"),),
    "audio_objects": (("sha256",), ("storage_key",)),
    "asr_models": (("model_name", "revision"),),
    "transcripts": (("video_part_id", "source_kind", "language", "version"),),
}
EXPECTED_PRIMARY_KEY_INDEXES = {
    "videos": (("bvid",),),
    # ``(bvid, tag_id)`` is the tag identity, and its implicit index is what
    # serves the one query this table has (all tags of one video).  No declared
    # index is added: see ``video_tags``' note below.
    "video_tags": (("bvid", "tag_id"),),
    # One row per video, so the ``bvid`` primary key is the only index this
    # table needs; no declared index is added (see ``video_tags``' note).
    "video_details": (("bvid",),),
    "ingestion_runs": (("run_id",),),
    "ingestion_pages": (("run_id", "page_number"),),
    "ingestion_discoveries": (("run_id", "page_number", "bvid"),),
    "part_audio_objects": (("video_part_id", "audio_id"),),
    "transcript_segments": (("transcript_id", "ordinal"),),
    "acquisition_runs": (("run_id",),),
    "acquisition_attempts": (("run_id", "video_part_id"),),
    "transcript_coverage_attestations": (("run_id", "video_part_id"),),
}
EXPECTED_INDEXES = {
    "videos": (
        (
            "ix_videos_pubdate_bvid",
            ("pubdate", "bvid"),
            False,
            False,
        ),
    ),
    "transcripts": (
        (
            "ux_transcripts_subtitle_content",
            ("video_part_id", "source_kind", "language", "content_sha256"),
            True,
            True,
        ),
    ),
    "acquisition_attempts": (
        (
            "ix_acquisition_attempts_part_time",
            ("video_part_id", "finished_at"),
            False,
            False,
        ),
    ),
    "acquisition_runs": (
        (
            "ix_acquisition_runs_kind_run",
            ("kind", "run_id"),
            False,
            False,
        ),
    ),
    "transcript_coverage_attestations": (
        (
            "ix_transcript_coverage_part_run",
            ("video_part_id", "run_id"),
            False,
            False,
        ),
    ),
}
EXPECTED_PARTIAL_INDEX_CLAUSES = {
    "ux_transcripts_subtitle_content": (
        "WHERE source_kind IN ('subtitle-ai', 'subtitle-cc')"
    ),
}
EXPECTED_CHECK_ENUMERATIONS = {
    "video_parts": (
        "processing_status IN ('discovered', 'metadata_collected', 'gone')",
    ),
    # A tag id is upstream's own positive integer.  Zero and negatives are not
    # tag identities, so they are refused at the storage boundary rather than
    # stored as if upstream had said them.
    "video_tags": ("tag_id > 0",),
    # Task 4's twin of the row above: a category id is upstream's own positive
    # integer, and NULL is the legitimate "not observed" state, so the CHECK
    # admits NULL and refuses zero/negatives.  Declared here because the loop
    # below iterates *declared* entries only -- an undeclared table's CHECK is
    # silently unpinned (measured: deleting it left this file 39 passed).
    "video_details": ("tid IS NULL OR tid > 0",),
    "ingestion_runs": (
        "source_package = 'bilibili-api-python'",
        "outcome IN ('running', 'complete', 'limited', 'risk_interrupted', 'failed')",
    ),
    "ingestion_cursors": (
        "state IN ('ready', 'complete', 'limited', 'risk_interrupted')",
    ),
    "ingestion_pages": ("outcome IN ('ok', 'empty', 'risk_interrupted', 'failed')",),
    "transcripts": (
        "source_kind IN ('subtitle-ai', 'subtitle-cc', 'asr-local')",
        "length(trim(language)) > 0",
        "length(content_sha256) = 64 AND content_sha256 = lower(content_sha256)",
        "UNIQUE (video_part_id, source_kind, language, version)",
    ),
    "transcript_segments": (
        "ordinal >= 0",
        "start_ms >= 0",
        "end_ms > start_ms",
    ),
    "acquisition_runs": (
        "kind IN ('subtitle', 'audio', 'asr')",
        "selector_kind IN ('pending', 'bvid')",
        "requested_limit IS NULL OR requested_limit > 0",
        "credential_present IN (0, 1)",
        "outcome IN ('running', 'complete', 'partial', 'failed')",
        "(selector_kind = 'pending' AND selector_target IS NULL) "
        "OR (selector_kind = 'bvid' AND selector_target IS NOT NULL)",
        "finished_at IS NULL OR finished_at >= started_at",
    ),
    "acquisition_attempts": (
        "outcome IN ('stored', 'unchanged', 'no-subtitle', 'failed')",
        "error_code IS NULL OR length(error_code) <= 64",
        "PRIMARY KEY (run_id, video_part_id)",
        "outcome = 'failed' AND error_code IS NOT NULL AND transcript_id IS NULL",
        "(error_code IS NULL OR error_code = 'not_found') AND transcript_id IS NULL",
        "outcome IN ('stored', 'unchanged') AND error_code IS NULL "
        "AND transcript_id IS NOT NULL",
        "CHECK (finished_at >= started_at)",
        "credential_verified IN (0, 1)",
        "absence_verified IN (0, 1)",
    ),
}
EXPECTED_VIEW_WORK_ID_EXPRESSION = "vp.bvid || ':p' || vp.page_index AS work_id"
EXPECTED_VIEW_COLUMNS = {
    "v_pending_subtitles": [
        "video_part_id",
        "work_id",
        "bvid",
        "page_index",
        "cid",
        "part_title",
        "duration_ms",
        "attempted",
        "last_attempt_at",
        "last_attempt_outcome",
        "last_attempt_error_code",
        "last_attempt_credential_present",
    ],
    "v_missing_subtitle": [
        "video_part_id",
        "work_id",
        "bvid",
        "page_index",
        "cid",
        "part_title",
        "duration_ms",
        "video_title",
        "pubdate",
    ],
    "v_missing_audio": [
        "video_part_id",
        "work_id",
        "bvid",
        "page_index",
        "cid",
        "part_title",
        "duration_ms",
        "video_title",
        "pubdate",
        "newest_outcome",
        "newest_error_code",
    ],
    "v_missing_transcript": [
        "video_part_id",
        "work_id",
        "bvid",
        "page_index",
        "cid",
        "part_title",
        "duration_ms",
        "video_title",
        "pubdate",
    ],
    "v_part_pipeline": [
        "video_part_id",
        "work_id",
        "bvid",
        "page_index",
        "processing_status",
        "pipeline_state",
    ],
}
EXPECTED_ENUM_COLUMNS = {
    ("video_parts", "processing_status"): ALLOWED_PROCESSING_STATUS,
    ("ingestion_runs", "outcome"): ALLOWED_RUN_OUTCOMES,
    ("ingestion_cursors", "state"): ALLOWED_CURSOR_STATES,
    ("ingestion_pages", "outcome"): ALLOWED_PAGE_OUTCOMES,
    ("transcripts", "source_kind"): ALLOWED_SOURCE_KINDS,
    ("acquisition_runs", "kind"): ALLOWED_ACQUISITION_KINDS,
    ("acquisition_runs", "outcome"): ALLOWED_ACQUISITION_OUTCOMES,
    ("acquisition_attempts", "outcome"): ALLOWED_ATTEMPT_OUTCOMES,
}
EXPECTED_LITERAL_SETS = (
    (ProcessingStatus, ALLOWED_PROCESSING_STATUS),
    (RunOutcome, ALLOWED_RUN_OUTCOMES),
    (PageOutcome, ALLOWED_PAGE_OUTCOMES),
    (CursorState, ALLOWED_CURSOR_STATES),
    (SourceKind, ALLOWED_SOURCE_KINDS),
    (AcquisitionKind, ALLOWED_ACQUISITION_KINDS),
    (AcquisitionOutcome, ALLOWED_ACQUISITION_OUTCOMES),
    (AttemptOutcome, ALLOWED_ATTEMPT_OUTCOMES),
)
# The pre-iteration shape of the transcript block, exactly as iteration
# `20260909-structured-metadata-schema` shipped it. It is what the bootstrap
# must leave untouched on an existing database (there is no migration path).
LEGAL_ATTEMPTS = (
    ("stored", None, 1),
    ("unchanged", None, 1),
    ("no-subtitle", None, None),
    ("no-subtitle", "not_found", None),
    ("failed", "timeout", None),
)
ILLEGAL_ATTEMPTS = (
    ("stored", None, None),
    ("stored", "timeout", 1),
    ("unchanged", None, None),
    ("failed", None, None),
    ("failed", "timeout", 1),
    ("no-subtitle", "timeout", None),
    ("no-subtitle", None, 1),
    ("unknown", None, None),
)


def test_schema_sql_is_declared_and_read_as_package_resource():
    project_root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads(
        (project_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    package_data = pyproject["tool"]["setuptools"]["package-data"]
    assert {"schema.sql", "schema-transcripts.sql", "schema-workflow.sql", "schema-editorial.sql"} <= set(
        package_data["bili_asr.storage"]
    )

    resources_root = resources.files("bili_asr.storage")
    schema_text = resources_root.joinpath("schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS videos" in schema_text

    transcript_resource = resources_root.joinpath("schema-transcripts.sql")
    assert transcript_resource.is_file()
    transcript_text = transcript_resource.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS transcripts (" in transcript_text
    # The bootstrap split is what keeps a pre-iteration database usable: the
    # metadata script no longer declares the transcript block.
    assert "CREATE TABLE IF NOT EXISTS transcripts (" not in schema_text
    assert "CREATE TABLE IF NOT EXISTS transcript_segments" not in schema_text


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    )
    return {row[0] for row in rows}






def _insert_transcript(
    connection: sqlite3.Connection,
    *,
    transcript_id: int,
    source_kind: str,
    language: str,
    version: int,
    content_sha256: str,
    video_part_id: int = 1,
    model_id: int | None = None,
    created_at: int = 200,
) -> None:
    connection.execute(
        """
        INSERT INTO transcripts(
            transcript_id, video_part_id, source_kind, language, model_id,
            version, content_sha256, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            transcript_id,
            video_part_id,
            source_kind,
            language,
            model_id,
            version,
            content_sha256,
            created_at,
        ),
    )


def _insert_subtitle_acquisition_run(
    connection: sqlite3.Connection,
    *,
    run_id: str = "run-subs",
    credential_present: int = 0,
    finished_at: int | None = None,
    outcome: str = "running",
) -> None:
    connection.execute(
        """
        INSERT INTO acquisition_runs(
            run_id, kind, selector_kind, selector_target, requested_limit,
            credential_present, started_at, finished_at, outcome
        ) VALUES (?, 'subtitle', 'pending', NULL, NULL, ?, 100, ?, ?)
        """,
        (run_id, credential_present, finished_at, outcome),
    )


def _insert_attempt(
    connection: sqlite3.Connection,
    *,
    outcome: str,
    error_code: str | None,
    transcript_id: int | None,
    run_id: str = "run-subs",
    video_part_id: int = 1,
    started_at: int = 100,
    finished_at: int = 200,
    credential_verified: bool = False,
    absence_verified: bool = False,
) -> None:
    connection.execute(
        """
        INSERT INTO acquisition_attempts(
            run_id, video_part_id, outcome, error_code, transcript_id,
            started_at, finished_at, credential_verified, absence_verified
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            video_part_id,
            outcome,
            error_code,
            transcript_id,
            started_at,
            finished_at,
            int(credential_verified),
            int(absence_verified),
        ),
    )


def _insert_attempt_parents(connection: sqlite3.Connection) -> None:
    """Insert the parents one attempt row needs: a part, a run, a transcript."""
    part_id = _insert_user_video_part(connection)
    _insert_subtitle_acquisition_run(connection)
    _insert_transcript(
        connection,
        transcript_id=1,
        video_part_id=part_id,
        source_kind="subtitle-ai",
        language="zh-CN",
        version=1,
        content_sha256="a" * 64,
    )




def test_fresh_database_initializes_archive_root_and_is_idempotent(tmp_root):
    connection = open_database(tmp_root)
    try:
        assert os.path.isfile(os.path.join(tmp_root, "archive.db"))
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.isolation_level == "DEFERRED"
        assert BASE_TABLES | VIEWS <= _table_names(connection)

        _insert_user_video_part(connection)
        connection.commit()
    finally:
        connection.close()

    reopened = open_database(os.path.join(tmp_root, "archive.db"))
    try:
        assert reopened.execute("SELECT COUNT(*) FROM video_parts").fetchone()[0] == 1
        assert reopened.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        reopened.close()


def test_open_database_memory_database_is_initialized():
    connection = open_database(":memory:")
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert BASE_TABLES | VIEWS <= _table_names(connection)
        _insert_user_video_part(connection)
        connection.commit()
        assert (
            connection.execute("SELECT COUNT(*) FROM video_parts").fetchone()[0] == 1
        )
    finally:
        connection.close()


def test_schema_check_enumerations_match_model_validation_sets():
    """The DDL CHECK literals and the model validation sets are one contract."""
    connection = open_database(":memory:")
    try:
        for (table, column), allowed in EXPECTED_ENUM_COLUMNS.items():
            ddl_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            match = re.search(rf"\b{column}\s+IN\s*\(([^)]*)\)", ddl_row[0])
            assert match is not None
            literals = re.findall(r"'([^']*)'", match.group(1))
            assert sorted(literals) == sorted(allowed)

        literal_sets = EXPECTED_LITERAL_SETS
        for literal, allowed in literal_sets:
            assert sorted(get_args(literal)) == sorted(allowed)
    finally:
        connection.close()


def test_page_and_duration_normalization_uses_contract_formulas():
    assert duration_to_ms(12.3459) == 12_345
    assert duration_to_ms(0) == 0
    assert normalize_page_index(1) == 0
    assert normalize_page_index(3) == 2
    with pytest.raises(ValueError):
        normalize_page_index(0)
    with pytest.raises(ValueError):
        duration_to_ms(-0.1)


def test_foreign_keys_reject_orphans_and_use_restrict(tmp_root):
    connection = open_database(tmp_root)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO videos(bvid, mid, title, pubdate, created_at, updated_at) "
                "VALUES ('BVORPHAN', 999, 'orphan', 1, 1, 1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO video_parts(
                    bvid, page_index, cid, title, duration_ms,
                    processing_status, created_at, updated_at
                ) VALUES ('BVORPHAN', 0, 1, 'orphan', 1, 'discovered', 1, 1)
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO ingestion_pages VALUES ('ghost-run', 1, 'ok', NULL, 1, 1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO ingestion_discoveries "
                "VALUES ('ghost-run', 1, 'BVORPHAN', 0, 1)"
            )

        _insert_user_video_part(connection)
        connection.execute(
            "INSERT INTO ingestion_runs VALUES "
            "('run-1', 23191782, 'bilibili-api-python', '1.0', 1, NULL, 1, NULL, 'running')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO ingestion_discoveries "
                "VALUES ('run-1', 1, 'BVUNKNOWN', 0, 1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM bilibili_users WHERE mid = 23191782")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM videos WHERE bvid = 'BV1TEST'")

        for table in BASE_TABLES:
            for row in connection.execute(f"PRAGMA foreign_key_list({table})"):
                assert row[6].upper() == "RESTRICT"
    finally:
        connection.close()


def test_duplicate_candidate_keys_are_rejected(tmp_root):
    connection = open_database(tmp_root)
    try:
        _insert_user_video_part(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO bilibili_users VALUES (23191782, 'same', 1, 1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO videos VALUES ('BV1TEST', 1002, 23191782, 'same', 1, 1, 1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO videos VALUES ('BV2TEST', 1001, 23191782, 'same', 1, 1, 1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO video_parts(
                    bvid, page_index, cid, title, duration_ms,
                    processing_status, created_at, updated_at
                ) VALUES ('BV1TEST', 0, 2, 'duplicate', 10, 'gone', 1, 1)
                """
            )

        connection.execute(
            """
            INSERT INTO audio_objects(
                audio_id, sha256, byte_size, format, duration_ms, storage_key, created_at
            ) VALUES (1, 'hash-1', 1, 'm4a', 1, 'audio/1', 1)
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO audio_objects(
                    audio_id, sha256, byte_size, format, duration_ms, storage_key, created_at
                ) VALUES (2, 'hash-1', 1, 'm4a', 1, 'audio/2', 1)
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO audio_objects(
                    audio_id, sha256, byte_size, format, duration_ms, storage_key, created_at
                ) VALUES (2, 'hash-2', 1, 'm4a', 1, 'audio/1', 1)
                """
            )
    finally:
        connection.close()


def test_video_tags_are_keyed_by_video_and_tag_id(tmp_root):
    """``(bvid, tag_id)`` is the identity, and nothing else is unique.

    ``tag_name`` is a display label upstream may rename, so it is deliberately
    *not* part of the key: the same name may label two ids, and one id may be
    renamed while its row stays the same row.
    """

    connection = open_database(tmp_root)
    try:
        _insert_user_video_part(connection)
        connection.execute(
            "INSERT INTO video_tags(bvid, tag_id, tag_name, tag_type)"
            " VALUES ('BV1TEST', 943, '爱情', 'old_channel')"
        )
        # A second video may carry the very same tag id.
        connection.execute(
            "INSERT INTO videos(bvid, aid, mid, title, pubdate, created_at, updated_at)"
            " VALUES ('BV2TEST', 1002, 23191782, '第二个视频', 1, 101, 101)"
        )
        connection.execute(
            "INSERT INTO video_tags(bvid, tag_id, tag_name, tag_type)"
            " VALUES ('BV2TEST', 943, '爱情', 'old_channel')"
        )
        # The same id twice on one video is a duplicate identity.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO video_tags(bvid, tag_id, tag_name, tag_type)"
                " VALUES ('BV1TEST', 943, '重复', 'old_channel')"
            )
        # A renamed label is the same identity, so it is refused too — the
        # store cannot hold two labels for one tag id.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO video_tags(bvid, tag_id, tag_name, tag_type)"
                " VALUES ('BV1TEST', 943, 'renamed upstream', 'old_channel')"
            )
        # A non-positive tag id is not an identity upstream can issue.
        for invalid in (0, -1):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO video_tags(bvid, tag_id, tag_name, tag_type)"
                    " VALUES ('BV1TEST', ?, 'x', 'old_channel')",
                    (invalid,),
                )
        # A tag cannot precede or outlive the video row it belongs to.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO video_tags(bvid, tag_id, tag_name, tag_type)"
                " VALUES ('BVUNKNOWN', 1, 'x', 'old_channel')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM videos WHERE bvid = 'BV1TEST'")

        rows = connection.execute(
            "SELECT bvid, tag_id, tag_name, tag_type FROM video_tags"
            " ORDER BY bvid, tag_id"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("BV1TEST", 943, "爱情", "old_channel"),
            ("BV2TEST", 943, "爱情", "old_channel"),
        ]
    finally:
        connection.close()


def test_video_tags_declare_no_index_of_their_own(tmp_root):
    """The composite primary key is the only index this table needs.

    The task's one declared query is "every tag of one video", which the
    implicit primary-key index serves directly (measured: ``SEARCH video_tags
    USING INDEX sqlite_autoindex_video_tags_1 (bvid=?)``).  A declared ``CREATE
    INDEX`` would therefore be additive cost with no query benefit, and the
    declared-index contract is pinned per table — so the absence is asserted
    here rather than left to the contract's own silence.
    """

    connection = open_database(tmp_root)
    try:
        declared = [
            row["name"]
            for row in connection.execute("PRAGMA index_list(video_tags)")
            if row["origin"] == "c"
        ]
        assert declared == []

        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT tag_id, tag_name FROM video_tags"
            " WHERE bvid = ?",
            ("BV1TEST",),
        ).fetchall()
        detail = " ".join(str(row["detail"]) for row in plan)
        assert "sqlite_autoindex_video_tags_1" in detail
        assert "SCAN" not in detail
    finally:
        connection.close()


def test_views_compute_work_id_and_keep_derived_values_out_of_base_tables(tmp_root):
    connection = open_database(tmp_root)
    try:
        part_id = _insert_user_video_part(connection)
        row = connection.execute(
            "SELECT * FROM v_video_parts WHERE video_part_id = ?", (part_id,)
        ).fetchone()
        assert row["work_id"] == "BV1TEST:p0"
        assert row["user_name"] == "未明子"
        assert row["video_title"] == "视频"

        for table in BASE_TABLES:
            columns = {
                item[1] for item in connection.execute(f"PRAGMA table_info({table})")
            }
            assert "work_id" not in columns
            assert "part_count" not in columns
            assert "video_count" not in columns
            assert "run_count" not in columns

        pending = connection.execute("SELECT work_id FROM v_pending_metadata").fetchall()
        assert [item[0] for item in pending] == ["BV1TEST:p0"]
    finally:
        connection.close()


def test_schema_constraints_cover_status_and_non_negative_values(tmp_root):
    connection = open_database(tmp_root)
    try:
        _insert_user_video_part(connection)
        invalid_statements = [
            "UPDATE video_parts SET page_index = -1",
            "UPDATE video_parts SET cid = 0",
            "UPDATE video_parts SET duration_ms = 0",
            "UPDATE video_parts SET processing_status = 'pending'",
            "INSERT INTO ingestion_runs VALUES ('run', 23191782, 'other', '1', 1, NULL, 1, NULL, 'running')",
            "INSERT INTO ingestion_runs VALUES ('run', 23191782, 'bilibili-api-python', '1', 0, NULL, 1, NULL, 'running')",
            "INSERT INTO ingestion_cursors VALUES (23191782, 0, NULL, 'ready', NULL, 1)",
            "INSERT INTO ingestion_pages VALUES ('run', 0, 'ok', NULL, 1, 1)",
            "INSERT INTO ingestion_pages VALUES ('run', 1, 'unknown', NULL, 1, 1)",
            "INSERT INTO audio_objects VALUES (1, 'hash', -1, 'm4a', 1, 'audio', 1)",
            f"INSERT INTO ingestion_cursors VALUES "
            f"(23191782, 2, NULL, 'ready', '{'y' * 65}', 1)",
            f"INSERT INTO ingestion_pages VALUES ('run', 3, 'failed', '{'x' * 65}', 1, 1)",
            f"INSERT INTO transcripts VALUES "
            f"(1, 1, 'subtitle-ai', '   ', NULL, 1, '{'a' * 64}', 1)",
            f"INSERT INTO transcripts VALUES "
            f"(1, 1, 'subtitle-ai', 'zh-CN', NULL, 1, '{'A' * 64}', 1)",
            f"INSERT INTO transcripts VALUES "
            f"(1, 1, 'subtitle-ai', 'zh-CN', NULL, 1, '{'a' * 63}', 1)",
            f"INSERT INTO transcripts VALUES "
            f"(1, 1, 'subtitle-ai', 'zh-CN', NULL, 0, '{'a' * 64}', 1)",
            f"INSERT INTO transcripts VALUES "
            f"(1, 1, 'asr-remote', 'zh-CN', NULL, 1, '{'a' * 64}', 1)",
            "INSERT INTO acquisition_runs VALUES "
            "('run', 'video', 'pending', NULL, NULL, 0, 1, NULL, 'running')",
            "INSERT INTO acquisition_runs VALUES "
            "('run', 'subtitle', 'bvid', NULL, NULL, 0, 1, NULL, 'running')",
            "INSERT INTO acquisition_runs VALUES "
            "('run', 'subtitle', 'pending', 'BV1TEST', NULL, 0, 1, NULL, 'running')",
            "INSERT INTO acquisition_runs VALUES "
            "('run', 'subtitle', 'pending', NULL, 0, 0, 1, NULL, 'running')",
            "INSERT INTO acquisition_runs VALUES "
            "('run', 'subtitle', 'pending', NULL, NULL, 2, 1, NULL, 'running')",
            "INSERT INTO acquisition_runs VALUES "
            "('run', 'subtitle', 'pending', NULL, NULL, 0, 1, NULL, 'limited')",
            "INSERT INTO acquisition_runs VALUES "
            "('run', 'subtitle', 'pending', NULL, NULL, 0, 100, 50, 'complete')",
        ]
        for statement in invalid_statements:
            with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError)):
                connection.execute(statement)
    finally:
        connection.close()


def test_transaction_order_parents_before_children(tmp_root):
    connection = open_database(tmp_root)
    try:
        with connection:
            connection.execute(
                "INSERT INTO bilibili_users VALUES (7, 'operator', 1, 1)"
            )
            connection.execute(
                """
                INSERT INTO ingestion_runs VALUES (
                    'run-1', 7, 'bilibili-api-python', '1.0', 1, 2,
                    1, NULL, 'running'
                )
                """
            )
            connection.execute(
                "INSERT INTO videos VALUES ('BV7', 7, 7, 'title', 1, 1, 1)"
            )
            connection.execute(
                """
                INSERT INTO video_parts(
                    bvid, page_index, cid, title, duration_ms,
                    processing_status, created_at, updated_at
                ) VALUES ('BV7', 0, 70, 'part', 1000, 'discovered', 1, 1)
                """
            )
            connection.execute(
                "INSERT INTO ingestion_discoveries VALUES ('run-1', 1, 'BV7', 0, 1)"
            )
            connection.execute(
                "INSERT INTO ingestion_cursors VALUES (7, 2, 1, 'ready', NULL, 1)"
            )
            connection.execute(
                "INSERT INTO ingestion_pages VALUES ('run-1', 1, 'ok', NULL, 1, 1)"
            )

        stats = connection.execute(
            "SELECT page_count, video_count FROM v_ingestion_run_stats "
            "WHERE run_id = 'run-1'"
        ).fetchone()
        assert tuple(stats) == (1, 1)
    finally:
        connection.close()


def test_schema_inspection_matches_the_declared_contract(tmp_root):
    connection = open_database(tmp_root)
    try:
        assert _table_names(connection) == BASE_TABLES | WORKFLOW_TABLES | EDITORIAL_TABLES | VIEWS | {
            "workflow_asr_profile_configs", "transcript_asr_evidence", "video_tag_observations",
        }

        for table in BASE_TABLES:
            columns = [
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            ]
            assert columns == EXPECTED_TABLE_COLUMNS[table]
            assert "work_id" not in columns

            foreign_keys = {
                (row["from"], row["table"], row["to"])
                for row in connection.execute(f"PRAGMA foreign_key_list({table})")
            }
            assert foreign_keys == set(EXPECTED_FOREIGN_KEYS.get(table, ()))
            for row in connection.execute(f"PRAGMA foreign_key_list({table})"):
                assert row["on_delete"] == "RESTRICT"

            unique_constraints = set()
            primary_key_indexes = set()
            for index in connection.execute(f"PRAGMA index_list({table})"):
                indexed_columns = tuple(
                    info["name"]
                    for info in connection.execute(f"PRAGMA index_info({index['name']})")
                )
                if index["origin"] == "u":
                    unique_constraints.add(indexed_columns)
                elif index["origin"] == "pk":
                    primary_key_indexes.add(indexed_columns)
            assert unique_constraints == set(
                EXPECTED_UNIQUE_CONSTRAINTS.get(table, ())
            )
            assert primary_key_indexes == set(
                EXPECTED_PRIMARY_KEY_INDEXES.get(table, ())
            )

            declared_indexes = {
                index["name"]: index
                for index in connection.execute(f"PRAGMA index_list({table})")
                if index["origin"] == "c"
            }
            expected_indexes = EXPECTED_INDEXES.get(table, ())
            assert set(declared_indexes) == {
                name for name, _, _, _ in expected_indexes
            }
            for name, columns, is_unique, is_partial in expected_indexes:
                assert bool(declared_indexes[name]["unique"]) is is_unique
                assert bool(declared_indexes[name]["partial"]) is is_partial
                assert (
                    tuple(
                        info["name"]
                        for info in connection.execute(f"PRAGMA index_info({name})")
                    )
                    == columns
                )
                clause = EXPECTED_PARTIAL_INDEX_CLAUSES.get(name)
                if clause is not None:
                    # A partial index is only content identity if its WHERE
                    # clause scopes it to the caption kinds.
                    assert _stored_ddl(connection, name).endswith(clause)

        for table, fragments in EXPECTED_CHECK_ENUMERATIONS.items():
            ddl_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            normalized_ddl = " ".join(ddl_row[0].split())
            for fragment in fragments:
                assert fragment in normalized_ddl

        for view in VIEWS:
            ddl_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'view' AND name = ?",
                (view,),
            ).fetchone()
            normalized_ddl = " ".join(ddl_row[0].split())
            if view in {
                "v_video_parts",
                "v_pending_metadata",
                "v_pending_subtitles",
                "v_missing_subtitle",
                "v_missing_audio",
                "v_missing_transcript",
                "v_part_pipeline",
            }:
                assert EXPECTED_VIEW_WORK_ID_EXPRESSION in normalized_ddl

        for view, columns in EXPECTED_VIEW_COLUMNS.items():
            assert [
                row["name"] for row in connection.execute(f"PRAGMA table_info({view})")
            ] == columns
    finally:
        connection.close()


def test_views_compute_derived_values_across_users_videos_and_runs(tmp_root):
    connection = open_database(tmp_root)
    try:
        connection.executescript(
            """
            INSERT INTO bilibili_users VALUES (23191782, '未明子', 1, 1);
            INSERT INTO bilibili_users VALUES (42, '第二位用户', 1, 1);
            INSERT INTO videos VALUES
                ('BV1SINGLE', 1001, 23191782, '单集视频', 1700000000, 1, 1);
            INSERT INTO videos VALUES
                ('BV1MULTI', 1002, 42, '多集视频', 1700000001, 1, 1);
            INSERT INTO videos VALUES
                ('BV1GONE', NULL, 42, '已下架视频', 1700000002, 1, 1);
            INSERT INTO video_parts(
                bvid, page_index, cid, title, duration_ms, processing_status,
                created_at, updated_at
            ) VALUES ('BV1SINGLE', 0, 2001, '第一集', 1000, 'discovered', 1, 1);
            INSERT INTO video_parts(
                bvid, page_index, cid, title, duration_ms, processing_status,
                created_at, updated_at
            ) VALUES ('BV1MULTI', 0, 3001, '上篇', 2000, 'discovered', 1, 1);
            INSERT INTO video_parts(
                bvid, page_index, cid, title, duration_ms, processing_status,
                created_at, updated_at
            ) VALUES ('BV1MULTI', 1, 3002, '下篇', 3000, 'metadata_collected', 1, 1);
            INSERT INTO video_parts(
                bvid, page_index, cid, title, duration_ms, processing_status,
                created_at, updated_at
            ) VALUES ('BV1GONE', 0, 4001, '残片', 4000, 'gone', 1, 1);
            INSERT INTO ingestion_runs VALUES
                ('run-1', 23191782, 'bilibili-api-python', '1.0', 1, NULL, 1, NULL, 'running');
            INSERT INTO ingestion_runs VALUES
                ('run-2', 42, 'bilibili-api-python', '1.0', 1, NULL, 1, NULL, 'running');
            INSERT INTO ingestion_pages VALUES ('run-1', 1, 'ok', NULL, 1, 2);
            INSERT INTO ingestion_pages VALUES ('run-1', 2, 'empty', NULL, 3, 4);
            INSERT INTO ingestion_discoveries VALUES ('run-1', 1, 'BV1SINGLE', 0, 5);
            INSERT INTO ingestion_discoveries VALUES ('run-1', 2, 'BV1SINGLE', 0, 6);
            INSERT INTO ingestion_discoveries VALUES ('run-1', 2, 'BV1MULTI', 1, 6);
            """
        )

        part_rows = connection.execute(
            "SELECT work_id, user_name, video_title, page_index, cid, part_title, "
            "processing_status FROM v_video_parts ORDER BY work_id"
        ).fetchall()
        assert [row["work_id"] for row in part_rows] == [
            "BV1GONE:p0",
            "BV1MULTI:p0",
            "BV1MULTI:p1",
            "BV1SINGLE:p0",
        ]
        single = part_rows[3]
        assert single["user_name"] == "未明子"
        assert single["video_title"] == "单集视频"
        multi = part_rows[2]
        assert multi["user_name"] == "第二位用户"
        assert multi["part_title"] == "下篇"
        assert multi["processing_status"] == "metadata_collected"

        stats = {
            row["run_id"]: (row["page_count"], row["video_count"])
            for row in connection.execute("SELECT * FROM v_ingestion_run_stats")
        }
        # run-1 has three discovery rows but only two distinct videos; run-2
        # has neither pages nor discoveries and still reports zero counts.
        assert stats == {"run-1": (2, 2), "run-2": (0, 0)}

        pending = [
            row["work_id"]
            for row in connection.execute(
                "SELECT work_id FROM v_pending_metadata ORDER BY work_id"
            )
        ]
        assert pending == ["BV1MULTI:p0", "BV1SINGLE:p0"]
    finally:
        connection.close()


def test_bootstrap_creates_the_full_contract_on_a_fresh_database(tmp_root):
    connection = open_database(tmp_root)
    try:
        assert require_subtitle_schema(connection) is None
        assert BASE_TABLES | VIEWS <= _table_names(connection)
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()


def test_bootstrap_reapplies_the_contract_to_a_current_database(tmp_root):
    database_path = os.path.join(tmp_root, "archive.db")
    connection = open_database(database_path)
    try:
        part_id = _insert_user_video_part(connection)
        _insert_transcript(
            connection,
            transcript_id=1,
            video_part_id=part_id,
            source_kind="subtitle-ai",
            language="zh-CN",
            version=1,
            content_sha256="a" * 64,
        )
        connection.execute(
            "INSERT INTO transcript_segments("
            "transcript_id, ordinal, start_ms, end_ms, text"
            ") VALUES (1, 0, 0, 1200, '第一句')"
        )
        connection.commit()
        declared_ddl = {
            name: _stored_ddl(connection, name)
            for name in ("transcripts", "transcript_segments", "v_pending_subtitles")
        }
    finally:
        connection.close()

    reopened = open_database(database_path)
    try:
        assert require_subtitle_schema(reopened) is None
        # Re-applying the script is a no-op for already-current objects: tables
        # and the untouched views are IF NOT EXISTS, and the shipped views are
        # compared against the stored bodies by ``refresh_shipped_views``, which
        # rewrites only a view whose body differs (a current one is not touched,
        # so nothing here moves).
        assert {
            name: _stored_ddl(reopened, name) for name in declared_ddl
        } == declared_ddl
        assert (
            reopened.execute("SELECT text FROM transcript_segments").fetchone()[0]
            == "第一句"
        )
        # A capability check is structural: a missing object is a broken
        # contract even while the columns look current.
        reopened.execute("DROP VIEW v_pending_subtitles")
        with pytest.raises(SchemaContractError):
            require_subtitle_schema(reopened)
    finally:
        reopened.close()

    repaired = open_database(database_path)
    try:
        assert require_subtitle_schema(repaired) is None
    finally:
        repaired.close()


def test_bootstrap_refuses_a_pre_iteration_database_without_modifying_it(tmp_root):
    database_path = Path(tmp_root) / "archive.db"
    _write_pre_iteration_database(database_path)
    before = database_path.read_bytes()
    with pytest.raises(SchemaContractError, match="preserve archive.db") as refused:
        open_database(database_path)
    assert "separate empty target" in str(refused.value)
    assert database_path.read_bytes() == before

def test_require_subtitle_schema_requires_the_process_record_objects(tmp_root):
    connection = open_database(tmp_root)
    try:
        connection.execute("DROP TABLE acquisition_attempts")
        with pytest.raises(SchemaContractError):
            require_subtitle_schema(connection)
    finally:
        connection.close()


def test_subtitle_content_index_is_partial_over_the_caption_kinds(tmp_root):
    connection = open_database(tmp_root)
    try:
        part_id = _insert_user_video_part(connection)
        digest = "a" * 64
        _insert_transcript(
            connection,
            transcript_id=1,
            video_part_id=part_id,
            source_kind="subtitle-ai",
            language="zh-CN",
            version=1,
            content_sha256=digest,
        )
        # Content identity: the same caption never lands twice for one part,
        # source kind and language.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_transcript(
                connection,
                transcript_id=2,
                video_part_id=part_id,
                source_kind="subtitle-ai",
                language="zh-CN",
                version=2,
                content_sha256=digest,
            )
        # A different language or source kind is a different fact.
        _insert_transcript(
            connection,
            transcript_id=3,
            video_part_id=part_id,
            source_kind="subtitle-cc",
            language="en-US",
            version=1,
            content_sha256=digest,
        )
        # asr-local keeps its own model-scoped identity rule: outside the index,
        # so a repeated hash appends a version instead of colliding.
        _insert_transcript(
            connection,
            transcript_id=4,
            video_part_id=part_id,
            source_kind="asr-local",
            language="zh-CN",
            version=1,
            content_sha256=digest,
        )
        _insert_transcript(
            connection,
            transcript_id=5,
            video_part_id=part_id,
            source_kind="asr-local",
            language="zh-CN",
            version=2,
            content_sha256=digest,
        )
        # The widened version key still rejects a repeated version.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_transcript(
                connection,
                transcript_id=6,
                video_part_id=part_id,
                source_kind="subtitle-ai",
                language="zh-CN",
                version=1,
                content_sha256="b" * 64,
            )
    finally:
        connection.close()


def test_transcript_base_tables_store_no_derived_values(tmp_root):
    connection = open_database(tmp_root)
    try:
        view_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(v_pending_subtitles)")
        }
        assert {"work_id", "attempted", "last_attempt_at"} <= view_columns

        forbidden = {
            "work_id",
            "attempted",
            "attempt_count",
            "segment_count",
            "transcript_count",
            "is_pending",
            "last_attempt_at",
            "last_attempt_outcome",
            "last_attempt_error_code",
            "last_attempt_credential_present",
        }
        for table in (
            "transcripts",
            "transcript_segments",
            "acquisition_runs",
            "acquisition_attempts",
        ):
            columns = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            assert columns.isdisjoint(forbidden), table
    finally:
        connection.close()


def test_pending_subtitles_view_carries_the_newest_attempt_evidence(tmp_root):
    connection = open_database(tmp_root)
    try:
        _insert_user_video_part(connection)
        connection.execute(
            "INSERT INTO videos(bvid, aid, mid, title, pubdate, created_at, updated_at) "
            "VALUES ('BV1GONE', NULL, 23191782, '已下架', 1, 1, 1)"
        )
        connection.execute(
            """
            INSERT INTO video_parts(
                bvid, page_index, cid, title, duration_ms, processing_status,
                created_at, updated_at
            ) VALUES ('BV1GONE', 0, 2001, '残片', 1000, 'gone', 1, 1)
            """
        )
        connection.execute(
            """
            INSERT INTO video_parts(
                bvid, page_index, cid, title, duration_ms, processing_status,
                created_at, updated_at
            ) VALUES ('BV1TEST', 1, 2002, '第二段', 2000, 'discovered', 1, 1)
            """
        )
        transcribed = int(
            connection.execute(
                """
                INSERT INTO video_parts(
                    bvid, page_index, cid, title, duration_ms, processing_status,
                    created_at, updated_at
                ) VALUES ('BV1TEST', 2, 2003, '第三段', 3000, 'discovered', 1, 1)
                """
            ).lastrowid
        )
        attempted = 3
        _insert_subtitle_acquisition_run(
            connection,
            run_id="run-1",
            credential_present=1,
            finished_at=300,
            outcome="complete",
        )
        _insert_subtitle_acquisition_run(
            connection,
            run_id="run-2",
            credential_present=0,
            finished_at=500,
            outcome="partial",
        )
        _insert_attempt(
            connection,
            run_id="run-1",
            video_part_id=attempted,
            outcome="no-subtitle",
            error_code="not_found",
            transcript_id=None,
            started_at=200,
            finished_at=300,
        )
        _insert_attempt(
            connection,
            run_id="run-2",
            video_part_id=attempted,
            outcome="failed",
            error_code="timeout",
            transcript_id=None,
            started_at=400,
            finished_at=500,
        )
        _insert_transcript(
            connection,
            transcript_id=1,
            video_part_id=transcribed,
            source_kind="subtitle-cc",
            language="zh-CN",
            version=1,
            content_sha256="c" * 64,
        )

        rows = connection.execute(
            "SELECT work_id, cid, attempted, last_attempt_at, last_attempt_outcome, "
            "last_attempt_error_code, last_attempt_credential_present "
            "FROM v_pending_subtitles ORDER BY work_id"
        ).fetchall()
        assert [row["work_id"] for row in rows] == ["BV1TEST:p0", "BV1TEST:p1"]

        fresh = rows[0]
        assert fresh["cid"] == 2001
        assert fresh["attempted"] == 0
        assert fresh["last_attempt_at"] is None
        assert fresh["last_attempt_outcome"] is None
        assert fresh["last_attempt_error_code"] is None
        assert fresh["last_attempt_credential_present"] is None

        retried = rows[1]
        assert retried["cid"] == 2002
        assert retried["attempted"] == 1
        assert retried["last_attempt_at"] == 500
        assert retried["last_attempt_outcome"] == "failed"
        assert retried["last_attempt_error_code"] == "timeout"
        assert retried["last_attempt_credential_present"] == 0
    finally:
        connection.close()


def test_pending_subtitles_view_is_scoped_to_subtitle_attempts(tmp_root):
    """The backlog is subtitle evidence; the audio/ASR kinds reuse the pair."""
    connection = open_database(tmp_root)
    try:
        part_id = _insert_user_video_part(connection)
        # The next iteration probes the same part under ``kind='audio'``: that
        # is process evidence, but not subtitle backlog evidence.
        connection.execute(
            """
            INSERT INTO acquisition_runs(
                run_id, kind, selector_kind, selector_target, requested_limit,
                credential_present, started_at, finished_at, outcome
            ) VALUES ('run-audio', 'audio', 'pending', NULL, NULL, 0, 100, 200, 'complete')
            """
        )
        _insert_attempt(
            connection,
            run_id="run-audio",
            video_part_id=part_id,
            outcome="failed",
            error_code="timeout",
            transcript_id=None,
            started_at=100,
            finished_at=200,
        )
        assert tuple(
            connection.execute(
                "SELECT attempted, last_attempt_at, last_attempt_outcome "
                "FROM v_pending_subtitles WHERE video_part_id = ?",
                (part_id,),
            ).fetchone()
        ) == (0, None, None)

        # A subtitle attempt on the same part is what makes it attempted; the
        # audio attempt neither hides it nor is reported as its evidence.
        _insert_subtitle_acquisition_run(
            connection,
            run_id="run-subs",
            credential_present=1,
            finished_at=400,
            outcome="complete",
        )
        _insert_attempt(
            connection,
            run_id="run-subs",
            video_part_id=part_id,
            outcome="no-subtitle",
            error_code=None,
            transcript_id=None,
            started_at=300,
            finished_at=400,
        )
        assert tuple(
            connection.execute(
                "SELECT attempted, last_attempt_at, last_attempt_outcome, "
                "last_attempt_credential_present FROM v_pending_subtitles "
                "WHERE video_part_id = ?",
                (part_id,),
            ).fetchone()
        ) == (1, 400, "no-subtitle", 1)
    finally:
        connection.close()


@pytest.mark.parametrize("missing_column", ["credential_verified", "absence_verified"])
def test_old_acquisition_attempt_shape_requires_recollection(tmp_root, missing_column):
    """Old observations are rejected, without ALTER TABLE or row preservation."""
    db_path = Path(tmp_root) / "archive.db"
    storage = resources.files("bili_asr.storage")
    with sqlite3.connect(db_path) as previous:
        for name in ("schema.sql", "schema-transcripts.sql", "schema-workflow.sql", "schema-editorial.sql"):
            script = storage.joinpath(name).read_text(encoding="utf-8")
            script = script.replace(
                f"    {missing_column} INTEGER NOT NULL DEFAULT 0 CHECK ({missing_column} IN (0, 1)),\n", ""
            )
            previous.executescript(script)
    before = db_path.read_bytes()
    with pytest.raises(SchemaContractError, match="unsupported table acquisition_attempts"):
        open_database(db_path)
    assert db_path.read_bytes() == before

def test_missing_audio_requires_credentialed_empty_inventory_confirmations(tmp_root):
    """Anonymous empty inventories never authorize the paid audio branch."""
    connection = open_database(tmp_root)
    try:
        part_id = _insert_user_video_part(connection)
        for run_id, credential_present, started_at in (
            ("anon-1", 0, 100),
            ("anon-2", 0, 200),
        ):
            _insert_subtitle_acquisition_run(
                connection,
                run_id=run_id,
                credential_present=credential_present,
                finished_at=started_at + 10,
                outcome="complete",
            )
            _insert_attempt(
                connection,
                run_id=run_id,
                video_part_id=part_id,
                outcome="no-subtitle",
                error_code=None,
                transcript_id=None,
                started_at=started_at,
                finished_at=started_at + 10,
            )
        connection.commit()
        assert connection.execute(
            "SELECT COUNT(*) FROM v_missing_audio WHERE video_part_id = ?",
            (part_id,),
        ).fetchone()[0] == 0

        for run_id, started_at in (("auth-1", 300), ("auth-2", 400)):
            _insert_subtitle_acquisition_run(
                connection,
                run_id=run_id,
                credential_present=1,
                finished_at=started_at + 10,
                outcome="complete",
            )
            _insert_attempt(
                connection,
                run_id=run_id,
                video_part_id=part_id,
                outcome="no-subtitle",
                error_code=None,
                transcript_id=None,
                started_at=started_at,
                finished_at=started_at + 10,
                credential_verified=True,
            )
        connection.commit()
        assert connection.execute(
            "SELECT COUNT(*) FROM v_missing_audio WHERE video_part_id = ?",
            (part_id,),
        ).fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.parametrize(("outcome", "error_code", "transcript_id"), LEGAL_ATTEMPTS)
def test_attempt_check_matrix_accepts_legal_evidence(
    tmp_root, outcome, error_code, transcript_id
):
    connection = open_database(tmp_root)
    try:
        _insert_attempt_parents(connection)
        _insert_attempt(
            connection,
            outcome=outcome,
            error_code=error_code,
            transcript_id=transcript_id,
        )
        row = connection.execute(
            "SELECT outcome, error_code, transcript_id FROM acquisition_attempts"
        ).fetchone()
        assert tuple(row) == (outcome, error_code, transcript_id)
    finally:
        connection.close()


@pytest.mark.parametrize(("outcome", "error_code", "transcript_id"), ILLEGAL_ATTEMPTS)
def test_attempt_check_matrix_rejects_illegal_evidence(
    tmp_root, outcome, error_code, transcript_id
):
    connection = open_database(tmp_root)
    try:
        _insert_attempt_parents(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_attempt(
                connection,
                outcome=outcome,
                error_code=error_code,
                transcript_id=transcript_id,
            )
    finally:
        connection.close()


def test_one_outcome_per_attempted_part_per_run(tmp_root):
    connection = open_database(tmp_root)
    try:
        _insert_attempt_parents(connection)
        _insert_attempt(
            connection, outcome="no-subtitle", error_code=None, transcript_id=None
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_attempt(
                connection, outcome="failed", error_code="timeout", transcript_id=None
            )
        # Attempts are append-only evidence scoped to their run: the same part
        # is recorded again by a later run, and that is not a conflict.
        _insert_subtitle_acquisition_run(connection, run_id="run-subs-2")
        _insert_attempt(
            connection,
            run_id="run-subs-2",
            outcome="stored",
            error_code=None,
            transcript_id=1,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM acquisition_attempts"
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_transcript_segment_record_mirrors_the_segment_invariant():
    segment = TranscriptSegmentRecord(start_ms=0, end_ms=1200, text="第一句")
    assert (segment.start_ms, segment.end_ms, segment.text) == (0, 1200, "第一句")
    # Caption text is verbatim: control characters survive, unlike the
    # metadata records' short code fields.
    assert TranscriptSegmentRecord(
        start_ms=0, end_ms=1, text=" 多行\n文本 "
    ).text == " 多行\n文本 "

    for invalid in (
        {"start_ms": -1, "end_ms": 10, "text": "x"},
        {"start_ms": 10, "end_ms": 10, "text": "x"},
        {"start_ms": 10, "end_ms": 9, "text": "x"},
        {"start_ms": 0, "end_ms": 10, "text": "   "},
    ):
        with pytest.raises(ValueError):
            TranscriptSegmentRecord(**invalid)
    with pytest.raises(TypeError):
        TranscriptSegmentRecord(start_ms=0, end_ms=10, text=1)


def test_transcript_write_result_validates_the_locked_shape():
    result = TranscriptWriteResult(
        outcome="stored", transcript_id=7, version=2, content_sha256="a" * 64
    )
    assert (result.outcome, result.transcript_id, result.version) == ("stored", 7, 2)

    for invalid in (
        {"outcome": "no-subtitle", "transcript_id": 1, "version": 1,
         "content_sha256": "a" * 64},
        {"outcome": "failed", "transcript_id": 1, "version": 1,
         "content_sha256": "a" * 64},
        {"outcome": "stored", "transcript_id": 0, "version": 1,
         "content_sha256": "a" * 64},
        {"outcome": "stored", "transcript_id": 1, "version": 0,
         "content_sha256": "a" * 64},
        {"outcome": "stored", "transcript_id": 1, "version": 1,
         "content_sha256": "A" * 64},
        {"outcome": "stored", "transcript_id": 1, "version": 1,
         "content_sha256": "a" * 63},
    ):
        with pytest.raises(ValueError):
            TranscriptWriteResult(**invalid)
    with pytest.raises(TypeError):
        TranscriptWriteResult(
            outcome="stored", transcript_id=1, version=1, content_sha256=None
        )


def _acquisition_run(**overrides):
    values = {
        "run_id": "run-subs-1",
        "kind": "subtitle",
        "selector_kind": "pending",
        "selector_target": None,
        "requested_limit": 25,
        "credential_present": False,
        "started_at": 100,
    }
    values.update(overrides)
    return AcquisitionRunRecord(**values)


def test_acquisition_run_record_validates_the_locked_shape():
    assert _acquisition_run().outcome == "running"
    assert _acquisition_run().finished_at is None
    assert (
        _acquisition_run(selector_kind="bvid", selector_target="BV1TEST:p0")
        .selector_target
        == "BV1TEST:p0"
    )
    assert _acquisition_run(outcome="complete", finished_at=200).outcome == "complete"

    for invalid in (
        {"run_id": "   "},
        {"kind": "video"},
        {"selector_kind": "pending", "selector_target": "BV1TEST"},
        {"selector_kind": "bvid", "selector_target": None},
        {"selector_kind": "bvid", "selector_target": "   "},
        {"requested_limit": 0},
        {"outcome": "limited"},
        # A terminal outcome without its finish time would store a run that
        # claims to be over and cannot say when.
        {"outcome": "complete"},
        {"finished_at": 99},
    ):
        with pytest.raises(ValueError):
            _acquisition_run(**invalid)
    with pytest.raises(TypeError):
        _acquisition_run(credential_present=1)
    with pytest.raises(TypeError):
        _acquisition_run(started_at="100")


def test_a_stale_view_body_is_refreshed_on_open(tmp_path):
    """A corrected view predicate must reach an EXISTING archive.

    Every view in the transcript script is declared ``CREATE VIEW IF NOT
    EXISTS``, and ``initialize_schema`` re-executes that script on every open.
    Without a refresh, SQLite keeps the *old* body and a corrected predicate
    silently never applies to an archive that already exists — the same failure
    class as the ``acquisition_attempts`` CHECK constraint, which is why this is
    pinned rather than assumed.

    The refresh must also not turn every open into a write: a current database
    is compared first and left alone, so a read-only archive still opens.
    """
    database_path = tmp_path / "archive.db"
    connection = open_database(database_path)
    try:
        require_subtitle_schema(connection)
        shipped = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'v_missing_audio'"
            ).fetchone()[0]
        )
        assert "confirmations" in shipped
    finally:
        connection.close()

    # Replace the view with a stale body, as an archive created by an older
    # build would carry.
    connection = open_database(database_path)
    try:
        connection.execute("DROP VIEW v_missing_audio")
        connection.execute(
            "CREATE VIEW v_missing_audio AS SELECT 1 AS video_part_id"
        )
        connection.commit()
        assert "confirmations" not in _stored_ddl(connection, "v_missing_audio")
    finally:
        connection.close()

    # Reopening refreshes it: the shipped body wins over the stale one.
    reopened = open_database(database_path)
    try:
        assert "confirmations" in _stored_ddl(reopened, "v_missing_audio")
    finally:
        reopened.close()


def test_refreshing_views_leaves_a_current_archive_untouched(tmp_path):
    """A current archive is not written on open, so read-only opens keep working.

    ``refresh_shipped_views`` compares the stored body with the shipped one
    before touching anything.  That comparison is what keeps a read-only archive
    (or one opened while another handle holds a read transaction) from being
    forced into a write it does not need, so the observable contract is:
    reopening a current database changes neither the file nor its schema version.
    """
    database_path = tmp_path / "archive.db"
    connection = open_database(database_path)
    try:
        require_subtitle_schema(connection)
    finally:
        connection.close()

    before = os.stat(database_path)
    connection = open_database(database_path)
    try:
        version_before = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        assert refresh_shipped_views(connection) == 0
        version_after = int(connection.execute("PRAGMA schema_version").fetchone()[0])
    finally:
        connection.close()

    after = os.stat(database_path)
    assert version_after == version_before, "a current archive must not be rewritten"
    assert after.st_mtime_ns == before.st_mtime_ns, "the file must not be touched"


def test_a_failing_view_refresh_leaves_the_view_in_place(tmp_path):
    """The refresh is atomic: a failure must not leave a view missing.

    A plain ``_transaction`` block does NOT give this: it commits but never
    issues ``BEGIN``, so a ``DROP VIEW`` inside it autocommits and a failing
    ``CREATE`` leaves the view absent from ``sqlite_master``.  The queue readers
    would then raise a raw ``no such table``.  The refresh uses a savepoint, and
    this pins that — the failure is injected through the shipped-body lookup so
    the real code path runs.
    """
    from bili_asr.storage import database as database_module

    database_path = tmp_path / "archive.db"
    connection = open_database(database_path)
    try:
        require_subtitle_schema(connection)
        # Make the stored body stale so the refresh actually fires, then break
        # the replacement statement so its CREATE fails.
        connection.execute("DROP VIEW v_missing_audio")
        connection.execute("CREATE VIEW v_missing_audio AS SELECT 1 AS video_part_id")
        connection.commit()

        real_bodies = _module_storage_database._shipped_view_bodies

        def broken() -> dict[str, str]:
            bodies = real_bodies()
            bodies["v_missing_audio"] = (
                "CREATE VIEW IF NOT EXISTS v_missing_audio AS SELECT FROM WHERE"
            )
            return bodies

        _module_storage_database._shipped_view_bodies = broken
        try:
            with pytest.raises(sqlite3.Error):
                refresh_shipped_views(connection)
        finally:
            _module_storage_database._shipped_view_bodies = real_bodies

        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name = 'v_missing_audio'"
            ).fetchone()[0]
            == 1
        ), "a failed refresh must leave the previous view in place"
    finally:
        connection.close()


def test_the_view_name_cannot_come_from_a_comment():
    """The shipped-name extractor reads the statement, not its comment block.

    The transcript script is densely commented, and a comment that quotes the
    words "CREATE VIEW" would otherwise supply the name for the *next*
    statement — the refresh would then drop the wrong view and recreate it with
    the wrong body.
    """
    statement = (
        "-- unlike CREATE VIEW IF NOT EXISTS v_decoy AS ...\n"
        "CREATE VIEW IF NOT EXISTS v_real AS SELECT 1 AS x"
    )
    assert _statement_view_name(statement) == "v_real"

    # Every shipped view still resolves to its own name.
    assert set(_shipped_view_bodies()) == {
        "v_pending_subtitles",
        "v_missing_subtitle",
        "v_missing_audio",
        "v_missing_transcript",
        "v_part_pipeline",
    }


def test_the_normalized_form_is_readable_prose():
    """The normalized form must be the statement, not a mangling of it.

    Equality alone cannot catch this: a normalizer that emitted the statement one
    character per list item and rejoined it with spaces produced garbage that was
    still *symmetrically* garbage, so every ``a == b`` assertion passed while the
    function returned nonsense.  (This actually happened in development, and the
    suite was green.)  Asserting the exact text is what makes that visible.
    """
    assert _normalize_view_sql("CREATE VIEW v AS SELECT 1 AS x") == (
        "create view v as select 1 as x"
    )
    # A literal survives intact, including its case and internal spacing.
    assert _normalize_view_sql("CREATE VIEW v AS SELECT 'Keep  Me' AS x") == (
        "create view v as select 'Keep  Me' as x"
    )
    # Comments vanish without fusing the tokens around them.
    assert _normalize_view_sql("CREATE VIEW v AS SELECT 1 -- note") == (
        "create view v as select 1"
    )


def test_the_normalized_form_of_a_real_view_is_the_whole_statement():
    """Pin a REAL shipped body, not a toy, or the guard above is length-blind.

    The statements in the test above are under 60 characters, while the shipped
    views normalize to 397-1417.  A symmetric defect gated on length — "if the
    text is longer than 500 characters, return the first 100" — therefore passed
    every suite, and with it a genuinely stale real-length body was judged current
    and silently never refreshed.  That is the original defect, reachable through
    the very guard added to prevent it.

    So this pins a real body three ways: the tokens that sit far past any short
    prefix must survive, the result must have the body's full length, and it must
    equal the normalization of what SQLite actually stores for that statement.
    """
    bodies = _shipped_view_bodies()
    shipped = bodies["v_missing_audio"]
    normalized = _normalize_view_sql(shipped)

    # (a) load-bearing tokens far past any short prefix
    assert "confirmations" in normalized, (
        "missing 'confirmations' - the normalized form is not the full statement"
    )
    assert "error_code is null" in normalized, (
        "missing the corroboration filter - normalized form truncated"
    )
    # (b) the full length, not a prefix of it
    assert len(normalized) > 600, (
        f"normalized v_missing_audio is only {len(normalized)} chars; the shipped "
        "body normalizes to over a thousand, so the form has been truncated"
    )
    # (c) it agrees with what SQLite stores for the same statement
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(shipped.replace("IF NOT EXISTS ", "", 1))
        stored = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'v_missing_audio'"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    assert normalized == _normalize_view_sql(stored), (
        "the shipped form must normalize to the same shape SQLite stores"
    )


def test_a_trailing_comment_does_not_make_a_view_look_stale():
    """Both comment forms are removed, so neither can force a rewrite loop.

    SQLite stores a statement's text as written, so a view shipped with a
    trailing comment would otherwise differ from itself on every reopen and be
    rewritten forever.
    """
    assert _normalize_view_sql("CREATE VIEW v AS SELECT 1 -- note") == _normalize_view_sql(
        "CREATE VIEW v AS SELECT 1"
    )
    assert _normalize_view_sql(
        "CREATE VIEW v AS\nSELECT 1\n-- note\n"
    ) == _normalize_view_sql("CREATE VIEW v AS SELECT 1")
    # A comment marker inside a literal is data, not a comment.
    assert _normalize_view_sql("CREATE VIEW v AS SELECT '--' AS x") == (
        "create view v as select '--' as x"
    )


def test_a_literal_only_difference_is_treated_as_stale():
    """Normalization must not erase differences inside string literals.

    Collapsing whitespace or folding case across a whole statement would also
    rewrite literal text, so a stored body differing from the shipped one only
    inside a literal would read as current and never refresh.
    """
    assert _normalize_view_sql(
        "CREATE VIEW v AS SELECT 'no-subtitle' AS x"
    ) != _normalize_view_sql("CREATE VIEW v AS SELECT 'NO-SUBTITLE' AS x")
    assert _normalize_view_sql(
        "CREATE VIEW v AS SELECT 'a  b' AS x"
    ) != _normalize_view_sql("CREATE VIEW v AS SELECT 'a b' AS x")
    # Formatting outside literals still compares equal, so a current view is
    # never needlessly rewritten.
    assert _normalize_view_sql(
        "CREATE VIEW   v\n  AS   SELECT  1 AS x"
    ) == _normalize_view_sql("CREATE VIEW v AS SELECT 1 AS x")


def test_every_quoting_form_is_respected_when_stripping_comments():
    """A ``--`` inside ANY quoted region is data, not a comment.

    Only understanding single quotes would truncate the statement at the first
    ``--`` inside a double-quoted, backtick or bracketed identifier, making two
    views that select *different* columns normalize equal — a false negative, so
    a genuinely stale body would be judged current and never refresh.  Block
    comments must be removed rather than truncated at.
    """
    for left, right, label in (
        (
            'CREATE VIEW v AS SELECT 1 AS "a--b"',
            'CREATE VIEW v AS SELECT 1 AS "a--c"',
            "double-quoted identifier",
        ),
        (
            "CREATE VIEW v AS SELECT 1 AS `a--b`",
            "CREATE VIEW v AS SELECT 1 AS `a--c`",
            "backtick identifier",
        ),
        (
            "CREATE VIEW v AS SELECT 1 AS [a--b]",
            "CREATE VIEW v AS SELECT 1 AS [a--c]",
            "bracket identifier",
        ),
        (
            "CREATE VIEW v AS SELECT 1 /* -- */ AS x",
            "CREATE VIEW v AS SELECT 1 /* -- */ AS y",
            "block comment between differing tokens",
        ),
    ):
        assert _normalize_view_sql(left) != _normalize_view_sql(right), label

    # ...and a real comment is still removed.
    assert _normalize_view_sql("CREATE VIEW v AS SELECT 1 /* note */") == (
        "create view v as select 1"
    )
    assert _strip_sql_comments("SELECT '--' , /* x */ 2") == "SELECT '--' ,  2"
