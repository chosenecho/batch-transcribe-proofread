"""Only reviewed error codes and bounded diagnostic fields cross to storage."""
import json
import pytest

from bili_asr.workflow_errors import JobExecutionError, safe_job_details
from bili_asr.workflow import WorkflowExecutor
from bili_asr.workflow_models import AsrPolicy, JobKind
from bili_asr.storage import open_database
from bili_asr.storage.workflow import WorkflowRepository
from tests.test_workflow_control_plane import _seed_part, _profile


@pytest.mark.parametrize("value", [
    {"url": "identifier"}, {"cookie": "secret"}, {"candidate": "https://private.invalid/a"},
    {"nested": {"raw_body": "secret"}}, {"count": float("nan")}, {"count": 2**2000},
    {"candidates": list(range(33))}, {"message": "arbitrary exception text with paths"},
])
def test_private_or_unbounded_diagnostics_are_refused(value):
    with pytest.raises(ValueError):
        safe_job_details(value)


def test_executor_preserves_actionable_failure_and_safe_attempt_evidence(tmp_path):
    connection = open_database(tmp_path)
    _seed_part(connection)
    repository = WorkflowRepository(connection)
    repository.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=_profile(repository))
    details = {"attempted_tracks": 2, "eligible_tracks": 2, "outcomes": ["empty_rows", "invalid_timeline"]}
    def fail(_job):
        raise JobExecutionError("subtitle_candidates_exhausted", details)
    executor = WorkflowExecutor(repository, worker_id="worker", handlers={JobKind.SUBTITLE: fail}, kinds=(JobKind.SUBTITLE,))
    assert executor.run().failed == 1
    row = connection.execute("SELECT error_code,result_json FROM workflow_attempts").fetchone()
    assert row["error_code"] == "subtitle_candidates_exhausted"
    assert json.loads(row["result_json"]) == {"diagnostic": details}
    details["attempted_tracks"] = 99
    assert json.loads(row["result_json"])["diagnostic"]["attempted_tracks"] == 2
    # Independent audio is still claimable after source failure.
    assert repository.claim("next", kinds=(JobKind.AUDIO,)).kind is JobKind.AUDIO
    connection.close()
