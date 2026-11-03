"""Backup automation checkpoints safely and retains auditable results."""
import sqlite3

import pytest

from bili_asr.services import maintenance_backup as backup
from bili_asr.services import maintenance_chain as chain
from tests.fixtures.frozen_migration_archive import frozen_archive


def test_checkpoint_committed_wal_preserves_rows_and_rejects_running_lease(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    connection = sqlite3.connect(source / "archive.db")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("UPDATE videos SET updated_at=updated_at+1")
    connection.commit()
    before = connection.execute("SELECT bvid,updated_at FROM videos ORDER BY bvid").fetchall()
    assert (source / "archive.db-wal").stat().st_size > 0
    assert backup.checkpoint_archive(source)["valid"]
    assert (source / "archive.db-wal").stat().st_size == 0
    assert connection.execute("SELECT bvid,updated_at FROM videos ORDER BY bvid").fetchall() == before
    connection.execute("UPDATE workflow_jobs SET status='running',lease_owner='active',lease_expires_at=99999999999 WHERE job_id=(SELECT job_id FROM workflow_jobs LIMIT 1)")
    connection.commit()
    with pytest.raises(ValueError, match="unfinished execution"):
        backup.checkpoint_archive(source)
    connection.close()


def test_backup_manual_time_event_incremental_receipts_and_failure_alert(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    repository = tmp_path / "repository"
    policy = backup.policy(source, repository, interval_seconds=10, alert_after=2, keep_count=2, keep_days=0)
    first = backup.run_backup(policy, trigger="manual", now=100)
    assert first["valid"] and first["restoration_verified"]
    assert backup.run_backup(policy, trigger="time", now=105)["skipped"] == "not_due"
    second = backup.run_backup(policy, trigger="event", event_id="released-1", now=110)
    assert second["valid"] and len(second["verification"]["chain"]) == 2
    assert backup.run_backup(policy, trigger="event", event_id="released-1", now=111)["skipped"] == "event_already_backed_up"
    assert backup.backup_status(repository, policy["policy_id"])["runs"][0]["receipt_id"]
    assert chain.verify_chain(repository / second["snapshot"])["valid"]
    with sqlite3.connect(source / "archive.db") as connection:
        connection.execute("UPDATE workflow_jobs SET status='running',lease_owner='active',lease_expires_at=99999999999 WHERE job_id=(SELECT job_id FROM workflow_jobs LIMIT 1)")
    failed = backup.run_backup(policy, now=120)
    assert failed["notification"] == "backup_failure"
    failed = backup.run_backup(policy, now=121)
    assert failed["notification"] == "backup_failure_alert"
    with sqlite3.connect(source / "archive.db") as connection:
        assert connection.execute("SELECT count(*) FROM workflow_jobs WHERE status='running'").fetchone()[0] == 1


def test_backup_rejects_writer_and_policy_drift_without_partial_snapshot(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    repository = tmp_path / "repository"
    policy = backup.policy(source, repository)
    connection = sqlite3.connect(source / "archive.db")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("UPDATE videos SET updated_at=updated_at+1")
    result = backup.run_backup(policy, now=100)
    assert not result["valid"]
    assert not list(repository.glob("*.zip"))
    connection.rollback()
    connection.close()
    policy["keep_count"] = 0
    with pytest.raises(ValueError, match="identity"):
        backup.run_backup(policy, now=101)


def test_incremental_automation_rolls_full_before_chain_depth_limit(tmp_path, monkeypatch):
    from bili_asr.services import maintenance_chain
    monkeypatch.setattr(maintenance_chain, "MAX_CHAIN_POINTS", 3)
    source = tmp_path / "source"
    frozen_archive(source)
    policy = backup.policy(source, tmp_path / "backups", interval_seconds=1, keep_count=1, keep_days=0)
    results = [backup.run_backup(policy, now=value) for value in (100, 110, 120, 130, 140)]
    assert all(result["valid"] for result in results)
    assert [len(result["verification"]["chain"]) for result in results] == [1, 2, 3, 1, 2]
    assert [result["rolled_full_baseline"] for result in results] == [False, False, False, True, False]
