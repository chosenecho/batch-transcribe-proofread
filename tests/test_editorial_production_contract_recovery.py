"""The three #350 failure families use the existing strict repair boundary."""

import json

import pytest

from bili_asr.editorial import EditorialConfig
from bili_asr.services.editorial_repair import repair_proofread
from tests import test_ai_editorial as fixtures
from tests.test_ai_editorial import FakeClient, envelope, no_changes, runtime

database = fixtures.database


class ContractFailureClient(FakeClient):
    def __init__(self, defect):
        super().__init__()
        self.defect = defect

    def complete(self, request, config):
        self.requests.append(request)
        chunk = json.loads(request["messages"][1]["content"])
        value = no_changes(chunk)
        paragraph = value["paragraphs"][0]
        if self.defect == "reference":
            paragraph["issues"] = [{"note": "uncertain", "candidate": "", "evidence_refs": ["unknown"]}]
        elif self.defect == "paragraph":
            paragraph["text"] = "# Invalid heading"
        else:
            paragraph.pop("text")
        return envelope(value)


@pytest.mark.parametrize("defect", ["reference", "paragraph", "missing_field"])
def test_existing_explicit_repair_keeps_strict_contract_and_original_failure(database, tmp_path, defect):
    invalid = ContractFailureClient(defect)
    workflow, editorial, handlers, executor = runtime(database, tmp_path, invalid)
    prepared = editorial.prepare(1, None, EditorialConfig(max_chunk_regenerations=0))
    old, render, *_ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert executor.run().failed == 1 and len(invalid.requests) == 1
    assert workflow.explain_job(old)["last_attempt"]["error_code"] == "RevisionValidationError"
    assert workflow.explain_job(render)["blocked"]
    before_attempts = [tuple(row) for row in database.execute("SELECT * FROM workflow_attempts ORDER BY rowid")]
    before_calls = [tuple(row) for row in database.execute("SELECT * FROM editorial_model_calls ORDER BY rowid")]
    repaired = repair_proofread(workflow, job_id=old,
        config=EditorialConfig(reasoning_effort="low", max_chunk_segments=1))
    assert repaired["input_id"] != prepared["input_id"] and repaired["job_id"] != old
    assert [tuple(row) for row in database.execute("SELECT * FROM workflow_attempts ORDER BY rowid")] == before_attempts
    assert [tuple(row) for row in database.execute("SELECT * FROM editorial_model_calls ORDER BY rowid")] == before_calls
    valid = FakeClient()
    handlers.client = valid
    assert executor.run().succeeded == 2 and len(valid.requests) == 2
    assert workflow.explain_job(old)["status"] == "failed" and workflow.explain_job(render)["blocked"]
    assert editorial.load_input(prepared["input_id"])["snapshot"] == prepared["snapshot"]
