from __future__ import annotations
import pytest

from bili_asr.storage import open_database
from bili_asr.storage.workflow_dependency_repair import repair_producer_dependencies
from tests.test_workflow_control_plane import _seed_part, _profile
from bili_asr.storage.workflow import WorkflowRepository
from bili_asr.workflow_models import AsrPolicy


def seeded(tmp_path):
    connection = open_database(tmp_path)
    _seed_part(connection)
    repository = WorkflowRepository(connection)
    profile_id = _profile(repository)
    repository.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=profile_id)
    jobs = {row["kind"]: row["job_id"] for row in connection.execute("SELECT job_id,kind FROM workflow_jobs")}
    with connection:
        connection.execute("INSERT INTO workflow_job_dependencies VALUES (?,?)", (jobs["audio"], jobs["subtitle"]))
        connection.execute("INSERT INTO workflow_job_dependencies VALUES (?,?)", (jobs["asr"], jobs["subtitle"]))
        connection.execute("UPDATE workflow_jobs SET status='failed',attempt_count=4,last_error_code='not_found' WHERE kind='subtitle'")
    return connection, repository, jobs


def test_reviewed_repair_unblocks_audio_and_keeps_asr_audio_dependency(tmp_path):
    connection, repository, jobs = seeded(tmp_path)
    plan = repair_producer_dependencies(connection, [1])
    assert len(plan["changes"]["remove"]) == 2 and not plan["applied"]
    assert repository.claim("worker") is None
    repaired = repair_producer_dependencies(connection, [1], apply=True, expected_plan_id=plan["plan_id"])
    assert repaired["applied"]
    assert repository.claim("worker").job_id == jobs["audio"]
    assert tuple(connection.execute("SELECT status,attempt_count FROM workflow_jobs WHERE kind='subtitle'").fetchone()) == ("failed", 4)
    assert connection.execute("SELECT prerequisite_job_id FROM workflow_job_dependencies WHERE job_id=?", (jobs["asr"],)).fetchone()[0] == jobs["audio"]
    connection.close()


def test_stale_and_running_plans_fail_without_partial_graph_changes(tmp_path):
    connection, _repository, jobs = seeded(tmp_path)
    plan = repair_producer_dependencies(connection, [1])
    with connection:
        connection.execute("UPDATE workflow_jobs SET priority=priority+1,updated_at=updated_at+1 WHERE kind='audio'")
    with pytest.raises(ValueError, match="stale"):
        repair_producer_dependencies(connection, [1], apply=True, expected_plan_id=plan["plan_id"])
    assert connection.execute("SELECT COUNT(*) FROM workflow_job_dependencies").fetchone()[0] == 3
    with connection:
        connection.execute("UPDATE workflow_jobs SET status='running',lease_owner='active',lease_expires_at=9999999999 WHERE kind='audio'")
    with pytest.raises(ValueError, match="running"):
        repair_producer_dependencies(connection, [1])
    connection.close()


def test_cancelled_producer_is_never_resurrected(tmp_path):
    connection, _repository, jobs = seeded(tmp_path)
    with connection:
        connection.execute("UPDATE workflow_jobs SET status='cancelled',attempt_count=4 WHERE kind='audio'")
    plan = repair_producer_dependencies(connection, [1], retry_failed=True)
    repair_producer_dependencies(connection, [1], apply=True, expected_plan_id=plan["plan_id"], retry_failed=True)
    assert tuple(connection.execute("SELECT status,attempt_count FROM workflow_jobs WHERE kind='audio'").fetchone()) == ("cancelled", 4)
    connection.close()
