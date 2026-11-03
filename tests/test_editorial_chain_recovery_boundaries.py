"""Recovery refuses ambiguous history and rolls back changes before queuing AI."""

import json

import pytest

from bili_asr.editorial import EditorialConfig
from bili_asr.services.editorial_chain_recovery import recover_editorial_chain
from bili_asr.workflow_models import JobKind
from tests import test_editorial_chain_recovery as fixtures

database = fixtures.database


def _payload(connection, job_id):
    return json.loads(connection.execute(
        "SELECT payload_json FROM workflow_jobs WHERE job_id=?", (job_id,)).fetchone()[0])


def _write_payload(connection, job_id, payload):
    connection.execute("UPDATE workflow_jobs SET payload_json=? WHERE job_id=?", (json.dumps(payload), job_id))


def _other_part(connection):
    connection.execute(
        "INSERT INTO video_parts(video_part_id,bvid,page_index,cid,title,duration_ms,processing_status,created_at,updated_at) "
        "SELECT 2,bvid,1,2,'another part',duration_ms,processing_status,created_at,updated_at "
        "FROM video_parts WHERE video_part_id=1")


def _copy_job(connection, job_id, new_id):
    row = dict(connection.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (job_id,)).fetchone())
    row.update(job_id=new_id, dedupe_key="boundary-fixture:" + new_id)
    connection.execute(
        "INSERT INTO workflow_jobs (" + ",".join(row) + ") VALUES (" + ",".join("?" for _ in row) + ")",
        tuple(row.values()))


def _rejected_without_writes(workflow, client, proof, replacement, message):
    before = fixtures.snapshot(workflow.connection)
    with pytest.raises(ValueError, match=message):
        fixtures.inspect(workflow, proof, replacement)
    assert fixtures.snapshot(workflow.connection) == before
    assert client.requests == []


def _record_recovery(workflow, editorial, proof, render, replacement):
    plan = fixtures.inspect(workflow, proof, replacement)
    prepared = editorial.build_input(1, None, EditorialConfig(), reuse_existing=True)
    with workflow.commit_guard.transaction():
        editorial.store_input(prepared)
        recovered = workflow.enqueue_editorial_recovery(
            video_part_id=1, input_id=plan["input_id"], previous_proofread_job_id=proof,
            previous_render_job_id=render, replacement_asr_job_id=replacement,
            replacement_attempt_id=plan["replacement_attempt_id"], replacement_run_id=plan["replacement_run_id"])
    return recovered[:2]


@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("consumer", ["proofread", "render"])
@pytest.mark.parametrize("damage", ["missing-edge", "extra-edge", "wrong-edge", "other-part"])
def test_recorded_recovery_must_have_exact_graph_and_part(database, tmp_path, retired, consumer, damage):
    workflow, editorial, _, client, original, proof, render, replacement = fixtures.chain(database, tmp_path)
    recovered_proof, recovered_render = _record_recovery(workflow, editorial, proof, render, replacement)
    plan = fixtures.inspect(workflow, proof, replacement)
    target = recovered_proof if consumer == "proofread" else recovered_render
    with database:
        if retired:
            database.execute("UPDATE workflow_jobs SET status='cancelled',last_error_code='superseded_by_recovery' "
                             "WHERE job_id IN (?,?)", (proof, render))
        if damage == "other-part":
            _other_part(database)
            database.execute("UPDATE workflow_jobs SET video_part_id=2 WHERE job_id=?", (target,))
        else:
            if damage != "extra-edge":
                database.execute("DELETE FROM workflow_job_dependencies WHERE job_id=?", (target,))
            if damage != "missing-edge":
                database.execute("INSERT INTO workflow_job_dependencies VALUES (?,?)", (target, original))
    _rejected_without_writes(workflow, client, proof, replacement, "recorded recovery graph")
    before = fixtures.snapshot(database)
    with pytest.raises(ValueError, match="recorded recovery graph"):
        fixtures.inspect(workflow, proof, replacement, apply=True, expected_plan_id=plan["plan_id"])
    assert fixtures.snapshot(database) == before and client.requests == []


