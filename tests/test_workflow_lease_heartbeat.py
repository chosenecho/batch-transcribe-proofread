"""Regression tests for long-running workflow attempts."""

from __future__ import annotations

import time

from bili_asr.storage import JobKind, WorkflowRepository, open_database
from bili_asr.asr import ASRInferenceTimeoutError
from bili_asr.workflow import WorkflowExecutor


def _seed_publish_job(connection) -> str:
    with connection:
        connection.execute(
            "INSERT INTO bilibili_users(mid, display_name, created_at, updated_at) "
            "VALUES (1, 'test', 1, 1)"
        )
        connection.execute(
            "INSERT INTO videos(bvid, aid, mid, title, pubdate, created_at, updated_at) "
            "VALUES ('BVlease', 1, 1, 'test', 1, 1, 1)"
        )
        connection.execute(
            "INSERT INTO video_parts(video_part_id, bvid, page_index, cid, title, duration_ms, "
            "processing_status, created_at, updated_at) VALUES "
            "(1, 'BVlease', 0, 1, 'p0', 1000, 'metadata_collected', 1, 1)"
        )
    job_id, _ = WorkflowRepository(connection).request_publication(
        video_part_id=1, transcript_id=1
    )
    return job_id


def test_executor_renews_a_lease_during_a_long_handler(tmp_path) -> None:
    connection = open_database(tmp_path)
    try:
        job_id = _seed_publish_job(connection)
        repository = WorkflowRepository(connection)

        def handler(_job):
            # Outlive the lease while leaving one second of scheduling margin:
            # stored timestamps have whole-second precision, so a one-second
            # lease can expire between a heartbeat and the terminal write.
            time.sleep(3.2)
            return {"ok": True, "requested_transcript_id": 1}

        summary = WorkflowExecutor(
            repository,
            worker_id="long-worker",
            handlers={JobKind.PUBLISH: handler},
            lease_seconds=2,
            heartbeat_interval_seconds=0.1,
        ).run(limit=1)

        assert (summary.succeeded, summary.failed, summary.idle) == (1, 0, False)
        job = connection.execute(
            "SELECT status, attempt_count, lease_owner, lease_expires_at FROM workflow_jobs "
            "WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        assert job["status"] == "succeeded"
        assert job["attempt_count"] == 1
        assert job["lease_owner"] is None
        assert job["lease_expires_at"] is None
        attempt = connection.execute(
            "SELECT outcome FROM workflow_attempts WHERE job_id = ?", (job_id,)
        ).fetchone()
        assert attempt["outcome"] == "succeeded"
    finally:
        connection.close()


def test_expired_attempt_is_reclaimed_and_old_worker_is_fenced(tmp_path) -> None:
    connection = open_database(tmp_path)
    try:
        job_id = _seed_publish_job(connection)
        repository = WorkflowRepository(connection)
        first = repository.claim("first-worker", lease_seconds=1)
        assert first is not None
        time.sleep(1.1)
        second = repository.claim("second-worker", lease_seconds=10)
        assert second is not None and second.attempt_count == first.attempt_count + 1
        repository.finish(
            second.job_id,
            worker_id="second-worker",
            result={"fresh": True, "requested_transcript_id": 1},
            expected_attempt_count=second.attempt_count,
        )
        assert connection.execute(
            "SELECT status, last_error_code FROM workflow_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()["status"] == "succeeded"
        assert connection.execute(
            "SELECT outcome, error_code FROM workflow_attempts WHERE job_id = ? "
            "ORDER BY rowid LIMIT 1",
            (job_id,),
        ).fetchone()["error_code"] == "lease_expired"
    finally:
        connection.close()


def test_inference_timeout_is_persisted_as_a_retryable_workflow_failure(tmp_path) -> None:
    connection = open_database(tmp_path)
    try:
        job_id = _seed_publish_job(connection)
        repository = WorkflowRepository(connection)

        def handler(_job):
            raise ASRInferenceTimeoutError("forced alignment deadline")

        summary = WorkflowExecutor(
            repository,
            worker_id="timeout-worker",
            handlers={JobKind.PUBLISH: handler},
            lease_seconds=10,
            heartbeat_interval_seconds=1,
        ).run()

        assert summary.failed == 1
        row = connection.execute(
            "SELECT status, last_error_code FROM workflow_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        assert (row["status"], row["last_error_code"]) == ("failed", "inference_timeout")
    finally:
        connection.close()
