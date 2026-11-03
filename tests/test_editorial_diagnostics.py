"""Actionable editorial failures expose positions and counts, never source text."""

import json
from copy import deepcopy

import pytest
import requests

from bili_asr.deepseek import DeepSeekClient, DeepSeekError, parse_response
from bili_asr.editorial import (
    EditorialConfig,
    RevisionValidationError,
    prepare_input,
    validate_revision,
)
from bili_asr.workflow_errors import safe_job_details
from tests import test_ai_editorial as editorial_tests
from tests.test_ai_editorial import FakeClient, envelope, no_changes, record, runtime

database = editorial_tests.database


@pytest.mark.parametrize("mutation,reason,location", [
    (lambda value: value.update(extra="SECRET https://private.invalid"), "invalid_fields", {}),
    (lambda value: value.update(chunk_id="SECRET https://private.invalid"), "wrong_chunk_identity", {}),
    (lambda value: value.update(paragraphs={}), "invalid_paragraphs", {}),
    (lambda value: value.update(paragraphs=[]), "empty_paragraphs", {}),
    (lambda value: value["paragraphs"][0].pop("text"), "invalid_fields", {"paragraph_index": 0}),
    (lambda value: value["paragraphs"].reverse(), "source_coverage_order", {"paragraph_index": 0}),
    (lambda value: value["paragraphs"][0].update(text=""), "invalid_string", {"paragraph_index": 0}),
    (lambda value: value["paragraphs"][0].update(text="SECRET\x01"), "control_character", {"paragraph_index": 0}),
    (lambda value: value["paragraphs"][0].update(text="# SECRET"), "invalid_paragraph_format", {"paragraph_index": 0}),
    (lambda value: value["paragraphs"][0].update(issues={}), "invalid_issues", {"paragraph_index": 0}),
    (lambda value: value["paragraphs"].pop(), "incomplete_source_coverage", {}),
])
def test_revision_failures_are_classified_without_reflecting_response(mutation, reason, location):
    chunk = prepare_input(record(), None, EditorialConfig())["chunks"][0]
    response = no_changes(chunk)
    mutation(response)
    with pytest.raises(RevisionValidationError) as captured:
        validate_revision(chunk, response)
    error = captured.value
    assert error.error_code == "RevisionValidationError"
    assert error.safe_details["stage"] == "revision_validation"
    assert error.safe_details["reason_code"] == reason
    assert all(error.safe_details[key] == value for key, value in location.items())
    assert safe_job_details(error.safe_details) == error.safe_details
    assert "SECRET" not in json.dumps(error.safe_details)


@pytest.mark.parametrize("issue,reason", [
    ({"note": "SECRET"}, "invalid_fields"),
    ({"note": "", "candidate": "SECRET", "evidence_refs": []}, "invalid_string"),
    ({"note": "uncertain", "candidate": "SECRET\x01", "evidence_refs": []}, "control_character"),
    ({"note": "uncertain", "candidate": "", "evidence_refs": ["SECRET https://private.invalid"]},
     "invalid_evidence_refs"),
])
def test_issue_diagnostics_locate_paragraph_and_issue(issue, reason):
    chunk = prepare_input(record(), None, EditorialConfig())["chunks"][0]
    response = no_changes(chunk)
    response["paragraphs"][0]["issues"] = [issue]
    with pytest.raises(RevisionValidationError) as captured:
        validate_revision(chunk, response)
    diagnosis = captured.value.safe_details
    assert diagnosis["reason_code"] == reason
    assert diagnosis["paragraph_index"] == diagnosis["issue_index"] == 0
    assert "SECRET" not in json.dumps(diagnosis)


@pytest.mark.parametrize("value,code,reason", [
    ({}, "deepseek_invalid_envelope", "invalid_envelope"),
    ({"choices": []}, "deepseek_invalid_envelope", "invalid_envelope"),
    ({"choices": [{"finish_reason": "stop", "message": {"content": ""}}]},
     "deepseek_empty_output", "empty_output"),
    ({"choices": [{"finish_reason": "stop", "message": {"content": "SECRET invalid JSON"}}]},
     "deepseek_invalid_revision_json", "invalid_revision_json"),
    ({"choices": [{"finish_reason": "length", "message": {"content": "SECRET"}}]},
     "deepseek_output_truncated", "unfinished_output"),
])
def test_response_parsing_has_stable_safe_failure_code(value, code, reason):
    with pytest.raises(DeepSeekError) as captured:
        parse_response(value)
    error = captured.value
    assert error.error_code == code
    assert error.safe_details["stage"] == "response_parse"
    assert error.safe_details["reason_code"] == reason
    assert "SECRET" not in str(error) + json.dumps(error.safe_details)