@pytest.mark.parametrize("retired", [False, True])
def test_recorded_recovery_refuses_duplicate_render_identities(database, tmp_path, retired):
    workflow, editorial, _, client, _, proof, render, replacement = fixtures.chain(database, tmp_path)
    recovered_proof, recovered_render = _record_recovery(workflow, editorial, proof, render, replacement)
    with database:
        _copy_job(database, recovered_render, "duplicate-recovery-render")
        database.execute("INSERT INTO workflow_job_dependencies VALUES (?,?)", ("duplicate-recovery-render", recovered_proof))
        if retired:
            database.execute("UPDATE workflow_jobs SET status='cancelled',last_error_code='superseded_by_recovery' "
                             "WHERE job_id IN (?,?)", (proof, render))
    _rejected_without_writes(workflow, client, proof, replacement, "ambiguous recorded recovery render")


def test_succeeded_render_with_broken_dependency_cannot_claim_delivery_recovered(database, tmp_path):
    workflow, _, executor, client, _, proof, _, replacement = fixtures.chain(database, tmp_path)
    plan = fixtures.inspect(workflow, proof, replacement)
    applied = fixtures.inspect(workflow, proof, replacement, apply=True, expected_plan_id=plan["plan_id"])
    assert executor.run().succeeded == 2
    assert fixtures.inspect(workflow, proof, replacement)["delivery_recovered"]
    with database:
        database.execute("DELETE FROM workflow_job_dependencies WHERE job_id=?", (applied["render_job_id"],))
    before = fixtures.snapshot(database)
    requests = len(client.requests)
    with pytest.raises(ValueError, match="recorded recovery graph"):
        fixtures.inspect(workflow, proof, replacement)
    assert fixtures.snapshot(database) == before and len(client.requests) == requests


@pytest.mark.parametrize("target", ["proofread", "replacement"])
@pytest.mark.parametrize("invalid", ["missing", "wrong-kind"])
def test_recovery_requires_existing_jobs_of_the_requested_kind(database, tmp_path, target, invalid):
    workflow, _, _, client, _, proof, render, replacement = fixtures.chain(database, tmp_path)
    selected = "not-recorded" if invalid == "missing" else render
    if target == "proofread":
        proof = selected
    else:
        replacement = selected
    expected_kind = "proofread" if target == "proofread" else "asr"
    _rejected_without_writes(workflow, client, proof, replacement, "existing " + expected_kind + " job")


@pytest.mark.parametrize(("field", "value"), [("asr_job_id", None), ("editorial_config", [])])
def test_recovery_requires_the_original_frozen_dependency_and_policy(database, tmp_path, field, value):
    workflow, _, _, client, _, proof, _, replacement = fixtures.chain(database, tmp_path)
    with database:
        payload = _payload(database, proof)
        payload[field] = value
        _write_payload(database, proof, payload)
    _rejected_without_writes(workflow, client, proof, replacement, "original frozen ASR dependency and editorial policy")


@pytest.mark.parametrize("change", ["same-job", "old-not-failed", "replacement-not-succeeded", "old-part", "replacement-part"])
def test_recovery_requires_an_explicit_successful_replacement_for_the_same_part(database, tmp_path, change):
    workflow, _, _, client, original, proof, _, replacement = fixtures.chain(database, tmp_path)
    with database:
        if change == "same-job":
            replacement = original
        elif change in {"old-part", "replacement-part"}:
            _other_part(database)
            selected = original if change == "old-part" else replacement
            database.execute("UPDATE workflow_jobs SET video_part_id=2 WHERE job_id=?", (selected,))
        else:
            selected = original if change == "old-not-failed" else replacement
            database.execute("UPDATE workflow_jobs SET status='queued' WHERE job_id=?", (selected,))
    _rejected_without_writes(workflow, client, proof, replacement, "explicitly selected successful replacement for the same part")


@pytest.mark.parametrize("consumer", ["proofread", "render"])
def test_recovery_refuses_additional_prerequisites(database, tmp_path, consumer):
    workflow, _, _, client, original, proof, render, replacement = fixtures.chain(database, tmp_path)
    job, prerequisite = (proof, replacement) if consumer == "proofread" else (render, original)
    with database:
        database.execute("INSERT INTO workflow_job_dependencies VALUES (?,?)", (job, prerequisite))
    expected = "proofreading" if consumer == "proofread" else "render"
    _rejected_without_writes(workflow, client, proof, replacement, "original " + expected + " dependencies are ambiguous")


