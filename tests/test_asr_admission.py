"""Issue #410: inference evidence survives failed admission and explicit repair."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from bili_asr import cli
from bili_asr.editorial import EditorialConfig
from bili_asr.editorial_runtime import EditorialWorkflowHandlers
from bili_asr.services.asr_repair import repair_asr
from bili_asr.storage import (
    AsrPolicy,
    AsrProfile,
    JobKind,
    TranscriptRepository,
    WorkflowRepository,
    open_database,
)
from bili_asr.storage.asr_quality import assess_transcript, read_assessment
from bili_asr.storage.editorial import EditorialRepository
from bili_asr.workflow import WorkflowExecutor
from bili_asr.workflow_failure_policy import failure_policy
from bili_asr.workflow_runtime import ArchiveWorkflowHandlers
from tests.support.asr_quality import coverage, cue
from tests.test_ai_editorial import FakeClient, insert_record, record
from tests.test_workflow_control_plane import _seed_part


@pytest.fixture
def runtime(tmp_path):
    connection = open_database(tmp_path)
    _seed_part(connection)
    with connection:
        connection.execute("UPDATE video_parts SET duration_ms=4000")
    workflow = WorkflowRepository(connection)
    profile = AsrProfile("bad-aligner", "offline-model", model_revision="model-commit", aligner_revision="bad-commit")
    profile_id = workflow.register_profile(profile)
    workflow.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=profile_id,
                  editorial_config=EditorialConfig().to_dict())
    audio_path = tmp_path / "audio" / "original.m4a"
    audio_path.parent.mkdir()
    audio_path.write_bytes(b"synthetic audio with substituted decoder")
    from bili_asr.storage.workflow_outputs import record_audio
    with connection:
        connection.execute("BEGIN")
        record_audio(connection, part_id=1, sha256=hashlib.sha256(audio_path.read_bytes()).hexdigest(),
                     byte_size=audio_path.stat().st_size, media_format="m4a", duration_ms=4000,
                     storage_key="audio/original.m4a", target_id="local")
    yield connection, workflow, profile, tmp_path
    connection.close()


def executor(runtime, cues, evidence):
    connection, workflow, _, root = runtime
    def infer(config, path, *, diagnostics_sink, **kwargs):
        diagnostics_sink.update({"quality": {"status": "no-known-risk", "flags": []}})
        return cues, {"language": "Chinese", "model": config.model_name,
                      "aligner_revision": config.aligner_revision}, evidence
    handlers = ArchiveWorkflowHandlers(connection, workflow, archive_root=root, sessdata=None, timeout_transcriber=infer)
    return handlers, WorkflowExecutor(workflow, worker_id="inference", handlers=handlers.handlers(), kinds=(JobKind.ASR,))


@pytest.mark.parametrize("cues,evidence,represented", [
    ([cue(2, 3), cue(1, 2)], coverage(4, 2), True),
    ([cue(0, float("inf"))], coverage(), False),
    ([cue(1, 1)], coverage(), False),
    ([], coverage(4, 0), False),
])
def test_failed_admission_preserves_original_inference_and_blocks_downstream(runtime, cues, evidence, represented):
    connection, workflow, _, root = runtime
    handlers, worker = executor(runtime, cues, evidence)
    try:
        assert worker.run().failed == 1
    finally:
        handlers.close()
    asr_job = next(job for job in workflow.list_jobs() if job.kind is JobKind.ASR)
    assert workflow.explain_job(asr_job.job_id)["error_code"] == "asr_quality_unusable"
    run = connection.execute("SELECT * FROM acquisition_runs WHERE kind='asr'").fetchone()
    assert run["outcome"] == "complete"
    report = read_assessment(connection, run_id=run["run_id"], video_part_id=1, include_evidence=True)
    assert report["status"] == "unusable" and report["inference_completed"]
    assert report["evidence"]["asr_evidence"]["audio"]["sha256"]
    assert report["evidence"]["asr_evidence"]["provenance"]["aligner_revision"] == "bad-commit"
    if represented:
        rows = list(connection.execute("SELECT start_ms,end_ms FROM transcript_segments ORDER BY ordinal"))
        assert [row["start_ms"] for row in rows] == [2000, 1000]
        assert TranscriptRepository(connection).read_asr_evidence(run["run_id"], 1)["admission"]["status"] == "unusable"
    else:
        assert connection.execute("SELECT count(*) FROM transcript_segments").fetchone()[0] == 0
        assert report["evidence"]["original_segments"] != [] or cues == []
        if cues and cues[0]["end"] == float("inf"):
            assert report["evidence"]["original_segments"][0]["end"] == {"nonfinite_number": "+inf"}
    assert workflow.claim("paid", kinds=(JobKind.PROOFREAD,)) is None
    assert connection.execute("SELECT count(*) FROM workflow_jobs WHERE kind='publish'").fetchone()[0] == 0
    assert not failure_policy("asr_quality_unusable").automatic_retry
    before = (root / "archive.db").read_bytes()
    assert cli.main(["workflow", "asr-quality", "--archive-root", str(root), "--run-id", run["run_id"], "--part-id", "1"]) == 0
    assert (root / "archive.db").read_bytes() == before


def test_explicit_frozen_bad_transcript_is_rechecked_before_any_paid_call(runtime):
    connection, workflow, _, root = runtime
    handlers, worker = executor(runtime, [cue(2, 3), cue(1, 2)], coverage(4, 2))
    try:
        worker.run()
    finally:
        handlers.close()
    editorial = EditorialRepository(connection)
    prepared = editorial.prepare(1, None, EditorialConfig())
    proof, *_ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    fake = FakeClient()
    paid = EditorialWorkflowHandlers(editorial, workflow, archive_root=root, client=fake)
    assert WorkflowExecutor(workflow, worker_id="paid", handlers=paid.handlers(), kinds=(JobKind.PROOFREAD,)).run().failed == 1
    assert fake.requests == [] and connection.execute("SELECT count(*) FROM editorial_model_calls").fetchone()[0] == 0
    assert workflow.explain_job(proof)["error_code"] == "asr_quality_unusable"
    publication, _ = workflow.request_publication(video_part_id=1, transcript_id=1)
    publisher = ArchiveWorkflowHandlers(connection, workflow, archive_root=root, sessdata=None)
    try:
        assert WorkflowExecutor(workflow, worker_id="publication", handlers=publisher.handlers(), kinds=(JobKind.PUBLISH,)).run().failed == 1
    finally:
        publisher.close()
    assert workflow.explain_job(publication)["error_code"] == "asr_quality_unusable"
    assert connection.execute("SELECT count(*) FROM workflow_publications").fetchone()[0] == 0


def test_repair_changes_version_and_keeps_failed_attempt_while_new_chain_succeeds(runtime):
    connection, workflow, profile, root = runtime
    handlers, worker = executor(runtime, [cue(2, 3), cue(1, 2)], coverage(4, 2))
    try:
        worker.run()
    finally:
        handlers.close()
    old_job = next(job for job in workflow.list_jobs() if job.kind is JobKind.ASR)
    before = [tuple(row) for row in connection.execute("SELECT * FROM workflow_attempts WHERE job_id=?", (old_job.job_id,))]
    with pytest.raises(ValueError, match="changed"):
        repair_asr(workflow, job_id=old_job.job_id, profile=profile, reason="original alignment checked")
    repaired = repair_asr(workflow, job_id=old_job.job_id, profile=replace(profile, aligner_revision="verified-commit"),
                          reason="original audio checked; aligner identity repaired")
    assert repaired["created"] and repaired["job_id"] != old_job.job_id
    repeated = repair_asr(workflow, job_id=old_job.job_id, profile=replace(profile, aligner_revision="verified-commit"),
                          reason="original audio checked; aligner identity repaired")
    assert not repeated["created"] and repeated["job_id"] == repaired["job_id"]
    assert connection.execute("SELECT count(*) FROM workflow_quality_assessments WHERE assessor='asr-admission/v1/repair'").fetchone()[0] == 1
    handlers, worker = executor(runtime, [cue()], coverage())
    try:
        assert worker.run().succeeded == 1
    finally:
        handlers.close()
    transcript_id = connection.execute("SELECT transcript_id FROM transcripts WHERE version=2").fetchone()[0]
    assert assess_transcript(connection, transcript_id)["status"] == "usable"
    assert workflow.explain_job(old_job.job_id)["status"] == "failed"
    assert before == [tuple(row) for row in connection.execute("SELECT * FROM workflow_attempts WHERE job_id=?", (old_job.job_id,))]
    fake = FakeClient()
    paid = EditorialWorkflowHandlers(EditorialRepository(connection), workflow, archive_root=root, client=fake)
    assert WorkflowExecutor(workflow, worker_id="paid", handlers=paid.handlers(), kinds=(JobKind.PROOFREAD,)).run().succeeded == 1
    assert len(fake.requests) == 1


def test_historical_missing_evidence_is_unknown_and_needs_fresh_explicit_inference(tmp_path):
    connection = open_database(tmp_path)
    try:
        _seed_part(connection)
        insert_record(connection, record(), measured=False)
        original = [tuple(row) for row in connection.execute("SELECT * FROM transcript_segments")]
        quality = assess_transcript(connection, 1)
        assert quality["status"] == "unknown" and quality["coverage"] == "unknown"
        fake = FakeClient()
        workflow = WorkflowRepository(connection)
        editorial = EditorialRepository(connection)
        prepared = editorial.prepare(1, None, EditorialConfig())
        workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
        paid = EditorialWorkflowHandlers(editorial, workflow, archive_root=tmp_path, client=fake)
        assert WorkflowExecutor(workflow, worker_id="paid", handlers=paid.handlers(), kinds=(JobKind.PROOFREAD,)).run().failed == 1
        assert fake.requests == []
        repaired = repair_asr(workflow, transcript_id=1, profile=AsrProfile("reverify", "offline-model"),
                              reason="historical decoded evidence missing; original audio will be re-evaluated")
        assert repaired["repair_of_transcript_id"] == 1 and repaired["created"]
        assert original == [tuple(row) for row in connection.execute("SELECT * FROM transcript_segments")]
    finally:
        connection.close()


def test_deduplicated_content_keeps_run_quality_and_explicit_run_cannot_borrow_new_evidence(runtime):
    connection, workflow, profile, _ = runtime
    handlers, worker = executor(runtime, [cue()], None)
    try:
        assert worker.run().failed == 1
    finally:
        handlers.close()
    old_job = next(job for job in workflow.list_jobs() if job.kind is JobKind.ASR)
    old_run = connection.execute("SELECT run_id FROM acquisition_runs WHERE kind='asr'").fetchone()[0]
    assert assess_transcript(connection, 1, run_id=old_run)["status"] == "unknown"
    repair_asr(workflow, job_id=old_job.job_id, profile=profile, reason="original audio verified; acquire missing decode measurement")
    handlers, worker = executor(runtime, [cue()], coverage())
    try:
        assert worker.run().succeeded == 1
    finally:
        handlers.close()
    assert connection.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM transcript_asr_evidence").fetchone()[0] == 2
    assert assess_transcript(connection, 1)["status"] == "usable"
    assert assess_transcript(connection, 1, run_id=old_run)["status"] == "unknown"


def test_assessment_failure_rolls_back_every_result_under_same_lease(runtime):
    connection, _, _, _ = runtime
    connection.execute("""CREATE TEMP TRIGGER reject_admission BEFORE INSERT ON workflow_quality_assessments
        BEGIN SELECT RAISE(ABORT,'assessment rejected'); END""")
    handlers, worker = executor(runtime, [cue()], coverage())
    try:
        assert worker.run().failed == 1
    finally:
        handlers.close()
    assert connection.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM acquisition_attempts").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM transcript_asr_evidence").fetchone()[0] == 0
    assert connection.execute("SELECT outcome FROM acquisition_runs WHERE kind='asr'").fetchone()[0] == "failed"


def test_explicit_repair_cli_freezes_profile_and_queries_without_exposing_text(runtime, capsys):
    _, workflow, _, root = runtime
    handlers, worker = executor(runtime, [cue(2, 3), cue(1, 2)], coverage(4, 2))
    try:
        worker.run()
    finally:
        handlers.close()
    job = next(job for job in workflow.list_jobs() if job.kind is JobKind.ASR)
    config_path = root / "repair-profile.json"
    config_path.write_text(json.dumps({"profile_key": "repair", "model_name": "offline-model",
                                      "aligner_revision": "verified-commit"}), encoding="utf-8")
    assert cli.main(["workflow", "repair-asr", "--archive-root", str(root), "--job-id", job.job_id,
                     "--profile-config", str(config_path), "--reason", "alignment cause verified"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["created"] and result["repair_of_job_id"] == job.job_id
    assert workflow.profile(result["profile_id"]).aligner_revision == "verified-commit"
    assert cli.main(["workflow", "asr-quality", "--archive-root", str(root), "--transcript-id", "1"]) == 0
    assert "正常完整转录" not in capsys.readouterr().out
    assert cli.main(["workflow", "asr-quality", "--archive-root", str(root), "--run-id", "absent", "--part-id", "1"]) == 1
    assert "no admission evidence" in capsys.readouterr().err
    assert cli.main(["workflow", "asr-quality", "--archive-root", str(root), "--transcript-id", "1", "--include-evidence"]) == 1
    assert "requires an exact" in capsys.readouterr().err


@pytest.mark.parametrize("body", [[], {"schema_version": 2}, {"schema_version": True},
                                 {"schema_version": 1, "audio": None}])
def test_unavailable_historical_evidence_stays_unknown_instead_of_traceback(tmp_path, body):
    connection = open_database(tmp_path)
    try:
        _seed_part(connection)
        insert_record(connection, record())
        with connection:
            connection.execute("UPDATE transcript_asr_evidence SET evidence_json=?", (json.dumps(body),))
        quality = assess_transcript(connection, 1)
        if isinstance(body, dict) and body.get("schema_version") == 1 and body.get("schema_version") is not True:
            assert quality["evidence_status"] == "persisted"
            assert quality["duration_ms"] == 1900
        else:
            assert quality["status"] == "unknown" and quality["evidence_status"] == "unavailable"
    finally:
        connection.close()


def test_legacy_automatic_chain_without_run_identity_cannot_borrow_healthy_evidence(runtime):
    connection, workflow, _, root = runtime
    handlers, worker = executor(runtime, [cue()], coverage())
    try:
        assert worker.run().succeeded == 1
    finally:
        handlers.close()
    with connection:
        connection.execute("UPDATE workflow_attempts SET result_json=json_remove(result_json,'$.run_id') WHERE outcome='succeeded' AND job_id IN (SELECT job_id FROM workflow_jobs WHERE kind='asr')")
    fake = FakeClient()
    paid = EditorialWorkflowHandlers(EditorialRepository(connection), workflow, archive_root=root, client=fake)
    assert WorkflowExecutor(workflow, worker_id="paid", handlers=paid.handlers(), kinds=(JobKind.PROOFREAD,)).run().failed == 1
    assert fake.requests == []
    attempt = connection.execute("SELECT result_json FROM workflow_attempts WHERE outcome='failed'").fetchone()[0]
    assert json.loads(attempt)["diagnostic"]["reason_code"] == "asr_prerequisite_run_unknown"


@pytest.mark.parametrize("reason", ["", " ", "x" * 513, "multiline\nreason", None])
def test_repair_rejects_invalid_reason_without_creating_jobs(runtime, reason):
    connection, workflow, profile, _ = runtime
    before = connection.execute("SELECT count(*) FROM workflow_jobs").fetchone()[0]
    with pytest.raises(ValueError, match="reason"):
        repair_asr(workflow, job_id="absent", profile=profile, reason=reason)
    assert connection.execute("SELECT count(*) FROM workflow_jobs").fetchone()[0] == before


def test_repair_rejects_ambiguous_missing_and_nonterminal_origins(runtime):
    connection, workflow, profile, _ = runtime
    for kwargs in ({}, {"job_id": "absent", "transcript_id": 1}):
        with pytest.raises(ValueError, match="exactly one"):
            repair_asr(workflow, profile=profile, reason="audio verified", **kwargs)
    for job_id in ("absent", next(job.job_id for job in workflow.list_jobs() if job.kind is JobKind.ASR)):
        with pytest.raises(ValueError, match="terminal"):
            repair_asr(workflow, job_id=job_id, profile=profile, reason="audio verified")
    with pytest.raises(ValueError, match="existing ASR"):
        assess_transcript(connection, 999)


def test_repair_of_already_usable_output_is_refused(runtime):
    _, workflow, profile, _ = runtime
    handlers, worker = executor(runtime, [cue()], coverage())
    try:
        assert worker.run().succeeded == 1
    finally:
        handlers.close()
    job = next(job for job in workflow.list_jobs() if job.kind is JobKind.ASR)
    for kwargs in ({"job_id": job.job_id}, {"transcript_id": 1}):
        with pytest.raises(ValueError, match="already usable"):
            repair_asr(workflow, profile=profile, reason="audio verified", **kwargs)


def test_assessment_and_repair_enqueue_require_fenced_transactions(runtime):
    connection, workflow, profile, _ = runtime
    from bili_asr.storage.asr_quality import record_assessment
    with pytest.raises(RuntimeError, match="fenced"):
        record_assessment(connection, video_part_id=1, details={})
    with pytest.raises(RuntimeError, match="write transaction"):
        workflow.enqueue_asr_repair(repair_of_job_id="absent", video_part_id=1, profile=profile, reference_transcript_id=None)


def test_transcript_reverification_preserves_explicit_lineage_in_new_result(runtime):
    connection, workflow, profile, _ = runtime
    handlers, worker = executor(runtime, [cue()], None)
    try:
        assert worker.run().failed == 1
    finally:
        handlers.close()
    repaired = repair_asr(workflow, transcript_id=1, profile=profile, reason="original audio decoded evidence reverified")
    handlers, worker = executor(runtime, [cue()], coverage())
    try:
        assert worker.run().succeeded == 1
    finally:
        handlers.close()
    run_id = json.loads(connection.execute(
        "SELECT result_json FROM workflow_attempts WHERE job_id=?", (repaired["job_id"],)).fetchone()[0])["run_id"]
    result = read_assessment(connection, run_id=run_id, video_part_id=1, include_evidence=True)
    assert result["status"] == "usable" and result["evidence"]["repair_of_transcript_id"] == 1


def test_legacy_terminal_job_uses_its_recorded_transcript_for_explicit_repair(runtime):
    connection, workflow, profile, _ = runtime
    handlers, worker = executor(runtime, [cue(2, 3), cue(1, 2)], coverage(4, 2))
    try:
        assert worker.run().failed == 1
    finally:
        handlers.close()
    job = next(job for job in workflow.list_jobs() if job.kind is JobKind.ASR)
    with connection:
        connection.execute("UPDATE workflow_attempts SET result_json=? WHERE job_id=?",
                           (json.dumps({"transcript_id": 1}), job.job_id))
    replacement = repair_asr(workflow, job_id=job.job_id, profile=replace(profile, aligner_revision="verified"),
                             reason="legacy producing transcript retained; alignment corrected")
    assert replacement["created"] and replacement["repair_of_job_id"] == job.job_id


def test_terminal_failure_without_inference_evidence_cannot_be_quality_repaired(runtime):
    _, workflow, profile, _ = runtime
    job = workflow.claim("failed-before-inference", kinds=(JobKind.ASR,))
    workflow.fail(job.job_id, worker_id="failed-before-inference", error_code="fixture_decoder_failure")
    with pytest.raises(ValueError, match="preserved completed inference"):
        repair_asr(workflow, job_id=job.job_id, profile=profile, reason="audio verified")


def test_quality_repair_requires_unambiguous_automatic_editorial_configuration(runtime):
    connection, workflow, profile, _ = runtime
    handlers, worker = executor(runtime, [cue(2, 3), cue(1, 2)], coverage(4, 2))
    try:
        assert worker.run().failed == 1
    finally:
        handlers.close()
    job = next(job for job in workflow.list_jobs() if job.kind is JobKind.ASR)
    profile_id = workflow.register_profile(profile)
    workflow.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=profile_id,
                  editorial_config=EditorialConfig(max_output_tokens=2100).to_dict())
    before = connection.execute("SELECT count(*) FROM workflow_jobs").fetchone()[0]
    with pytest.raises(ValueError, match="ambiguous editorial configuration"):
        repair_asr(workflow, job_id=job.job_id, profile=replace(profile, aligner_revision="verified"),
                   reason="audio and alignment verified")
    assert connection.execute("SELECT count(*) FROM workflow_jobs").fetchone()[0] == before


@pytest.mark.parametrize("values,message", [
    ([], "JSON object"),
    ({"profile_key": True, "model_name": "offline"}, "nonempty strings"),
    ({"profile_key": "repair", "model_name": "offline", "hotwords": "word"}, "JSON array"),
    ({"profile_key": "repair", "model_name": "offline", "unsupported": 1}, "unsupported fields"),
])
def test_repair_cli_rejects_invalid_profile_without_new_jobs(runtime, capsys, values, message):
    connection, _, _, root = runtime
    config_path = root / "invalid-profile.json"
    config_path.write_text(json.dumps(values), encoding="utf-8")
    before = connection.execute("SELECT count(*) FROM workflow_jobs").fetchone()[0]
    assert cli.main(["workflow", "repair-asr", "--archive-root", str(root), "--job-id", "absent",
                     "--profile-config", str(config_path), "--reason", "audio verified"]) == 1
    assert message in capsys.readouterr().err
    assert connection.execute("SELECT count(*) FROM workflow_jobs").fetchone()[0] == before
