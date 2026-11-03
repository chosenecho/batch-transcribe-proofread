"""Contract tests for the independent-producer workflow control plane."""

from __future__ import annotations

import json
import shutil
import subprocess
import sqlite3
import wave

import pytest

from bili_asr.storage import (
    AcquisitionRunRecord,
    AsrPolicy,
    AsrProfile,
    TranscriptRepository,
    TranscriptSegmentRecord,
    WorkflowRepository,
    open_database,
)
from bili_asr.coverage_report import CoverageReport
from bili_asr.export import export_records
from bili_asr.integrity import IntegrityVerifier
from bili_asr.services.workflow_projection import workflow_records
from bili_asr.workflow_runtime import ArchiveWorkflowHandlers


def test_metadata_only_archive_runs_without_manuscript_bootstrap(tmp_path, capsys) -> None:
    from bili_asr.cli import main
    from importlib import resources

    with sqlite3.connect(tmp_path / 'archive.db') as connection:
        package = resources.files("bili_asr.storage")
        for name in ("schema.sql", "schema-transcripts.sql", "schema-workflow.sql"):
            connection.executescript(package.joinpath(name).read_text("utf-8"))
    assert main(['workflow', 'run', '--archive-root', str(tmp_path), '--limit', '1']) == 0
    assert 'succeeded=0 failed=0' in capsys.readouterr().out
    assert main(['workflow', 'run', '--archive-root', str(tmp_path), '--only-editorial']) == 1
    assert 'manuscript-schema-contract' in capsys.readouterr().err
    with sqlite3.connect(tmp_path / 'archive.db') as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='manuscript_contract'").fetchone() is None


def test_workflow_legacy_manuscript_refusal_is_bounded_and_readonly(tmp_path, capsys) -> None:
    from bili_asr.cli import main

    path = tmp_path / 'archive.db'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE reading_publications(value TEXT)')
    before = path.read_bytes()
    assert main(['workflow', 'run', '--archive-root', str(tmp_path)]) == 1
    assert 'manuscript-schema-contract' in capsys.readouterr().err
    assert path.read_bytes() == before


def _seed_part(connection) -> int:
    with connection:
        connection.execute(
            "INSERT INTO bilibili_users(mid, display_name, created_at, updated_at) VALUES (1, 'test', 1, 1)"
        )
        connection.execute(
            """INSERT INTO videos(bvid, aid, mid, title, pubdate, created_at, updated_at)
               VALUES ('BVtest', 1, 1, 'test', 1, 1, 1)"""
        )
        connection.execute(
            """INSERT INTO video_parts(
                   video_part_id, bvid, page_index, cid, title, duration_ms,
                   processing_status, created_at, updated_at
               ) VALUES (1, 'BVtest', 0, 1, 'p0', 1000, 'metadata_collected', 1, 1)"""
        )
    return 1


def _profile(repo: WorkflowRepository, key: str = "cpu") -> int:
    return repo.register_profile(
        AsrProfile(profile_key=key, model_name="Qwen/Qwen3-ASR-1.7B-hf", device="cpu")
    )


def test_asr_dependency_is_audio_only_and_subtitle_failure_does_not_block_it(tmp_path) -> None:
    connection = open_database(tmp_path)
    try:
        part_id = _seed_part(connection)
        repo = WorkflowRepository(connection)
        plan = repo.plan(part_ids=[part_id], policy=AsrPolicy.ALL, profile_id=_profile(repo))
        assert plan.subtitle_jobs == plan.audio_jobs == plan.asr_jobs == 1

        claimed = [repo.claim("worker"), repo.claim("worker")]
        assert all(job is not None for job in claimed)
        by_kind = {job.kind.value: job for job in claimed if job is not None}
        assert set(by_kind) == {"subtitle", "audio"}
        repo.fail(by_kind["subtitle"].job_id, worker_id="worker", error_code="upstream_error")
        repo.finish(
            by_kind["audio"].job_id,
            worker_id="worker",
            result={"storage_key": "audio/test.m4a"},
        )
        asr = repo.claim("worker")
        assert asr is not None and asr.kind.value == "asr"
        assert asr.video_part_id == part_id
    finally:
        connection.close()


