"""SQLite work for gap queues excludes history of ineligible parts."""

from __future__ import annotations

import pytest

from bili_asr.storage import (
    AcquisitionRunRecord,
    MetadataRepository,
    TranscriptRepository,
    TranscriptSegmentRecord,
    UserRecord,
    VideoPartRecord,
    VideoRecord,
    open_database,
)

_MID = 23191782
_QUEUE_QUERY = (
    "SELECT * FROM v_pending_subtitles "
    "ORDER BY attempted, last_attempt_at, bvid, page_index LIMIT 1"
)
_COUNT_QUERY = "SELECT COUNT(*) FROM v_pending_subtitles"
_AUDIO_QUEUE_QUERY = (
    "SELECT * FROM v_missing_audio "
    "ORDER BY pubdate DESC, bvid, page_index LIMIT 1"
)
_AUDIO_COUNT_QUERY = "SELECT COUNT(*) FROM v_missing_audio"

# The previous shipped view, with all subtitle history windowed before the
# eligible-part predicate. This is a real older view for the reopen migration.
_PREVIOUS_VIEW = """
CREATE VIEW v_pending_subtitles AS
WITH subtitle_attempts AS (
    SELECT aa.video_part_id, aa.finished_at, aa.outcome, aa.error_code,
           ar.credential_present,
           ROW_NUMBER() OVER (
               PARTITION BY aa.video_part_id
               ORDER BY aa.finished_at DESC, ar.started_at DESC, ar.rowid DESC
           ) AS recency
    FROM acquisition_attempts AS aa
    JOIN acquisition_runs AS ar ON ar.run_id = aa.run_id
    WHERE ar.kind = 'subtitle'
)
SELECT vp.video_part_id, vp.bvid || ':p' || vp.page_index AS work_id,
       vp.bvid, vp.page_index, vp.cid, vp.title AS part_title, vp.duration_ms,
       CASE WHEN latest.video_part_id IS NULL THEN 0 ELSE 1 END AS attempted,
       latest.finished_at AS last_attempt_at,
       latest.outcome AS last_attempt_outcome,
       latest.error_code AS last_attempt_error_code,
       latest.credential_present AS last_attempt_credential_present
FROM video_parts AS vp
LEFT JOIN subtitle_attempts AS latest
    ON latest.video_part_id = vp.video_part_id AND latest.recency = 1
WHERE vp.processing_status <> 'gone'
  AND NOT EXISTS (
      SELECT 1 FROM transcripts AS t WHERE t.video_part_id = vp.video_part_id
  )
"""

_PREVIOUS_AUDIO_VIEW = """
CREATE VIEW v_missing_audio AS
WITH subtitle_attempts AS (
    SELECT aa.video_part_id, aa.outcome, aa.error_code, aa.run_id,
           ar.credential_present, aa.credential_verified, aa.absence_verified,
           ROW_NUMBER() OVER (
               PARTITION BY aa.video_part_id
               ORDER BY aa.finished_at DESC, ar.started_at DESC, ar.rowid DESC
           ) AS recency
    FROM acquisition_attempts AS aa
    JOIN acquisition_runs AS ar ON ar.run_id = aa.run_id
    WHERE ar.kind = 'subtitle'
), empty_inventory_confirmations AS (
    SELECT video_part_id, COUNT(DISTINCT run_id) AS confirmations
    FROM subtitle_attempts
    WHERE outcome = 'no-subtitle' AND error_code IS NULL
      AND credential_present = 1 AND credential_verified = 1
    GROUP BY video_part_id
)
SELECT vp.video_part_id, vp.bvid || ':p' || vp.page_index AS work_id,
       vp.bvid, vp.page_index, vp.cid, vp.title AS part_title, vp.duration_ms,
       v.title AS video_title, v.pubdate,
       latest.outcome AS newest_outcome, latest.error_code AS newest_error_code
FROM video_parts AS vp
JOIN videos AS v ON vp.bvid = v.bvid
JOIN subtitle_attempts AS latest
    ON latest.video_part_id = vp.video_part_id AND latest.recency = 1
LEFT JOIN empty_inventory_confirmations AS eic
    ON eic.video_part_id = vp.video_part_id
WHERE vp.processing_status <> 'gone'
  AND NOT EXISTS (
      SELECT 1 FROM transcripts AS t WHERE t.video_part_id = vp.video_part_id
  )
  AND latest.outcome = 'no-subtitle'
  AND ((latest.error_code = 'not_found' AND latest.absence_verified = 1)
       OR (latest.error_code IS NULL
           AND latest.credential_present = 1 AND latest.credential_verified = 1
           AND COALESCE(eic.confirmations, 0) >= 2))
  AND NOT EXISTS (
      SELECT 1 FROM part_audio_objects AS pao WHERE pao.video_part_id = vp.video_part_id
  )
"""


