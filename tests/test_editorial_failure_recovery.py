"""Production failure chains replay offline without changing historical inputs."""

from dataclasses import replace
import json

import pytest

from bili_asr import cli
from bili_asr.editorial import EditorialConfig, canonical, digest, prepare_input
from bili_asr.editorial_quality import check_source_quality
from bili_asr.services.editorial_repair import repair_proofread
from bili_asr.services.archive_migration import initialize_archive
from bili_asr.archive_session import ArchiveSession, ArchiveAccessMode
from bili_asr.workflow_errors import JobExecutionError
from bili_asr.workflow_models import JobKind
from bili_asr.storage import open_database
from tests import test_ai_editorial as editorial_tests
from tests.test_ai_editorial import record, insert_record, runtime, FakeClient, no_changes, envelope
from tests.test_workflow_control_plane import _seed_part

database = editorial_tests.database


def fail_proof(workflow, proof_id):
    claimed = workflow.claim("failure", kinds=(JobKind.PROOFREAD,))
    assert claimed.job_id == proof_id
    workflow.fail(proof_id, worker_id="failure", error_code="RevisionValidationError")


def attempt_result(connection, job_id):
    return connection.execute(
        "SELECT result_json FROM workflow_attempts WHERE job_id=? ORDER BY rowid DESC LIMIT 1",
        (job_id,),
    ).fetchone()[0]


@pytest.mark.parametrize("universal", [False, True])
def test_repair_uses_current_prompt_preserves_attempts_and_renders_new_chain(tmp_path, universal):
    root = tmp_path / "archive"
    if universal:
        initialize_archive(root)
    else:
        open_database(root).close()
    with ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session:
        db = session.connection
        _seed_part(db)
        insert_record(db, record())
        fake = FakeClient()
        workflow, editorial, _, executor = runtime(db, root, fake)
        previous = editorial.build_input(1, None, EditorialConfig(budget_policy="legacy-v1"))
        previous["snapshot"]["system_prompt"] = "Old frozen prompt."
        previous["snapshot"]["prompt_sha256"] = digest("Old frozen prompt.")
        previous["input_id"] = digest(previous["snapshot"])
        # A coherent historical snapshot does not need the current chunk algorithm.
        for index, chunk in enumerate(previous["chunks"]):
            chunk["chunk_id"] = digest({"input_id": previous["input_id"], "index": index})
        with db:
            editorial.store_input(previous)
        old_proof, old_render, *_ = workflow.request_editorial(video_part_id=1, input_id=previous["input_id"])
        fail_proof(workflow, old_proof)
        before = db.execute("SELECT prepared_json FROM editorial_inputs WHERE input_id=?",
                            (previous["input_id"],)).fetchone()[0]
        attempts = [tuple(r) for r in db.execute("SELECT * FROM workflow_attempts")]
        config = EditorialConfig(reasoning_effort="low", max_chunk_segments=1)
        repaired = repair_proofread(workflow, job_id=old_proof, config=config)
        again = repair_proofread(workflow, job_id=old_proof, config=config)
        assert repaired["created"] and not again["created"]
        assert repaired["job_id"] == again["job_id"] != old_proof
        payload = json.loads(db.execute("SELECT payload_json FROM workflow_jobs WHERE job_id=?",
                                       (repaired["job_id"],)).fetchone()[0])
        assert payload["repair_of_job_id"] == old_proof
        assert before == db.execute("SELECT prepared_json FROM editorial_inputs WHERE input_id=?",
                                    (previous["input_id"],)).fetchone()[0]
        assert attempts == [tuple(r) for r in db.execute("SELECT * FROM workflow_attempts")]
        assert workflow.explain_job(old_render)["blocked"]
        assert executor.run().succeeded == 2
        assert all("allowed_issue_refs" in request["messages"][0]["content"] for request in fake.requests)
        assert editorial.revision_for_job(repaired["job_id"])
        assert workflow.explain_job(old_proof)["status"] == "failed"
        assert workflow.explain_job(old_render)["blocked"]
        assert workflow.explain_job(repaired["render_job_id"])["status"] == "succeeded"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_repair_rejects_live_jobs_identical_input_and_wrong_part(database, tmp_path):
    workflow, editorial, _, _ = runtime(database, tmp_path, FakeClient())
    prepared = editorial.prepare(1, None, EditorialConfig())
    proof, *_ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    with pytest.raises(ValueError, match="failed"):
        repair_proofread(workflow, job_id=proof, config=EditorialConfig())
    fail_proof(workflow, proof)
    with pytest.raises(ValueError, match="must change"):
        repair_proofread(workflow, job_id=proof, config=EditorialConfig())
    with pytest.raises(ValueError, match="unknown transcript"):
        repair_proofread(workflow, job_id=proof, config=EditorialConfig(reasoning_effort="low"),
                        base_transcript_id=999)
    with database:
        database.execute("INSERT INTO video_parts(video_part_id,bvid,page_index,cid,title,duration_ms,"
                         "processing_status,created_at,updated_at) "
                         "VALUES (2,'BVtest',1,2,'p1',1000,'metadata_collected',1,1)")
    insert_record(database, record(transcript_id=2, part=2))
    with pytest.raises(ValueError, match="another part"):
        repair_proofread(workflow, job_id=proof, config=EditorialConfig(reasoning_effort="low"),
                        base_transcript_id=2)
    assert database.execute("SELECT count(*) FROM editorial_inputs").fetchone()[0] == 1


