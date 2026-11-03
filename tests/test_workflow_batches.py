from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from threading import Barrier
import sqlite3

import pytest

from bili_asr.storage.database import connect_database, open_database
from bili_asr.storage.snapshots import create_database_snapshot
from bili_asr.storage.workflow import WorkflowRepository
from bili_asr.storage.workflow_batches import BatchRepository, install_batches
from bili_asr.storage.workflow_batch_contract import require_batches
from bili_asr.storage.schema_contract import SchemaContractError


def seed(connection, count):
    with connection:
        connection.executemany(
            "INSERT INTO workflow_jobs(job_id,kind,dedupe_key,payload_json,status,available_at,created_at,updated_at) "
            "VALUES (?,'index',?,'{}','queued',0,0,0)", [(f"job-{i:03}", f"key-{i}") for i in range(count)])


def test_10_drain_pause_single_independent_job_and_20_resume_survive_backup(tmp_path):
    connection = open_database(tmp_path)
    try:
        seed(connection, 31)
        install_batches(connection)
        batches, workflow = BatchRepository(connection), WorkflowRepository(connection)
        batch = batches.create([f"job-{i:03}" for i in range(30)], actor="operator", reason="synthetic acceptance")
        claimed = [workflow.claim(f"worker-{i}") for i in range(10)]
        assert all(claimed)
        paused = batches.set_state(batch["batch_id"], paused=True, actor="operator", reason="finish current claims")
        assert paused["running"] == 10 and paused["paused"] == 20
        detail = json.loads(connection.execute(
            "SELECT detail_json FROM workflow_batch_events WHERE event='paused'").fetchone()[0])
        assert set(detail["claimed_job_ids"]) == {job.job_id for job in claimed}
        for job in claimed:
            workflow.finish(job.job_id, worker_id=job.lease_owner)
        supplement = batches.create(["job-030"], desired_workers=1, actor="operator", reason="single independent member")
        one = workflow.claim("single-worker")
        assert one.job_id == "job-030"
        workflow.finish(one.job_id, worker_id="single-worker")
        assert batches.status(supplement["batch_id"])["complete"]
        assert workflow.claim("blocked") is None
        backup = tmp_path / "backup.db"
        create_database_snapshot(tmp_path / "archive.db", backup)
        with closing(connect_database(backup)) as restored:
            assert BatchRepository(restored).status(batch["batch_id"])["state"] == "paused"
            assert WorkflowRepository(restored).claim("after-restart") is None
        batches.set_state(batch["batch_id"], paused=False, desired_workers=20, actor="operator", reason="resume")
        remaining = [workflow.claim(f"next-{i}") for i in range(20)]
        assert len({job.job_id for job in remaining}) == 20
        for job in remaining:
            workflow.finish(job.job_id, worker_id=job.lease_owner)
        assert batches.status(batch["batch_id"])["complete"]
        assert connection.execute("SELECT count(*) FROM workflow_attempts").fetchone()[0] == 31
    finally:
        connection.close()


def test_resume_preserves_finite_and_far_future_retry_times_and_failed_jobs(tmp_path):
    connection = open_database(tmp_path)
    try:
        seed(connection, 3)
        install_batches(connection)
        with connection:
            connection.execute("UPDATE workflow_jobs SET available_at=4102444800 WHERE job_id='job-001'")
            connection.execute("UPDATE workflow_jobs SET status='failed',last_error_code='budget_exhausted' WHERE job_id='job-002'")
        batches = BatchRepository(connection)
        batch = batches.create(["job-000", "job-001", "job-002"], actor="operator", reason="import explicit members")
        before = [tuple(row) for row in connection.execute("SELECT * FROM workflow_jobs")]
        batches.set_state(batch["batch_id"], paused=True, actor="operator", reason="pause")
        batches.set_state(batch["batch_id"], paused=False, desired_workers=20, actor="operator", reason="resume")
        assert [tuple(row) for row in connection.execute("SELECT * FROM workflow_jobs")] == before
        assert batches.status(batch["batch_id"])["retry_delayed"] == 1
        assert batches.status(batch["batch_id"])["failed"] == 1
        with pytest.raises(sqlite3.IntegrityError):
            batches.create(["job-001"], actor="operator", reason="duplicate batch membership")
        assert connection.execute("SELECT count(*) FROM workflow_batches").fetchone()[0] == 1
    finally:
        connection.close()


def test_competing_connections_obey_batch_concurrency_without_duplicate_claims(tmp_path):
    with closing(open_database(tmp_path)) as connection:
        seed(connection, 2)
        install_batches(connection)
        BatchRepository(connection).create(["job-000", "job-001"], desired_workers=1, actor="operator", reason="race")
    barrier = Barrier(2)

    def claim(worker):
        with closing(connect_database(tmp_path / "archive.db")) as connection:
            repository = WorkflowRepository(connection)
            barrier.wait(timeout=10)
            return repository.claim(worker)

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(claim, ["a", "b"]))
    assert sum(job is not None for job in jobs) == 1


