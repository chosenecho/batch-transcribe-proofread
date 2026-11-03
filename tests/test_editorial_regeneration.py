"""Paid invocation budgets and request policy upgrades use only offline fakes."""

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import replace

import pytest
import requests

from bili_asr import cli
from bili_asr.deepseek import (
    DeepSeekClient,
    DeepSeekError,
    DeepSeekTransportError,
    request_body,
)
from bili_asr.editorial import EditorialConfig, digest
from bili_asr.storage import open_database
from bili_asr.storage.editorial_call_budget import PHYSICAL_CALL_SQL
from bili_asr.storage.editorial_replay import ReplayEvidenceError
from bili_asr.storage.workflow_retry import editorial_retry_facts
from bili_asr.workflow_errors import JobExecutionError
from tests import test_ai_editorial as editorial_fixtures
from tests.test_ai_editorial import envelope, no_changes, runtime


@pytest.fixture
def database(tmp_path):
    yield from editorial_fixtures.database.__wrapped__(tmp_path)


class ReferenceClient:
    def __init__(self, failures=1, on_call=None):
        self.requests = []
        self.failures = failures
        self.on_call = on_call

    def complete(self, request, config):
        self.requests.append(deepcopy(request))
        if self.on_call:
            self.on_call()
        chunk = json.loads(request["messages"][1]["content"])
        value = no_changes(chunk)
        if len(self.requests) <= self.failures:
            value["paragraphs"][0]["issues"] = [{"note": "核对", "candidate": "",
                "evidence_refs": [chunk["editable_segments"][1]["segment_id"]]}]
        return envelope(value)


def queued(connection, root, client, config=None):
    workflow, repository, handlers, executor = runtime(connection, root, client)
    prepared = repository.prepare(1, None, config or EditorialConfig())
    job_id, _, _, _ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    return workflow, repository, handlers, executor, prepared, job_id


@pytest.mark.parametrize("endpoint", ["https://api.deepseek.com",
                                       "https://opencode.ai/inference/go/v1",
                                       "https://opencode.ai/zen/go/v1"])