def test_repair_rolls_back_new_input_when_enqueue_fails(database, tmp_path, monkeypatch):
    workflow, editorial, _, _ = runtime(database, tmp_path, FakeClient())
    prepared = editorial.prepare(1, None, EditorialConfig())
    proof, *_ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    fail_proof(workflow, proof)
    before = [tuple(row) for row in database.execute("SELECT * FROM workflow_jobs")]

    def fail_enqueue(**kwargs):
        raise RuntimeError("offline simulated enqueue failure")

    monkeypatch.setattr(workflow, "enqueue_editorial", fail_enqueue)
    with pytest.raises(RuntimeError, match="simulated"):
        repair_proofread(workflow, job_id=proof, config=EditorialConfig(reasoning_effort="low"))
    assert database.execute("SELECT count(*) FROM editorial_inputs").fetchone()[0] == 1
    assert before == [tuple(row) for row in database.execute("SELECT * FROM workflow_jobs")]


def test_repetition_gate_prevents_call_and_preserves_raw_source(database, tmp_path):
    bad = record(("All these things are indexes, are indexes. " * 100,), transcript_id=2)
    bad = replace(bad, version=2, segments=(replace(bad.segments[0], end_ms=7799),))
    insert_record(database, bad)
    fake = FakeClient()
    workflow, editorial, _, executor = runtime(database, tmp_path, fake)
    prepared = editorial.prepare(2, None, EditorialConfig())
    proof, render, *_ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert executor.run().failed == 1
    assert fake.requests == []
    assert database.execute("SELECT count(*) FROM editorial_model_calls").fetchone()[0] == 0
    last = workflow.explain_job(proof)["last_attempt"]
    assert last["error_code"] == "editorial_source_repetition"
    saved = attempt_result(database, proof)
    diagnosis = json.loads(saved)["diagnostic"]
    assert diagnosis["transcript_id"] == 2 and diagnosis["segment_ordinal"] == 0
    assert "indexes" not in saved
    assert workflow.explain_job(render)["blocked"]
    assert editorial.read_source(2).segments == bad.segments
    with pytest.raises(JobExecutionError, match="source_repetition"):
        repair_proofread(workflow, job_id=proof, config=EditorialConfig(reasoning_effort="low"))
    # An explicit corrected source creates a new repair identity; original remains.
    repaired = repair_proofread(workflow, job_id=proof, base_transcript_id=1,
                               config=EditorialConfig(reasoning_effort="low"))
    assert executor.run().succeeded == 2
    assert workflow.explain_job(repaired["job_id"])["status"] == "succeeded"