class Session:
    def __init__(self, *, status=200, value=None, failure=None):
        self.status_code, self.value, self.failure = status, value, failure
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        if self.failure:
            raise self.failure
        return self

    def json(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


@pytest.mark.parametrize("session,code,reason", [
    (Session(status=402, value="SECRET"), "deepseek_http_402", "http_status"),
    (Session(status=429), "deepseek_http_429", "http_status"),
    (Session(failure=requests.ConnectionError("SECRET https://private.invalid")),
     "deepseek_transport_failed", "transport_failure"),
    (Session(value=ValueError("SECRET")), "deepseek_non_json_response", "non_json_response"),
    (Session(value=[]), "deepseek_invalid_envelope", "invalid_envelope"),
])
def test_provider_errors_are_not_retried_or_leaked(session, code, reason):
    with pytest.raises(DeepSeekError) as captured:
        DeepSeekClient("SECRET", session=session).complete({}, EditorialConfig())
    assert session.calls == 1
    error = captured.value
    assert error.error_code == code
    assert error.safe_details["reason_code"] == reason
    assert "SECRET" not in str(error) + json.dumps(error.safe_details)


def test_missing_configuration_is_distinct_from_transport_failure(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    session = Session()
    with pytest.raises(DeepSeekError) as captured:
        DeepSeekClient(session=session).complete({}, EditorialConfig())
    assert session.calls == 0
    assert captured.value.error_code == "deepseek_missing_api_key"
    assert captured.value.safe_details["reason_code"] == "missing_api_key"


class MalformedClient(FakeClient):
    def complete(self, request, config):
        chunk = json.loads(request["messages"][1]["content"])
        response = deepcopy(no_changes(chunk))
        response["paragraphs"][0].pop("text")
        response["paragraphs"][0]["extra"] = "SECRET raw response evidence"
        return envelope(response)


def test_malformed_revision_keeps_raw_evidence_but_persists_safe_diagnostic(database, tmp_path):
    workflow, editorial, _, executor = runtime(database, tmp_path, MalformedClient())
    prepared = editorial.prepare(1, None, EditorialConfig())
    proof, *_ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert executor.run().failed == 1
    saved = database.execute("SELECT result_json FROM workflow_attempts WHERE job_id=?", (proof,)).fetchone()[0]
    diagnosis = json.loads(saved)["diagnostic"]
    assert diagnosis["reason_code"] == "invalid_fields"
    assert diagnosis["input_id"] == prepared["input_id"]
    assert diagnosis["call_id"] and diagnosis["chunk_id"]
    assert "SECRET" not in saved
    call = database.execute("SELECT response_json,error_code FROM editorial_model_calls").fetchone()
    assert "SECRET" in call["response_json"]
    assert call["error_code"] == "RevisionValidationError"
    assert database.execute("SELECT count(*) FROM editorial_revisions").fetchone()[0] == 0


class HttpFailureClient(FakeClient):
    def complete(self, request, config):
        raise DeepSeekError("DeepSeek HTTP 402", error_code="deepseek_http_402",
                            reason_code="http_status", http_status=402)


def test_runtime_preserves_classified_code_in_attempt_and_call(database, tmp_path):
    workflow, editorial, _, executor = runtime(database, tmp_path, HttpFailureClient())
    prepared = editorial.prepare(1, None, EditorialConfig())
    proof, *_ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert executor.run().failed == 1
    attempt = database.execute("SELECT error_code,result_json FROM workflow_attempts WHERE job_id=?", (proof,)).fetchone()
    assert attempt["error_code"] == "deepseek_http_402"
    assert json.loads(attempt["result_json"])["diagnostic"]["http_status"] == 402
    assert database.execute("SELECT error_code FROM editorial_model_calls").fetchone()[0] == "deepseek_http_402"