def test_versioned_request_modes_have_only_effective_parameters(endpoint):
    config = EditorialConfig(base_url=endpoint)
    body = request_body({}, config)
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"
    assert body["top_p"] == .95 and "temperature" not in body
    disabled = request_body({}, EditorialConfig(base_url=endpoint, thinking_mode="disabled", temperature=.2))
    assert disabled["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in disabled and disabled["top_p"] == 1
    assert disabled["temperature"] == .2
    legacy = request_body({}, EditorialConfig.from_snapshot({"base_url": endpoint}))
    assert legacy["thinking"] == {"type": "enabled", "reasoning_effort": "high"}
    assert "reasoning_effort" not in legacy and legacy["top_p"] == .95


@pytest.mark.parametrize("value", [-1, 2.1, True, float("nan"), float("inf")])
def test_invalid_temperature_rejected_before_request(value):
    with pytest.raises(ValueError):
        EditorialConfig(thinking_mode="disabled", temperature=value)
    with pytest.raises(ValueError):
        EditorialConfig(temperature=.2)


def test_transport_failure_has_distinct_safe_error_code():
    class Session:
        def post(self, *args, **kwargs):
            raise requests.ReadTimeout("private request details")
    with pytest.raises(DeepSeekTransportError, match="DeepSeek transport failed"):
        DeepSeekClient("offline-key", session=Session()).complete({}, EditorialConfig())


@pytest.mark.parametrize("error", [DeepSeekError("DeepSeek HTTP 402"), ValueError("invalid")])
def test_generic_provider_or_unknown_failure_is_not_maintenance_eligible(database, tmp_path, error):
    class FailedClient:
        def complete(self, request, config):
            raise error
    _, repository, _, executor, _, job_id = queued(database, tmp_path, FailedClient())
    assert executor.run().failed == 1
    assert repository.recovery_status_for_job(job_id)["eligible"] is False


def test_regeneration_persists_feedback_and_preserves_original_input(database, tmp_path):
    client = ReferenceClient()
    _, repository, _, executor, prepared, job_id = queued(database, tmp_path, client)
    frozen = deepcopy(prepared)
    assert executor.run().succeeded == 2
    assert len(client.requests) == 2
    assert client.requests[0]["messages"] == client.requests[1]["messages"][:2]
    feedback = json.loads(client.requests[1]["messages"][2]["content"])
    assert feedback["validation_diagnostic"]["reason"] == "cross_paragraph_reference"
    assert feedback["regeneration"] == 1
    assert repository.load_input(prepared["input_id"]) == frozen
    calls = repository.physical_calls(prepared["input_id"], prepared["chunks"][0]["chunk_id"])
    assert calls[0]["error_code"] == "RevisionValidationError"
    assert calls[1]["stored_request"]["recovery"]["previous_call_id"] == calls[0]["call_id"]
    assert repository.revision_for_job(job_id)


def test_second_chunk_reference_recovery_reuses_first_success(database, tmp_path):
    with database:
        database.execute("INSERT INTO transcript_segments VALUES (1,2,2000,2900,?)", ("第三段文字",))
        database.execute("INSERT INTO transcript_segments VALUES (1,3,3000,3900,?)", ("第四段文字",))
        database.execute("UPDATE transcript_coverage_attestations SET decoded_s=3.9,produced_s=3.9 WHERE transcript_id=1")
        database.execute("UPDATE transcript_asr_evidence SET evidence_json=json_set(evidence_json,'$.audio.duration_ms',3900) WHERE transcript_id=1")
    class SecondChunkClient(ReferenceClient):
        def complete(self, request, config):
            self.requests.append(deepcopy(request))
            chunk = json.loads(request["messages"][1]["content"])
            value = no_changes(chunk)
            if len(self.requests) == 2:
                value["paragraphs"][0]["issues"] = [{"note": "核对", "candidate": "",
                    "evidence_refs": [chunk["editable_segments"][1]["segment_id"]]}]
            return envelope(value)
    client = SecondChunkClient()
    _, repository, _, executor, prepared, job_id = queued(database, tmp_path, client,
        EditorialConfig(max_chunk_segments=2, context_segments=0))
    assert len(prepared["chunks"]) == 2
    assert executor.run().succeeded == 2 and len(client.requests) == 3
    first, second = prepared["chunks"]
    assert len(repository.physical_calls(prepared["input_id"], first["chunk_id"])) == 1
    calls = repository.physical_calls(prepared["input_id"], second["chunk_id"])
    assert len(calls) == 2 and calls[0]["response"] is not None
    assert calls[0]["error_code"] == "RevisionValidationError"
    assert calls[1]["stored_request"]["recovery"]["previous_call_id"] == calls[0]["call_id"]
    assert repository.revision_for_job(job_id)


@pytest.mark.parametrize("response_id", ["offline-call", "local-precheck"])
def test_repeated_bad_references_exhaust_once_and_remain_exhausted_after_restart(database, tmp_path, response_id):
    class ResponseIdClient(ReferenceClient):
        def complete(self, request, config):
            response = super().complete(request, config)
            response["id"] = response_id
            return response
    client = ResponseIdClient(failures=10)
    workflow, repository, _, executor, prepared, job_id = queued(database, tmp_path, client)
    def check_committed_reservation():
        # A separate connection observes the reservation before the fake HTTP
        # response exists, including earlier responses with the local marker.
        with closing(sqlite3.connect(tmp_path / "archive.db")) as reader:
            rows = reader.execute("SELECT request_json,response_json FROM editorial_model_calls ORDER BY rowid").fetchall()
        assert len(rows) == len(client.requests)
        assert all(json.loads(row[0])["invocation_kind"] == "provider" for row in rows)
        assert rows[-1][1] is None
        assert len(repository.physical_calls(prepared["input_id"], prepared["chunks"][0]["chunk_id"])) == len(rows)
    client.on_call = check_committed_reservation
    assert executor.run().failed == 1 and len(client.requests) == 3
    preserved_calls = [tuple(row) for row in database.execute("SELECT * FROM editorial_model_calls ORDER BY rowid")]
    state = repository.recovery_status_for_job(job_id)
    assert state["exhausted"] and not state["eligible"]
    assert editorial_retry_facts(database, [{"job_id": job_id, "kind": "proofread"}])[job_id] == state
    result = database.execute("SELECT result_json FROM workflow_attempts WHERE job_id=?", (job_id,)).fetchone()[0]
    assert json.loads(result)["diagnostic"]["physical_calls"] == 3
    assert workflow.explain_job(job_id)["error_code"] == "EditorialRecoveryExhausted"
    with closing(open_database(tmp_path)) as connection:
        new_client = ReferenceClient(failures=10)
        new_workflow, new_repository, _, new_executor = runtime(connection, tmp_path, new_client)
        new_workflow.requeue_failed(job_ids=[job_id])
        assert new_executor.run().failed == 1 and not new_client.requests
        assert len(new_repository.physical_calls(prepared["input_id"], prepared["chunks"][0]["chunk_id"])) == 3
        assert [tuple(row) for row in connection.execute("SELECT * FROM editorial_model_calls ORDER BY rowid")] == preserved_calls


@pytest.mark.parametrize(("kind", "encoding", "consumed"), [
    ("provider", "v2", 1), ("provider", "unversioned", 1),
    ("provider", "legacy-envelope", 1), ("provider", "legacy-raw", 1),
    ("local_precheck", "v2", 0), ("response_replay", "v2", 0),
])
def test_local_response_marker_only_exempts_legacy_formatting_without_invocation_kind(
        database, tmp_path, kind, encoding, consumed):
    workflow, repository, _, _, prepared, job_id = queued(database, tmp_path, ReferenceClient(),
                                                        EditorialConfig(max_chunk_regenerations=0))
    job = workflow.claim("marker-worker")
    chunk = prepared["chunks"][0]
    call_id = repository.begin_call(job, prepared["input_id"], chunk["chunk_id"],
                                   request_body(chunk, EditorialConfig()), invocation_kind=kind)
    response = {"id": "local-precheck", "model": "local-precheck", "choices": [],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
    repository.finish_call(call_id, response, "RevisionValidationError")
    row = database.execute("SELECT request_json FROM editorial_model_calls WHERE call_id=?", (call_id,)).fetchone()
    stored = json.loads(row[0])
    if encoding != "v2":
        del stored["envelope_version"]
    if encoding == "legacy-envelope":
        del stored["invocation_kind"]
    elif encoding == "legacy-raw":
        stored = stored["request"]
    with database:
        database.execute("UPDATE editorial_model_calls SET request_json=? WHERE call_id=?", (json.dumps(stored), call_id))
    evidence = [tuple(row) for row in database.execute("SELECT * FROM editorial_model_calls")]
    calls = repository.physical_calls(prepared["input_id"], chunk["chunk_id"])
    assert len(calls) == consumed
    retry = editorial_retry_facts(database, [{"job_id": job_id, "kind": "proofread"}])[job_id]
    assert retry["exhausted"] is bool(consumed) and retry["eligible"] is not bool(consumed)
    if consumed:
        with pytest.raises(JobExecutionError) as error:
            repository.begin_call(job, prepared["input_id"], chunk["chunk_id"],
                                  request_body(chunk, EditorialConfig()), recovery={"max_physical_calls": 1})
        assert error.value.error_code == "EditorialRecoveryExhausted"
    assert [tuple(row) for row in database.execute("SELECT * FROM editorial_model_calls")] == evidence


@pytest.mark.parametrize("legacy_request", [False, True])
@pytest.mark.parametrize("response_case", ["missing_id", "bad_json", "unfinished", "timeout"])
def test_ambiguous_response_cannot_release_runtime_or_maintenance_reservation(
        database, tmp_path, legacy_request, response_case):
    workflow, repository, _, _, prepared, job_id = queued(database, tmp_path, ReferenceClient(),
                                                        EditorialConfig(max_chunk_regenerations=0))
    job = workflow.claim("ambiguous-worker")
    chunk = prepared["chunks"][0]
    request = request_body(chunk, EditorialConfig())
    call_id = repository.begin_call(job, prepared["input_id"], chunk["chunk_id"], request)
    if response_case != "unfinished":
        response = None if response_case == "timeout" else {
            "model": "local-precheck", "choices": [], "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
        repository.finish_call(call_id, response, "Timeout" if response_case == "timeout" else "RevisionValidationError")
    with database:
        if legacy_request:
            stored = json.loads(database.execute("SELECT request_json FROM editorial_model_calls").fetchone()[0])
            del stored["invocation_kind"], stored["envelope_version"]
            database.execute("UPDATE editorial_model_calls SET request_json=?", (json.dumps(stored),))
        if response_case == "bad_json":
            database.execute("UPDATE editorial_model_calls SET response_json='{' WHERE call_id=?", (call_id,))
    evidence = [tuple(row) for row in database.execute("SELECT * FROM editorial_model_calls")]
    projection = editorial_retry_facts(database, [{"job_id": job_id, "kind": "proofread"}])[job_id]
    assert projection == {"eligible": False, "exhausted": True, "reason": "physical_budget_exhausted"}
    if response_case == "bad_json":
        with pytest.raises(ReplayEvidenceError):
            repository.recovery_status_for_job(job_id)
        expected_error = ReplayEvidenceError
    else:
        assert repository.recovery_status_for_job(job_id) == projection
        assert len(repository.physical_calls(prepared["input_id"], chunk["chunk_id"])) == 1
        expected_error = JobExecutionError
    with pytest.raises(expected_error):
        repository.begin_call(job, prepared["input_id"], chunk["chunk_id"], request,
                              recovery={"max_physical_calls": 1})
    assert [tuple(row) for row in database.execute("SELECT * FROM editorial_model_calls")] == evidence


@pytest.mark.parametrize("encoding", ["legacy-envelope", "legacy-raw"])
@pytest.mark.parametrize("evidence_case", ["complete", "missing_id", "missing_model", "missing_usage",
    "provider_choices", "extra_field", "boolean_usage", "uncommitted", "error", "unfinished", "malformed", "unknown",
    "explicit_provider"])
def test_historical_local_format_requires_complete_committed_writer_evidence(database, tmp_path, encoding, evidence_case):
    local_record = replace(editorial_fixtures.record(("  这是一句。。  ",), transcript_id=2), version=2)
    editorial_fixtures.insert_record(database, local_record)
    client = ReferenceClient()
    workflow, repository, _, executor = runtime(database, tmp_path, client)
    prepared = repository.prepare(2, None, EditorialConfig(max_chunk_regenerations=0))
    workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert executor.run().succeeded == 2 and client.requests == []
    row = database.execute("SELECT * FROM editorial_model_calls").fetchone()
    stored = json.loads(row["request_json"])
    assert stored.pop("invocation_kind") == "local_precheck"
    del stored["envelope_version"]
    if evidence_case == "explicit_provider":
        stored["invocation_kind"] = "provider"
    elif encoding == "legacy-raw":
        stored = stored["request"]
    response = json.loads(row["response_json"])
    if evidence_case.startswith("missing_"):
        del response[evidence_case.removeprefix("missing_")]
    elif evidence_case == "provider_choices":
        response["choices"] = envelope(no_changes(prepared["chunks"][0]))["choices"]
    elif evidence_case == "extra_field":
        response["object"] = "chat.completion"
    elif evidence_case == "boolean_usage":
        response["usage"]["prompt_tokens"] = False
    raw_response = "{" if evidence_case == "malformed" else json.dumps(response)
    with database:
        database.execute("UPDATE editorial_model_calls SET request_json=?,response_json=? WHERE call_id=?",
                         (json.dumps(stored), raw_response, row["call_id"]))
        if evidence_case == "uncommitted":
            database.execute("DELETE FROM editorial_chunk_results")
        elif evidence_case == "error":
            database.execute("UPDATE editorial_model_calls SET error_code='DeepSeekError'")
        elif evidence_case in {"unfinished", "unknown"}:
            database.execute("UPDATE editorial_model_calls SET response_json=NULL,finished_at=NULL")
    evidence = [tuple(item) for item in database.execute("SELECT * FROM editorial_model_calls")]
    scalar = database.execute(f"SELECT {PHYSICAL_CALL_SQL} FROM editorial_model_calls m").fetchone()[0]
    assert scalar == int(evidence_case != "complete")
    if evidence_case == "malformed":
        with pytest.raises(ReplayEvidenceError) as error:
            repository.physical_calls(prepared["input_id"], prepared["chunks"][0]["chunk_id"])
        assert error.value.safe_details["reason_code"] == "response_json_invalid"
    else:
        assert len(repository.physical_calls(prepared["input_id"], prepared["chunks"][0]["chunk_id"])) == scalar
    assert [tuple(item) for item in database.execute("SELECT * FROM editorial_model_calls")] == evidence


def test_unknown_references_fail_once_without_regeneration(database, tmp_path):
    class UnknownClient(ReferenceClient):
        def complete(self, request, config):
            response = super().complete(request, config)
            value = json.loads(response["choices"][0]["message"]["content"])
            value["paragraphs"][0]["issues"][0]["evidence_refs"] = ["unknown-id"]
            return envelope(value)
    client = UnknownClient()
    _, repository, _, executor, _, job_id = queued(database, tmp_path, client)
    assert executor.run().failed == 1 and len(client.requests) == 1
    assert repository.recovery_status_for_job(job_id)["reason"] == "no_allowlisted_failure"


def test_cancel_during_provider_call_prevents_regeneration_and_checkpoint(database, tmp_path):
    client = ReferenceClient()
    workflow, _, _, executor, _, job_id = queued(database, tmp_path, client)
    client.on_call = lambda: workflow.cancel(job_ids=[job_id])
    executor.run()
    assert len(client.requests) == 1
    assert database.execute("SELECT COUNT(*) FROM editorial_chunk_results").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM editorial_revisions").fetchone()[0] == 0


def test_unknown_inflight_reservation_consumes_budget_without_call(database, tmp_path):
    client = ReferenceClient(failures=0)
    workflow, repository, _, executor, prepared, job_id = queued(database, tmp_path, client,
                                                               EditorialConfig(max_chunk_regenerations=0))
    job = workflow.claim("crashed-worker")
    chunk = prepared["chunks"][0]
    repository.begin_call(job, prepared["input_id"], chunk["chunk_id"], request_body(chunk, EditorialConfig()),
                          recovery={"policy": "paragraph-reference-v1", "max_physical_calls": 1, "regeneration": 0})
    workflow.fail(job_id, worker_id="crashed-worker", error_code="DeepSeekError")
    workflow.requeue_failed(job_ids=[job_id])
    assert executor.run().failed == 1 and not client.requests
    assert repository.recovery_status_for_job(job_id)["exhausted"]


def test_last_completed_envelope_replay_does_not_consume_budget(database, tmp_path):
    client = ReferenceClient(failures=0)
    workflow, repository, _, executor, prepared, job_id = queued(database, tmp_path, client,
                                                               EditorialConfig(max_chunk_regenerations=0))
    job = workflow.claim("crashed-worker")
    chunk = prepared["chunks"][0]
    request = request_body(chunk, EditorialConfig())
    call_id = repository.begin_call(job, prepared["input_id"], chunk["chunk_id"], request,
                                   recovery={"policy": "paragraph-reference-v1", "max_physical_calls": 1, "regeneration": 0})
    repository.finish_call(call_id, envelope(no_changes(chunk)))
    workflow.fail(job_id, worker_id="crashed-worker", error_code="crash")
    workflow.requeue_failed(job_ids=[job_id])
    assert executor.run().succeeded == 2 and not client.requests
    assert len(repository.physical_calls(prepared["input_id"], chunk["chunk_id"])) == 1
    row = database.execute("SELECT request_json FROM editorial_model_calls ORDER BY rowid DESC LIMIT 1").fetchone()
    assert json.loads(row[0])["invocation_kind"] == "response_replay"


def test_bad_cached_response_is_reclassified_then_regenerated_once(database, tmp_path):
    client = ReferenceClient(failures=0)
    workflow, repository, _, executor, prepared, job_id = queued(database, tmp_path, client)
    job = workflow.claim("crashed-worker")
    chunk = prepared["chunks"][0]
    request = request_body(chunk, EditorialConfig())
    call_id = repository.begin_call(job, prepared["input_id"], chunk["chunk_id"], request,
                                   recovery={"policy": "paragraph-reference-v1", "max_physical_calls": 3, "regeneration": 0})
    invalid = no_changes(chunk)
    invalid["paragraphs"][0]["issues"] = [{"note": "核对", "candidate": "", "evidence_refs": ["t1:s1"]}]
    repository.finish_call(call_id, envelope(invalid))
    with database:
        database.execute("UPDATE editorial_model_calls SET finished_at=123 WHERE call_id=?", (call_id,))
    workflow.fail(job_id, worker_id="crashed-worker", error_code="crash")
    workflow.requeue_failed(job_ids=[job_id])
    assert executor.run().succeeded == 2 and len(client.requests) == 1
    row = database.execute("SELECT finished_at,error_code FROM editorial_model_calls WHERE call_id=?", (call_id,)).fetchone()
    assert row["finished_at"] == 123 and row["error_code"] == "RevisionValidationError"
    assert len(repository.physical_calls(prepared["input_id"], chunk["chunk_id"])) == 2


def test_legacy_raw_call_is_classified_read_only_without_enabling_regeneration(database, tmp_path):
    client = ReferenceClient(failures=5)
    workflow, repository, _, executor, prepared, job_id = queued(database, tmp_path, client,
        EditorialConfig.from_snapshot({}))
    assert executor.run().failed == 1 and len(client.requests) == 1
    row = database.execute("SELECT call_id,request_json FROM editorial_model_calls").fetchone()
    raw = json.loads(row["request_json"])["request"]
    with database:
        database.execute("UPDATE editorial_model_calls SET request_json=? WHERE call_id=?", (json.dumps(raw), row["call_id"]))
    before = [tuple(r) for r in database.execute("SELECT * FROM editorial_model_calls")]
    assert repository.recovery_status_for_job(job_id) == {
        "eligible": True, "exhausted": False, "reason": "cross_paragraph_reference"}
    assert [tuple(r) for r in database.execute("SELECT * FROM editorial_model_calls")] == before
    workflow.requeue_failed(job_ids=[job_id])
    assert executor.run().failed == 1 and len(client.requests) == 2
    assert client.requests[0] == client.requests[1]
    assert repository.load_input(prepared["input_id"]) == prepared


def test_explicit_baseline_reuses_legacy_but_large_default_creates_new_identity(database, tmp_path):
    _, repository, _, _, prepared, _ = queued(database, tmp_path, ReferenceClient(),
        EditorialConfig.from_snapshot({}))
    # Remove fields that were absent in archives, recomputing fixture identity.
    legacy = deepcopy(prepared)
    for field in ("request_policy", "temperature", "max_chunk_regenerations"):
        del legacy["snapshot"]["config"][field]
    legacy["input_id"] = digest(legacy["snapshot"])
    repository.store_input(legacy)
    assert repository.build_input(1, None, EditorialConfig.preset("baseline-v1"), reuse_existing=True) == legacy
    assert repository.build_input(1, None, EditorialConfig(), reuse_existing=True)["input_id"] != legacy["input_id"]
    fresh = repository.build_input(1, None, EditorialConfig(max_chunk_regenerations=1), reuse_existing=True)
    assert fresh["input_id"] != legacy["input_id"]
    assert repository.build_input(1, None, EditorialConfig(), reuse_existing=False)["input_id"] != legacy["input_id"]


def test_cli_freezes_temperature_and_rejects_ignored_enabled_temperature(database, tmp_path, capsys):
    args = ["workflow", "proofread", "--archive-root", str(tmp_path), "--part-id", "1", "--temperature", ".2"]
    assert cli.main(args) == 1
    assert "temperature requires disabled thinking" in capsys.readouterr().err
    assert cli.main(args + ["--thinking-mode", "disabled"]) == 0
    config = json.loads(database.execute("SELECT prepared_json FROM editorial_inputs").fetchone()[0])["snapshot"]["config"]
    assert config["temperature"] == .2 and config["request_policy"] == "deepseek-v2"