def _seed(connection):
    metadata = MetadataRepository(connection)
    with metadata.transaction():
        metadata.upsert_user(UserRecord(
            mid=_MID, display_name="owner", created_at=1, updated_at=1
        ))
        metadata.upsert_video(VideoRecord(
            bvid="BV1COST0001", aid=1, mid=_MID, title="history", pubdate=1,
            created_at=1, updated_at=1,
        ))
        for page_index, status in enumerate(("metadata_collected", "metadata_collected", "gone")):
            metadata.upsert_part(VideoPartRecord(
                bvid="BV1COST0001", page_index=page_index, cid=100 + page_index,
                title=f"part {page_index}", duration_ms=1000,
                processing_status=status, created_at=1, updated_at=1,
            ))
    parts = {row["page_index"]: row["video_part_id"] for row in connection.execute(
        "SELECT page_index, video_part_id FROM video_parts"
    )}
    transcripts = TranscriptRepository(connection)
    transcripts.start_acquisition_run(AcquisitionRunRecord(
        run_id="stored-caption", kind="subtitle", selector_kind="pending",
        selector_target=None, requested_limit=None, credential_present=False, started_at=10,
    ))
    transcripts.record_acquired_transcript(
        run_id="stored-caption", video_part_id=parts[1], source_kind="subtitle-ai",
        language="zh-CN", segments=(TranscriptSegmentRecord(
            start_ms=0, end_ms=1000, text="archived caption"
        ),), started_at=10, finished_at=11, created_at=12,
    )
    # Equal-second retries with reverse UUID order retain the insertion-order
    # rule. Filtering irrelevant history must not change pending evidence.
    for run_id, credential, outcome, code in (
        ("zz-old", True, "no-subtitle", None),
        ("aa-new", False, "failed", "transport_error"),
    ):
        transcripts.start_acquisition_run(AcquisitionRunRecord(
            run_id=run_id, kind="subtitle", selector_kind="pending", selector_target=None,
            requested_limit=None, credential_present=credential, started_at=50,
        ))
        transcripts.record_subtitle_attempt(
            run_id=run_id, video_part_id=parts[0], outcome=outcome, error_code=code,
            started_at=60, finished_at=70, credential_verified=credential,
        )
    return parts


def _irrelevant_history(connection, video_part_id, count=2000):
    connection.executemany(
        "INSERT INTO acquisition_runs(run_id, kind, selector_kind, selector_target, "
        "requested_limit, credential_present, started_at, outcome) "
        "VALUES (?, 'subtitle', 'pending', NULL, NULL, 0, ?, 'running')",
        ((f"history-{index}", index) for index in range(count)),
    )
    connection.executemany(
        "INSERT INTO acquisition_attempts(run_id, video_part_id, outcome, error_code, "
        "transcript_id, started_at, finished_at) "
        "VALUES (?, ?, 'failed', 'transport_error', NULL, ?, ?)",
        ((f"history-{index}", video_part_id, index, index + 1) for index in range(count)),
    )
    connection.commit()


def _seed_audio(connection):
    parts = _seed(connection)
    metadata = MetadataRepository(connection)
    metadata.upsert_part(VideoPartRecord(
        bvid="BV1COST0001", page_index=3, cid=103, title="already downloaded",
        duration_ms=1000, processing_status="metadata_collected", created_at=1,
        updated_at=1,
    ))
    parts[3] = connection.execute(
        "SELECT video_part_id FROM video_parts WHERE page_index = 3"
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO audio_objects(sha256, byte_size, format, duration_ms, "
        "storage_key, created_at) VALUES (?, 10, 'm4a', 1000, 'already.m4a', 1)",
        ("a" * 64,),
    )
    connection.execute(
        "INSERT INTO part_audio_objects(video_part_id, audio_id, acquired_at, "
        "acquisition_source) VALUES (?, last_insert_rowid(), 1, 'download')",
        (parts[3],),
    )
    connection.commit()
    transcripts = TranscriptRepository(connection)
    # A real nonempty download queue: two distinct authenticated observations
    # admit page 0, while transcript/gone/audio evidence excludes the others.
    for run_id in ("zz-empty-first", "aa-empty-second"):
        transcripts.start_acquisition_run(AcquisitionRunRecord(
            run_id=run_id, kind="subtitle", selector_kind="pending", selector_target=None,
            requested_limit=None, credential_present=True, started_at=200,
        ))
        for part_id in parts.values():
            transcripts.record_subtitle_attempt(
                run_id=run_id, video_part_id=part_id, outcome="no-subtitle",
                error_code=None, started_at=200, finished_at=300,
                credential_verified=True,
            )
    return parts


def _measured(connection, query):
    """Count real SQLite virtual-machine steps, independently of wall clock."""

    steps = 0

    def progress():
        nonlocal steps
        steps += 1
        return 0

    connection.set_progress_handler(progress, 1)
    try:
        rows = [tuple(row) for row in connection.execute(query)]
    finally:
        connection.set_progress_handler(None, 0)
    return rows, steps


@pytest.mark.parametrize("query", [_QUEUE_QUERY, _COUNT_QUERY], ids=["limit-one", "count"])
@pytest.mark.parametrize("excluded_part", [1, 2], ids=["transcribed", "gone"])
def test_excluded_part_history_does_not_inflate_pending_work(query, excluded_part):
    connection = open_database(":memory:")
    try:
        parts = _seed(connection)
        baseline_rows, baseline_steps = _measured(connection, query)
        _irrelevant_history(connection, parts[excluded_part])
        rows, steps = _measured(connection, query)
        assert rows == baseline_rows
        # 2000 excluded attempts used to add >100000 VM instructions. A small
        # allowance accommodates SQLite planning changes without allowing a
        # scan proportional to irrelevant history.
        assert steps <= baseline_steps + 500, (baseline_steps, steps)
        evidence = connection.execute(_QUEUE_QUERY).fetchone()
        assert evidence["work_id"] == "BV1COST0001:p0"
        assert evidence["last_attempt_outcome"] == "failed"
        assert evidence["last_attempt_error_code"] == "transport_error"
        assert evidence["last_attempt_credential_present"] == 0
        plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + query)]
        assert any(
            "SEARCH aa" in item and "video_part_id=?" in item for item in plan
        ), plan
    finally:
        connection.close()