@pytest.mark.parametrize("change", ["missing", "duplicate", "wrong-kind", "other-part", "attempted", "payload"])
def test_recovery_requires_one_unambiguous_unattempted_render_consumer(database, tmp_path, change):
    workflow, _, _, client, _, proof, render, replacement = fixtures.chain(database, tmp_path)
    with database:
        if change == "missing":
            database.execute("DELETE FROM workflow_job_dependencies WHERE job_id=?", (render,))
        elif change == "duplicate":
            _copy_job(database, render, "second-render")
            database.execute("INSERT INTO workflow_job_dependencies VALUES (?,?)", ("second-render", proof))
        elif change == "wrong-kind":
            database.execute("UPDATE workflow_jobs SET kind='index' WHERE job_id=?", (render,))
        elif change == "other-part":
            _other_part(database)
            database.execute("UPDATE workflow_jobs SET video_part_id=2 WHERE job_id=?", (render,))
        elif change == "attempted":
            database.execute("UPDATE workflow_jobs SET status='running',attempt_count=1 WHERE job_id=?", (render,))
        else:
            payload = _payload(database, render)
            payload["proofread_job_id"] = "another-proofread"
            _write_payload(database, render, payload)
    _rejected_without_writes(workflow, client, proof, replacement, "one never-attempted render consumer")


@pytest.mark.parametrize("retired", ["proofread", "render"])
def test_recovery_does_not_complete_a_partially_retired_chain(database, tmp_path, retired):
    workflow, _, _, client, _, proof, render, replacement = fixtures.chain(database, tmp_path)
    with database:
        database.execute(
            "UPDATE workflow_jobs SET status='cancelled',last_error_code='superseded_by_recovery' WHERE job_id=?",
            (proof if retired == "proofread" else render,))
    _rejected_without_writes(workflow, client, proof, replacement, "partially retired chain")


@pytest.mark.parametrize("result", [
    pytest.param(None, id="null-result"),
    pytest.param([], id="non-object"),
    pytest.param({}, id="missing-identities"),
    pytest.param({"transcript_id": True, "run_id": "editorial-fixture-1"}, id="boolean-transcript"),
    pytest.param({"transcript_id": "1", "run_id": "editorial-fixture-1"}, id="string-transcript"),
    pytest.param({"transcript_id": 1, "run_id": 1}, id="non-string-run"),
    pytest.param({"transcript_id": 1, "run_id": ""}, id="empty-run"),
])
def test_recovery_requires_exact_typed_producer_result_identity(database, tmp_path, result):
    workflow, _, _, client, _, proof, _, replacement = fixtures.chain(database, tmp_path)
    with database:
        database.execute("UPDATE workflow_attempts SET result_json=? WHERE job_id=?",
                         (None if result is None else json.dumps(result), replacement))
    _rejected_without_writes(workflow, client, proof, replacement, "lacks exact transcript and run evidence")


def test_recovery_requires_a_recorded_successful_attempt(database, tmp_path):
    workflow, _, _, client, _, proof, _, replacement = fixtures.chain(database, tmp_path)
    with database:
        database.execute("DELETE FROM workflow_attempts WHERE job_id=?", (replacement,))
    _rejected_without_writes(workflow, client, proof, replacement, "lacks exact transcript and run evidence")


@pytest.mark.parametrize("change", ["other-part", "other-producer"])
def test_recovery_rejects_cross_part_or_non_asr_producer_output(database, tmp_path, change):
    workflow, _, _, client, _, proof, _, replacement = fixtures.chain(database, tmp_path)
    with database:
        if change == "other-part":
            _other_part(database)
            database.execute("UPDATE transcripts SET video_part_id=2 WHERE transcript_id=1")
        else:
            database.execute("UPDATE transcripts SET source_kind='subtitle-ai' WHERE transcript_id=1")
    _rejected_without_writes(workflow, client, proof, replacement, "belongs to another part or producer")


@pytest.mark.parametrize("alternative", ["duplicate", "queued", "running", "succeeded"])
def test_recovery_does_not_replace_duplicate_or_active_alternative_recoveries(database, tmp_path, alternative):
    workflow, editorial, _, client, _, proof, render, replacement = fixtures.chain(database, tmp_path)
    recovered_proof, _ = _record_recovery(workflow, editorial, proof, render, replacement)
    with database:
        if alternative == "duplicate":
            _copy_job(database, recovered_proof, "duplicate-recovery")
            database.execute("INSERT INTO workflow_job_dependencies VALUES (?,?)", ("duplicate-recovery", replacement))
        else:
            payload = _payload(database, recovered_proof)
            payload["replacement_run_id"] = "another-producer-run"
            _write_payload(database, recovered_proof, payload)
            database.execute("UPDATE workflow_jobs SET status=? WHERE job_id=?", (alternative, recovered_proof))
    _rejected_without_writes(workflow, client, proof, replacement, "active alternative recovery already exists")


