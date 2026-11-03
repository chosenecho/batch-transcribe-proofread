"""Opt-in scale evidence for the current SQLite workflow, without media or models."""
import time

import pytest

from bili_asr.storage import AsrPolicy, JobKind, WorkflowRepository, open_database
from tests.test_workflow_control_plane import _profile, _seed_part


@pytest.mark.scale(skip_reason="SQLite scale validation is opt-in: set BILI_SCALE=1")
@pytest.mark.parametrize("part_count", [1001, 10001])
def test_sqlite_planning_replay_and_attempt_history_at_scale(tmp_path, opt_in_gate, record_testsuite_property, part_count):
    connection = open_database(tmp_path)
    try:
        _seed_part(connection)
        with connection:
            connection.executemany(
                "INSERT INTO video_parts(video_part_id,bvid,page_index,cid,title,duration_ms,"
                "processing_status,created_at,updated_at) "
                "VALUES (?,'BVtest',?,?,'synthetic scale part',1000,'metadata_collected',1,1)",
                ((part, part - 1, part) for part in range(2, part_count + 1)),
            )
        repository = WorkflowRepository(connection)
        profile = _profile(repository)
        started = time.monotonic()
        plan = repository.plan(part_ids=range(1, part_count + 1), policy=AsrPolicy.ALL, profile_id=profile)
        record_testsuite_property(f"planning_seconds_{part_count}_parts", time.monotonic() - started)
        assert plan.subtitle_jobs == plan.audio_jobs == plan.asr_jobs == part_count
        replay = repository.plan(part_ids=range(1, part_count + 1), policy=AsrPolicy.ALL, profile_id=profile)
        assert replay.subtitle_jobs == replay.audio_jobs == replay.asr_jobs == 0
        assert connection.execute("SELECT COUNT(*) FROM workflow_jobs").fetchone()[0] == 3 * part_count
        claimed = set()
        for index in range(50):
            job = repository.claim("scale-worker", kinds=[JobKind.AUDIO])
            assert job is not None and job.job_id not in claimed
            claimed.add(job.job_id)
            if index % 2:
                repository.fail(job.job_id, worker_id="scale-worker", error_code="synthetic_scale_failure")
            else:
                repository.finish(job.job_id, worker_id="scale-worker", result={})
        assert repository.peek_candidate(kinds=[JobKind.ASR]) is not None
    finally:
        connection.close()
    # Reopening must retain failures and successful attempts without requeueing.
    connection = open_database(tmp_path)
    try:
        states = dict(connection.execute("SELECT status,COUNT(*) FROM workflow_jobs GROUP BY status"))
        assert states == {"queued": 3 * part_count - 50, "failed": 25, "succeeded": 25}
        attempts = dict(connection.execute("SELECT outcome,COUNT(*) FROM workflow_attempts GROUP BY outcome"))
        assert attempts == {"failed": 25, "succeeded": 25}
        assert connection.execute("SELECT COUNT(DISTINCT job_id) FROM workflow_attempts").fetchone()[0] == 50
    finally:
        connection.close()