def test_extract_one_job_does_not_unpause_source_or_recreate_attempts(tmp_path):
    with closing(open_database(tmp_path)) as connection:
        seed(connection, 3)
        install_batches(connection)
        batches, workflow = BatchRepository(connection), WorkflowRepository(connection)
        source = batches.create(["job-000", "job-001", "job-002"], actor="operator", reason="initial")
        batches.set_state(source["batch_id"], paused=True, actor="operator", reason="pause")
        selected = batches.extract(source["batch_id"], ["job-001"], actor="operator", reason="one job")
        job = workflow.claim("single")
        assert job.job_id == "job-001"
        workflow.finish(job.job_id, worker_id="single")
        assert batches.status(selected["batch_id"])["complete"]
        assert batches.status(source["batch_id"])["paused"] == 2
        assert workflow.claim("blocked") is None
        assert connection.execute("SELECT count(*) FROM workflow_attempts").fetchone()[0] == 1


def test_changed_member_payload_fails_before_attempt_or_paid_execution(tmp_path):
    with closing(open_database(tmp_path)) as connection:
        seed(connection, 1)
        install_batches(connection)
        BatchRepository(connection).create(["job-000"], actor="operator", reason="frozen input")
        with connection:
            connection.execute("UPDATE workflow_jobs SET payload_json='{" + '"changed":true' + "}'")
        with pytest.raises(ValueError, match="frozen configuration"):
            WorkflowRepository(connection).claim("worker")
        assert connection.execute("SELECT count(*) FROM workflow_attempts").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM workflow_jobs").fetchone()[0] == "queued"


def test_cli_install_create_status_pause_extract_and_resume(tmp_path, capsys):
    from bili_asr.cli import main
    with closing(open_database(tmp_path)) as connection:
        seed(connection, 3)
    assert main(["archive", "install-workflow-batches", "--archive-root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["installed"]
    common = ["workflow", "batch", "--archive-root", str(tmp_path)]
    actor = ["--actor", "operator", "--reason", "acceptance"]
    assert main([*common, "create", "--job-id", "job-000", "--job-id", "job-001", *actor]) == 0
    batch = json.loads(capsys.readouterr().out)["batch_id"]
    for action in ("status", "pause", "resume", "pause"):
        assert main([*common, action, "--batch-id", batch, *(actor if action != "status" else [])]) == 0
        assert json.loads(capsys.readouterr().out)["batch_id"] == batch
    assert main([*common, "extract", "--batch-id", batch, "--job-id", "job-001", *actor]) == 0
    assert json.loads(capsys.readouterr().out)["desired_workers"] == 1
    assert main([*common, "resume", "--batch-id", batch, "--workers", "20", "--dry-run", *actor]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["state"] == "paused" and preview["requested_workers"] == 20
    with closing(connect_database(tmp_path / "archive.db")) as connection:
        assert BatchRepository(connection).status(batch)["state"] == "paused"


def test_extension_validation_and_invalid_operations_fail_closed(tmp_path):
    with closing(open_database(tmp_path)) as connection:
        assert not require_batches(connection)
        with pytest.raises(SchemaContractError, match="explicitly install"):
            BatchRepository(connection)
        seed(connection, 2)
        connection.execute("BEGIN")
        with pytest.raises(ValueError, match="own transaction"):
            install_batches(connection)
        connection.rollback()
        install_batches(connection)
        assert install_batches(connection)["reused"]
        batches = BatchRepository(connection)
        for ids, workers in (([], 10), (["job-000"], 0), (["missing"], 1)):
            with pytest.raises(ValueError):
                batches.create(ids, desired_workers=workers, actor="operator", reason="invalid")
        with pytest.raises(ValueError, match="actor"):
            batches.create(["job-000"], actor=" ", reason="invalid")
        batch = batches.create(["job-000"], actor="operator", reason="valid")["batch_id"]
        with pytest.raises(ValueError, match="unknown batch"):
            batches.status("missing")
        with pytest.raises(ValueError, match="invalid batch"):
            batches.set_state(batch, paused="yes", actor="operator", reason="invalid")
        with pytest.raises(ValueError, match="paused source"):
            batches.extract(batch, ["job-000"], actor="operator", reason="invalid")
        batches.set_state(batch, paused=True, actor="operator", reason="pause")
        with pytest.raises(ValueError, match="belong to the source"):
            batches.extract(batch, ["job-001"], actor="operator", reason="invalid")
        with pytest.raises(ValueError, match="valid desired"):
            batches.extract(batch, [], actor="operator", reason="invalid")
        connection.execute("UPDATE workflow_batch_contract SET version=1")
        connection.execute("DELETE FROM workflow_batch_contract")
        with pytest.raises(SchemaContractError, match="marker"):
            require_batches(connection)
        connection.rollback()
        connection.execute("DROP INDEX workflow_batch_members_batch")
        with pytest.raises(SchemaContractError, match="missing or altered"):
            require_batches(connection)