@pytest.mark.parametrize("missing", ["proofread", "render"])
def test_recovery_does_not_treat_incomplete_retirement_as_success(database, tmp_path, missing):
    workflow, editorial, _, client, _, proof, render, replacement = fixtures.chain(database, tmp_path)
    if missing == "render":
        _, recovered_render = _record_recovery(workflow, editorial, proof, render, replacement)
        with database:
            payload = _payload(database, recovered_render)
            payload["repair_of_job_id"] = "another-retired-render"
            _write_payload(database, recovered_render, payload)
    with database:
        database.execute(
            "UPDATE workflow_jobs SET status='cancelled',last_error_code='superseded_by_recovery' WHERE job_id IN (?,?)",
            (proof, render))
    _rejected_without_writes(workflow, client, proof, replacement, "retired chain has no complete recorded recovery")


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_recovery_does_not_reset_terminal_recovery_attempts(database, tmp_path, terminal):
    workflow, editorial, _, client, _, proof, render, replacement = fixtures.chain(database, tmp_path)
    recovered_proof, _ = _record_recovery(workflow, editorial, proof, render, replacement)
    claimed = workflow.claim("fixture", kinds=(JobKind.PROOFREAD,))
    assert claimed.job_id == recovered_proof
    if terminal == "failed":
        workflow.fail(recovered_proof, worker_id="fixture", error_code="provider_content_filter")
    else:
        workflow.cancel(job_ids=(recovered_proof,))
    assert workflow.explain_job(recovered_proof)["status"] == terminal
    _rejected_without_writes(workflow, client, proof, replacement, "existing recovery is terminal")


def test_recovery_rolls_back_if_a_consumer_changes_after_enqueue(database, tmp_path, monkeypatch):
    workflow, _, _, client, _, proof, render, replacement = fixtures.chain(database, tmp_path)
    plan = fixtures.inspect(workflow, proof, replacement)
    before = fixtures.snapshot(database)
    enqueue = workflow.enqueue_editorial_recovery

    def enqueue_then_change_consumer(**kwargs):
        result = enqueue(**kwargs)
        database.execute("UPDATE workflow_jobs SET status='running',attempt_count=1 WHERE job_id=?", (render,))
        return result

    monkeypatch.setattr(workflow, "enqueue_editorial_recovery", enqueue_then_change_consumer)
    with pytest.raises(ValueError, match="consumer changed during recovery"):
        fixtures.inspect(workflow, proof, replacement, apply=True, expected_plan_id=plan["plan_id"])
    assert fixtures.snapshot(database) == before
    assert client.requests == []


def test_recovery_rolls_back_if_external_controls_change_after_enqueue(database, tmp_path, monkeypatch):
    workflow, _, _, client, _, proof, render, replacement = fixtures.chain(database, tmp_path)
    path = tmp_path / "controls.json"
    path.write_text(json.dumps({"retry_holds": {}, "retry_ledger": {}}), encoding="utf-8")
    arguments = {"proofread_job_id": proof, "replacement_asr_job_id": replacement, "control_state_path": path}
    plan = recover_editorial_chain(workflow, **arguments)
    before = fixtures.snapshot(database)
    enqueue = workflow.enqueue_editorial_recovery

    def enqueue_then_add_hold(**kwargs):
        result = enqueue(**kwargs)
        path.write_text(json.dumps({"retry_holds": {render: "inspect consumer"}, "retry_ledger": {}}), encoding="utf-8")
        return result

    monkeypatch.setattr(workflow, "enqueue_editorial_recovery", enqueue_then_add_hold)
    with pytest.raises(ValueError, match="retry control state changed during recovery"):
        recover_editorial_chain(workflow, apply=True, expected_plan_id=plan["plan_id"], **arguments)
    assert fixtures.snapshot(database) == before
    assert json.loads(path.read_text(encoding="utf-8"))["retry_holds"] == {render: "inspect consumer"}
    assert client.requests == []