@pytest.mark.parametrize("text,duration", [
    ("强调这个观点。 " * 8, 5000),
    ("All these things are indexes. " * 80, 180000),
    ("abcdefghijklmnop" * 100, 5000),
])
def test_quality_gate_does_not_treat_length_or_ordinary_repetition_as_failure(text, duration):
    base = record((text,))
    base = replace(base, segments=(replace(base.segments[0], end_ms=duration),))
    check_source_quality(prepare_input(base, None, EditorialConfig()))


class BadReferenceClient(FakeClient):
    def complete(self, request, config):
        self.requests.append(request)
        chunk = json.loads(request["messages"][1]["content"])
        value = no_changes(chunk)
        value["paragraphs"][0]["issues"] = [{"note": "uncertain", "candidate": "",
            "evidence_refs": [chunk["editable_segments"][1]["segment_id"], "SECRET https://example.invalid"]}]
        return envelope(value)


def test_reference_error_is_safe_durable_and_still_blocks_revision(database, tmp_path):
    workflow, editorial, _, executor = runtime(database, tmp_path, BadReferenceClient())
    prepared = editorial.prepare(1, None, EditorialConfig())
    proof, render, *_ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert executor.run().failed == 1
    saved = attempt_result(database, proof)
    details = json.loads(saved)["diagnostic"]
    assert details["paragraph_index"] == details["issue_index"] == 0
    assert details["invalid_count"] == 2
    assert len(details["invalid_ref_hashes"]) == 2
    assert "SECRET" not in saved and "https" not in saved
    assert details["call_id"] and details["chunk_id"]
    assert database.execute("SELECT count(*) FROM editorial_revisions").fetchone()[0] == 0
    assert workflow.explain_job(render)["blocked"]


class ExhaustedClient(FakeClient):
    def complete(self, request, config):
        return {"usage": {"prompt_tokens": 33051, "completion_tokens": 262144,
                         "completion_tokens_details": {"reasoning_tokens": 262144}},
                "choices": [{"finish_reason": "length", "message": {"content": "",
                            "reasoning_content": "are indexes " * 80}}]}


def test_exhausted_reasoning_usage_is_saved_without_free_text(database, tmp_path):
    workflow, editorial, _, executor = runtime(database, tmp_path, ExhaustedClient())
    prepared = editorial.prepare(1, None, EditorialConfig())
    proof, *_ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert executor.run().failed == 1
    details = json.loads(attempt_result(database, proof))["diagnostic"]
    assert details["length_exceeded"] and details["output_chars"] == 0
    assert details["reasoning_tokens"] == details["completion_tokens"] == 262144
    assert "indexes" not in canonical(details)
    assert database.execute("SELECT count(*) FROM editorial_chunk_results").fetchone()[0] == 0


@pytest.mark.parametrize("config", [
    EditorialConfig(max_chunk_segments=3), EditorialConfig(max_chunk_chars=25),
])
def test_chunk_limits_cover_sources_once_and_change_input_identity(config):
    source = record(tuple("a sentence" for _ in range(10)))
    prepared = prepare_input(source, None, config)
    assert len(prepared["chunks"]) > 1
    assert [s["segment_id"] for chunk in prepared["chunks"] for s in chunk["editable_segments"]] == [
        f"t1:s{i}" for i in range(10)]
    assert prepared["input_id"] != prepare_input(source, None, EditorialConfig())["input_id"]


def test_cli_repair_only_queues_and_preserves_old_failure(database, tmp_path, capsys):
    workflow, editorial, _, _ = runtime(database, tmp_path, FakeClient())
    prepared = editorial.prepare(1, None, EditorialConfig())
    proof, *_ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    fail_proof(workflow, proof)
    assert cli.main(["workflow", "repair-proofread", "--archive-root", str(tmp_path),
                     "--job-id", proof]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["job_id"] != proof
    assert workflow.explain_job(result["job_id"])["status"] == "queued"
    assert workflow.explain_job(proof)["status"] == "failed"
    assert database.execute("SELECT count(*) FROM editorial_model_calls").fetchone()[0] == 0
