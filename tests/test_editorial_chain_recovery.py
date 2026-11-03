"""Offline recovery of consumers preserves the failed producer and its DAG."""

import json

import pytest

from bili_asr import cli
from bili_asr.editorial import EditorialConfig
from bili_asr.services.editorial_chain_recovery import recover_editorial_chain
from bili_asr.workflow_models import AsrPolicy, AsrProfile, JobKind
from tests import test_ai_editorial as fixtures
from tests.test_ai_editorial import FakeClient, runtime

database = fixtures.database


def chain(database, tmp_path, *, config=None):
    client = FakeClient()
    workflow, editorial, handlers, executor = runtime(database, tmp_path, client)
    workflow.plan_with_profile(part_ids=[1], policy=AsrPolicy.ALL,
        profile=AsrProfile(profile_key="broken", model_name="old-model"),
        editorial_config=(config or EditorialConfig()).to_dict())
    for _ in range(2):
        job = workflow.claim("fixture", kinds=(JobKind.AUDIO, JobKind.SUBTITLE))
        workflow.finish(job.job_id, worker_id="fixture", result={})
    original = workflow.claim("fixture", kinds=(JobKind.ASR,))
    workflow.fail(original.job_id, worker_id="fixture", error_code="inference_timeout")
    proof = next(job for job in workflow.list_jobs() if job.kind is JobKind.PROOFREAD)
    render = next(job for job in workflow.list_jobs() if job.kind is JobKind.RENDER_DOCUMENT)
    with workflow.commit_guard.transaction():
        replacement = workflow.enqueue_asr_repair(repair_of_job_id=original.job_id,
            video_part_id=1, reference_transcript_id=None,
            profile=AsrProfile(profile_key="fixed", model_name="new-model"))["job_id"]
    selected = workflow.claim("fixture", kinds=(JobKind.ASR,))
    assert selected.job_id == replacement
    workflow.finish(replacement, worker_id="fixture", result={"transcript_id": 1, "run_id": "editorial-fixture-1"})
    return workflow, editorial, executor, client, original.job_id, proof.job_id, render.job_id, replacement


def snapshot(database):
    return {table: [tuple(row) for row in database.execute("SELECT * FROM " + table + " ORDER BY rowid")]
            for table in ("workflow_jobs", "workflow_attempts", "workflow_job_dependencies", "editorial_inputs")}


def inspect(workflow, proof, replacement, **kwargs):
    return recover_editorial_chain(workflow, proofread_job_id=proof, replacement_asr_job_id=replacement,
                                   no_control_state=True, **kwargs)


def test_recovery_enqueue_requires_application_transaction_before_changing_chain(database, tmp_path):
    workflow, _, _, client, _, proof, render, replacement = chain(database, tmp_path)
    plan = inspect(workflow, proof, replacement)
    before = snapshot(database)
    assert not database.in_transaction
    with pytest.raises(RuntimeError, match="editorial recovery enqueue requires a write transaction"):
        workflow.enqueue_editorial_recovery(video_part_id=plan["video_part_id"], input_id=plan["input_id"],
            previous_proofread_job_id=proof, previous_render_job_id=render,
            replacement_asr_job_id=replacement, replacement_attempt_id=plan["replacement_attempt_id"],
            replacement_run_id=plan["replacement_run_id"])
    assert not database.in_transaction
    assert snapshot(database) == before and client.requests == []
    applied = inspect(workflow, proof, replacement, apply=True, expected_plan_id=plan["plan_id"])
    assert applied["applied"] and applied["created"] and client.requests == []


def test_recovery_plans_readonly_and_creates_an_explicit_frozen_chain(database, tmp_path):
    workflow, editorial, executor, client, original, proof, render, replacement = chain(database, tmp_path)
    before = snapshot(database)
    plan = inspect(workflow, proof, replacement)
    assert plan["producer_recovered"] is True and plan["delivery_recovered"] is False
    assert plan["allowed"] and plan["budget"]["provider_calls_started"] == 0
    assert snapshot(database) == before and client.requests == []
    applied = inspect(workflow, proof, replacement, apply=True, expected_plan_id=plan["plan_id"])
    assert applied["applied"] and applied["created"] and client.requests == []
    old_jobs = {row[0]: row for row in before["workflow_jobs"]}
    for job_id in (original, proof, render):
        row = database.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (job_id,)).fetchone()
        assert row["payload_json"] == old_jobs[job_id][row.keys().index("payload_json")]
    assert [tuple(row) for row in database.execute("SELECT * FROM workflow_attempts ORDER BY rowid")] == before["workflow_attempts"]
    assert all(row in [tuple(r) for r in database.execute("SELECT * FROM workflow_job_dependencies")]
               for row in before["workflow_job_dependencies"])
    assert workflow.explain_job(original)["status"] == "failed"
    assert workflow.explain_job(proof)["status"] == workflow.explain_job(render)["status"] == "cancelled"
    new = workflow.explain_job(applied["job_id"])
    assert new["prerequisites"][0]["job_id"] == replacement
    assert editorial.load_input(applied["input_id"])["snapshot"]["config"] == EditorialConfig().to_dict()
    after = snapshot(database)
    again = inspect(workflow, proof, replacement, apply=True, expected_plan_id=plan["plan_id"])
    assert again["already_applied"] and not again["applied"] and snapshot(database) == after
    outcome = executor.run()
    assert outcome.succeeded == 2 and outcome.failed == 0 and len(client.requests) == 1
    result = json.loads(database.execute("SELECT result_json FROM workflow_attempts WHERE job_id=?",
                                       (applied["job_id"],)).fetchone()[0])
    assert result["source_selection"] == {"format": "editorial-source-selection/v1", "asr_job_id": replacement,
        "attempt_id": plan["replacement_attempt_id"], "run_id": "editorial-fixture-1", "transcript_id": 1}
    assert inspect(workflow, proof, replacement)["delivery_recovered"] is True