def test_reopening_refreshes_the_old_unfiltered_view_without_changing_facts(tmp_root):
    connection = open_database(tmp_root)
    try:
        parts = _seed(connection)
        _irrelevant_history(connection, parts[1])
        connection.execute("DROP VIEW v_pending_subtitles")
        connection.execute(_PREVIOUS_VIEW)
        connection.commit()
        previous_rows, previous_steps = _measured(connection, _QUEUE_QUERY)
        facts = [tuple(row) for row in connection.execute(
            "SELECT * FROM acquisition_attempts ORDER BY run_id, video_part_id"
        )]
    finally:
        connection.close()
    reopened = open_database(tmp_root)
    try:
        rows, steps = _measured(reopened, _QUEUE_QUERY)
        assert rows == previous_rows
        assert previous_steps >= steps * 50, (previous_steps, steps)
        assert [tuple(row) for row in reopened.execute(
            "SELECT * FROM acquisition_attempts ORDER BY run_id, video_part_id"
        )] == facts
        assert "eligible_parts" in reopened.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'v_pending_subtitles'"
        ).fetchone()[0]
    finally:
        reopened.close()


@pytest.mark.parametrize("query", [_AUDIO_QUEUE_QUERY, _AUDIO_COUNT_QUERY], ids=["limit-one", "count"])
@pytest.mark.parametrize("excluded_part", [1, 2, 3], ids=["transcribed", "gone", "has-audio"])
def test_excluded_part_history_does_not_inflate_download_work(query, excluded_part):
    connection = open_database(":memory:")
    try:
        parts = _seed_audio(connection)
        baseline_rows, baseline_steps = _measured(connection, query)
        _irrelevant_history(connection, parts[excluded_part])
        rows, steps = _measured(connection, query)
        assert rows == baseline_rows
        assert steps <= baseline_steps + 500, (baseline_steps, steps)
        assert [row["work_id"] for row in connection.execute(_AUDIO_QUEUE_QUERY)] == [
            "BV1COST0001:p0"
        ]
        assert connection.execute(_AUDIO_COUNT_QUERY).fetchone()[0] == 1
        states = dict(connection.execute(
            "SELECT page_index, pipeline_state FROM v_part_pipeline"
        ))
        # The status view still reports gone-part history; its newest failed
        # attempt may change that state even though neither queue admits it.
        assert states == {
            0: "audio_pending", 1: "transcribed",
            2: "discovered" if excluded_part == 2 else "no_subtitle", 3: "audio_ok",
        }
        plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + query)]
        assert any("SEARCH aa" in item and "video_part_id=?" in item for item in plan), plan
    finally:
        connection.close()


def test_reopening_refreshes_old_download_view_preserving_proof_and_facts(tmp_root):
    connection = open_database(tmp_root)
    try:
        parts = _seed_audio(connection)
        _irrelevant_history(connection, parts[3])
        connection.execute("DROP VIEW v_missing_audio")
        connection.execute(_PREVIOUS_AUDIO_VIEW)
        connection.commit()
        previous_rows, previous_steps = _measured(connection, _AUDIO_QUEUE_QUERY)
        facts = [tuple(row) for row in connection.execute(
            "SELECT * FROM acquisition_attempts ORDER BY run_id, video_part_id"
        )]
    finally:
        connection.close()
    reopened = open_database(tmp_root)
    try:
        rows, steps = _measured(reopened, _AUDIO_QUEUE_QUERY)
        assert rows == previous_rows
        assert rows[0][1] == "BV1COST0001:p0"
        assert previous_steps >= steps * 50, (previous_steps, steps)
        assert [tuple(row) for row in reopened.execute(
            "SELECT * FROM acquisition_attempts ORDER BY run_id, video_part_id"
        )] == facts
        assert "eligible_parts" in reopened.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'v_missing_audio'"
        ).fetchone()[0]
    finally:
        reopened.close()