def test_asr_profiles_are_immutable_and_existing_jobs_keep_their_model(tmp_path) -> None:
    connection = open_database(tmp_path)
    try:
        part_id = _seed_part(connection)
        repo = WorkflowRepository(connection)
        first = repo.register_profile(AsrProfile(profile_key="stable", model_name="model-v1"))
        repo.plan(part_ids=[part_id], policy=AsrPolicy.ALL, profile_id=first)
        old_job = connection.execute(
            "SELECT profile_id, payload_json FROM workflow_jobs WHERE kind = 'asr'"
        ).fetchone()

        second = repo.register_profile(AsrProfile(profile_key="stable", model_name="model-v2"))
        assert second != first
        assert repo.profile(int(old_job["profile_id"])).model_name == "model-v1"
        repo.plan(part_ids=[part_id], policy=AsrPolicy.ALL, profile_id=second)
        assert connection.execute(
            "SELECT COUNT(*) FROM workflow_jobs WHERE kind = 'asr'"
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_asr_job_snapshots_the_reference_transcript_at_plan_time(tmp_path) -> None:
    connection = open_database(tmp_path)
    try:
        part_id = _seed_part(connection)
        transcript_repo = TranscriptRepository(connection)
        transcript_repo.start_acquisition_run(
            AcquisitionRunRecord(
                run_id="snapshot-caption",
                kind="subtitle",
                selector_kind="bvid",
                selector_target="BVtest",
                requested_limit=1,
                credential_present=False,
                started_at=1,
            )
        )
        stored = transcript_repo.record_acquired_transcript(
            run_id="snapshot-caption",
            video_part_id=part_id,
            source_kind="subtitle-ai",
            language="zh-CN",
            segments=(TranscriptSegmentRecord(start_ms=0, end_ms=1000, text="first"),),
            started_at=1,
            finished_at=2,
            created_at=2,
        )
        transcript_repo.finish_acquisition_run("snapshot-caption", 2)
        repo = WorkflowRepository(connection)
        profile = repo.register_profile(AsrProfile(profile_key="snapshot", model_name="model"))
        repo.plan(part_ids=[part_id], policy=AsrPolicy.ALL, profile_id=profile)
        payload = connection.execute(
            "SELECT payload_json FROM workflow_jobs WHERE kind = 'asr'"
        ).fetchone()[0]
        assert json.loads(payload)["reference_transcript_id"] == stored.transcript_id
    finally:
        connection.close()


def test_audio_and_asr_are_planned_regardless_of_quality_or_subtitle_presence(tmp_path) -> None:
    connection = open_database(tmp_path)
    try:
        part_id = _seed_part(connection)
        repo = WorkflowRepository(connection)
        profile_id = _profile(repo)
        with connection:
            connection.execute(
                """INSERT INTO workflow_quality_assessments(
                       video_part_id, source_kind, score, assessor, details_json, assessed_at
                   ) VALUES (?, 'subtitle-ai', 0.2, 'test', '{}', 1)""",
                (part_id,),
            )
        first = repo.plan(part_ids=[part_id], policy=AsrPolicy.ALL, profile_id=profile_id)
        assert first.subtitle_jobs == first.audio_jobs == first.asr_jobs == 1
        second = repo.plan(part_ids=[part_id], policy=AsrPolicy.ALL, profile_id=profile_id)
        assert second.subtitle_jobs == second.audio_jobs == second.asr_jobs == 0
    finally:
        connection.close()


def test_publish_job_projects_the_current_best_transcript_into_an_archive_bundle(tmp_path) -> None:
    connection = open_database(tmp_path)
    try:
        part_id = _seed_part(connection)
        transcript_repo = TranscriptRepository(connection)
        transcript_repo.start_acquisition_run(
            AcquisitionRunRecord(
                run_id="subtitle-run",
                kind="subtitle",
                selector_kind="bvid",
                selector_target="BVtest",
                requested_limit=1,
                credential_present=False,
                started_at=1,
            )
        )
        stored = transcript_repo.record_acquired_transcript(
            run_id="subtitle-run",
            video_part_id=part_id,
            source_kind="subtitle-ai",
            language="ai-zh",
            segments=(TranscriptSegmentRecord(start_ms=0, end_ms=1000, text="测试字幕。"),),
            started_at=1,
            finished_at=2,
            created_at=2,
        )
        transcript_repo.finish_acquisition_run("subtitle-run", 2)
        workflow = WorkflowRepository(connection)
        workflow.request_publication(video_part_id=part_id, transcript_id=stored.transcript_id)
        job = workflow.claim("publisher")
        assert job is not None and job.kind.value == "publish"
        handlers = ArchiveWorkflowHandlers(
            connection, workflow, archive_root=tmp_path / "archive", sessdata=None
        )
        try:
            result = handlers.publish(job)
        finally:
            handlers.close()
        workflow.finish(job.job_id, worker_id="publisher", result=result)
        assert (tmp_path / "archive" / result["srt_path"]).is_file()
        assert (tmp_path / "archive" / result["txt_path"]).read_text(encoding="utf-8") == "测试字幕。\n"
        publication = connection.execute(
            "SELECT transcript_id FROM workflow_publications WHERE video_part_id = ?", (part_id,)
        ).fetchone()
        assert publication is not None and publication["transcript_id"] == stored.transcript_id
    finally:
        connection.close()


def test_workflow_only_archive_is_visible_to_read_projections(tmp_path) -> None:
    connection = open_database(tmp_path)
    try:
        part_id = _seed_part(connection)
        transcript_repo = TranscriptRepository(connection)
        transcript_repo.start_acquisition_run(
            AcquisitionRunRecord(
                run_id="projection-run",
                kind="subtitle",
                selector_kind="bvid",
                selector_target="BVtest",
                requested_limit=1,
                credential_present=False,
                started_at=1,
            )
        )
        stored = transcript_repo.record_acquired_transcript(
            run_id="projection-run",
            video_part_id=part_id,
            source_kind="subtitle-ai",
            language="zh-CN",
            segments=(TranscriptSegmentRecord(start_ms=0, end_ms=1000, text="投影"),),
            started_at=1,
            finished_at=2,
            created_at=2,
        )
        transcript_repo.finish_acquisition_run("projection-run", 2)
        workflow = WorkflowRepository(connection)
        workflow.request_publication(video_part_id=part_id, transcript_id=stored.transcript_id)
        job = workflow.claim("projection-worker")
        assert job is not None
        handlers = ArchiveWorkflowHandlers(connection, workflow, archive_root=tmp_path, sessdata=None)
        try:
            result = handlers.publish(job)
        finally:
            handlers.close()
        workflow.finish(job.job_id, worker_id="projection-worker", result=result,
                        expected_attempt_count=job.attempt_count)
    finally:
        connection.close()

    assert not (tmp_path / "manifest" / "manifest.jsonl").exists()
    assert workflow_records(tmp_path, with_text=True)["BVtest:p0"]["status"] == "archived"
    report = CoverageReport.build(tmp_path).data
    assert report["denominator"] == {
        "unit": "work_items", "count": 1, "state": "available",
        "source": "workflow_database",
    }
    assert json.loads(export_records(tmp_path, "json", with_text=True, private=True))[0]["transcript_text"] == "投影"
    assert IntegrityVerifier().verify(tmp_path).authoritative is True


def test_read_projections_do_not_create_a_missing_database(tmp_path) -> None:
    assert CoverageReport.build(tmp_path).data["denominator"]["state"] == "unavailable"
    assert IntegrityVerifier().verify(tmp_path).authoritative is False
    assert export_records(tmp_path, "json") == "[]"
    assert not (tmp_path / "archive.db").exists()


def test_new_publication_request_while_a_publish_job_is_running_requeues_it(tmp_path) -> None:
    connection = open_database(tmp_path)
    try:
        part_id = _seed_part(connection)
        workflow = WorkflowRepository(connection)
        first_job_id, created = workflow.request_publication(video_part_id=part_id, transcript_id=10)
        assert created
        running = workflow.claim("publisher")
        assert running is not None and running.job_id == first_job_id
        same_job_id, created = workflow.request_publication(video_part_id=part_id, transcript_id=11)
        assert same_job_id == first_job_id and not created
        workflow.finish(
            running.job_id,
            worker_id="publisher",
            result={"requested_transcript_id": 10, "transcript_id": 10},
        )
        retried = workflow.claim("publisher")
        assert retried is not None and retried.job_id == first_job_id
        assert retried.payload["transcript_id"] == 11
    finally:
        connection.close()


def test_failed_jobs_can_be_requeued_without_erasing_their_attempt_history(tmp_path) -> None:
    connection = open_database(tmp_path)
    try:
        part_id = _seed_part(connection)
        workflow = WorkflowRepository(connection)
        workflow.plan(part_ids=[part_id], policy=AsrPolicy.ALL, profile_id=_profile(workflow))
        job = workflow.claim("worker")
        assert job is not None
        workflow.fail(job.job_id, worker_id="worker", error_code="temporary_error")
        assert workflow.requeue_failed(part_ids=[part_id]) == 1
        retried = workflow.claim("worker")
        assert retried is not None and retried.job_id == job.job_id
        attempts = connection.execute(
            "SELECT outcome FROM workflow_attempts WHERE job_id = ? ORDER BY started_at, rowid",
            (job.job_id,),
        ).fetchall()
        assert [row["outcome"] for row in attempts] == ["failed", "running"]
    finally:
        connection.close()


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe required")
@pytest.mark.parametrize("valid_audio", [True, False])
def test_audio_handler_measures_downloaded_media_before_recording_it(tmp_path, monkeypatch, valid_audio) -> None:
    from bili_asr import audio

    def download(_client, _identity, target, *, artifact_roots=None):
        assert artifact_roots is not None
        assert target.is_relative_to(artifact_roots.write_base)
        target.parent.mkdir(parents=True, exist_ok=True)
        if valid_audio:
            with wave.open(str(target), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\x00\x00" * 20000)
        else:
            target.write_bytes(b"invalid media")
        return target

    monkeypatch.setattr(audio, "download_audio", download)
    connection = open_database(tmp_path)
    try:
        part_id = _seed_part(connection)
        repo = WorkflowRepository(connection)
        repo.plan(part_ids=[part_id], policy=AsrPolicy.ALL, profile_id=_profile(repo))
        jobs = [repo.claim("worker"), repo.claim("worker")]
        job = next(job for job in jobs if job is not None and job.kind.value == "audio")
        runtime = ArchiveWorkflowHandlers(connection, repo, archive_root=tmp_path / "archive", sessdata=None)
        try:
            if valid_audio:
                result = runtime.audio(job)
                row = connection.execute("SELECT duration_ms, byte_size FROM audio_objects").fetchone()
                assert row["duration_ms"] == result["duration_ms"] == 1250
                assert row["byte_size"] > 40000
                assert connection.execute("SELECT COUNT(*) FROM part_audio_objects").fetchone()[0] == 1
            else:
                with pytest.raises(subprocess.CalledProcessError):
                    runtime.audio(job)
                assert connection.execute("SELECT COUNT(*) FROM audio_objects").fetchone()[0] == 0
        finally:
            runtime.close()
    finally:
        connection.close()