@pytest.mark.parametrize("control", ["unknown", "held"])
def test_recovery_respects_external_controls(database, tmp_path, control):
    workflow, _, _, client, _, proof, render, replacement = chain(database, tmp_path)
    arguments = {}
    if control == "held":
        path = tmp_path / "controls.json"
        path.write_text(json.dumps({"retry_holds": {render: "manual inspection"}, "retry_ledger": {}}), encoding="utf-8")
        arguments["control_state_path"] = path
    before = snapshot(database)
    plan = recover_editorial_chain(workflow, proofread_job_id=proof, replacement_asr_job_id=replacement, **arguments)
    assert plan["allowed"] is False
    with pytest.raises(ValueError, match="blocked"):
        recover_editorial_chain(workflow, proofread_job_id=proof, replacement_asr_job_id=replacement,
            apply=True, expected_plan_id=plan["plan_id"], **arguments)
    assert snapshot(database) == before and client.requests == []


def test_recovery_rejects_a_stale_plan_and_rolls_back_partial_writes(database, tmp_path, monkeypatch):
    workflow, _, _, _, _, proof, _, replacement = chain(database, tmp_path)
    plan = inspect(workflow, proof, replacement)
    with database:
        database.execute("UPDATE videos SET title='changed source metadata'")
    before = snapshot(database)
    with pytest.raises(ValueError, match="plan changed"):
        inspect(workflow, proof, replacement, apply=True, expected_plan_id=plan["plan_id"])
    assert snapshot(database) == before
    current = inspect(workflow, proof, replacement)
    enqueue = workflow.enqueue_editorial_recovery

    def interrupted(**kwargs):
        enqueue(**kwargs)
        raise RuntimeError("simulated interruption")

    monkeypatch.setattr(workflow, "enqueue_editorial_recovery", interrupted)
    with pytest.raises(RuntimeError, match="interruption"):
        inspect(workflow, proof, replacement, apply=True, expected_plan_id=current["plan_id"])
    assert snapshot(database) == before


@pytest.mark.parametrize("status", ["running", "failed", "succeeded", "cancelled"])
def test_recovery_does_not_cancel_attempted_or_running_consumers(database, tmp_path, status):
    workflow, _, _, _, _, proof, _, replacement = chain(database, tmp_path)
    with database:
        database.execute("UPDATE workflow_jobs SET status=?,attempt_count=1 WHERE job_id=?", (status, proof))
    before = snapshot(database)
    with pytest.raises(ValueError, match="never-attempted"):
        inspect(workflow, proof, replacement)
    assert snapshot(database) == before


@pytest.mark.parametrize("change", ["attempt_result", "source_text"])
def test_recovery_revalidates_exact_producer_before_provider_call(database, tmp_path, change):
    workflow, _, executor, client, _, proof, _, replacement = chain(database, tmp_path)
    plan = inspect(workflow, proof, replacement)
    applied = inspect(workflow, proof, replacement, apply=True, expected_plan_id=plan["plan_id"])
    with database:
        if change == "attempt_result":
            database.execute("UPDATE workflow_attempts SET result_json=? WHERE attempt_id=?",
                (json.dumps({"transcript_id": 1, "run_id": "changed-run"}), plan["replacement_attempt_id"]))
        else:
            database.execute("UPDATE transcript_segments SET text='changed after freezing' WHERE transcript_id=1")
    assert executor.run().failed == 1
    assert client.requests == []
    assert workflow.explain_job(applied["job_id"])["status"] == "failed"


def test_legacy_recovery_reports_unknown_physical_budget_without_silently_upgrading_policy(database, tmp_path):
    legacy = EditorialConfig.from_snapshot({})
    workflow, editorial, _, client, _, proof, _, replacement = chain(database, tmp_path, config=legacy)
    plan = inspect(workflow, proof, replacement)
    assert plan["budget"]["physical_budget_enforced"] is False
    assert plan["budget"]["physical_calls_max"] is None
    assert plan["execution"]["provider_execution_started"] is False
    applied = inspect(workflow, proof, replacement, apply=True, expected_plan_id=plan["plan_id"])
    assert editorial.load_input(applied["input_id"])["snapshot"]["config"] == legacy.to_dict()
    assert client.requests == []


def test_recovery_cli_requires_inspected_identity_and_never_starts_ai(database, tmp_path, capsys):
    workflow, _, _, client, _, proof, _, replacement = chain(database, tmp_path)
    args = ["workflow", "recover-editorial", "--archive-root", str(tmp_path),
            "--proofread-job-id", proof, "--replacement-asr-job-id", replacement, "--no-control-state"]
    before = snapshot(database)
    assert cli.main(args) == 0
    plan = json.loads(capsys.readouterr().out)
    assert snapshot(database) == before
    assert cli.main(args + ["--apply"]) == 1
    assert "expected-plan-id" in capsys.readouterr().err
    assert cli.main(args + ["--apply", "--expected-plan-id", plan["plan_id"]]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["applied"] and client.requests == []
    assert workflow.explain_job(result["job_id"])["status"] == "queued"
