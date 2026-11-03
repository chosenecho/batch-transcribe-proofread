"""State scheduling preserves live jobs and only prunes its own inactive evidence."""
import json
import sqlite3
import shutil
from pathlib import Path

import pytest

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.services import maintenance_backup as backup
from bili_asr.services import maintenance_package_core as package
from bili_asr.services.maintenance_state_retention import retain_state_packages
from bili_asr.services.maintenance_task_state import verify_task_state
from bili_asr.storage.maintenance_selection import table_ledger
from tests.fixtures.frozen_migration_archive import frozen_archive


def setup_policy(tmp_path, **kwargs):
    source = tmp_path / "source"
    frozen_archive(source)
    return backup.policy(source, tmp_path / "repository", profile="task-state", mode="full",
                         no_external_control_state=True, interval_seconds=1, keep_days=0, **kwargs)


def test_idle_writer_state_backup_and_restart_keep_exact_evidence(tmp_path):
    policy = setup_policy(tmp_path, keep_count=2)
    source = Path(policy["source_root"])
    before = table_ledger(source / "archive.db")
    with ArchiveSession(source, mode=ArchiveAccessMode.WRITE).open():
        first = backup.run_backup(policy, now=100)
        assert first["valid"] and first["state_evidence_complete"], first.get('error', first)
        assert first["worker_started"] is False and first["restoration_verified"] is False
        reused = backup.run_backup(json.loads(json.dumps(policy)), now=100)
        assert reused["reused"] and reused["run_id"] == first["run_id"]
        event = backup.run_backup(policy, trigger="event", event_id="job-failed-1", now=101)
        assert event["valid"]
        assert backup.run_backup(policy, trigger="event", event_id="job-failed-1", now=102)["skipped"] == "event_already_backed_up"
        assert backup.run_backup(policy, trigger="time", now=101)["skipped"] == "not_due"
        last = backup.run_backup(policy, now=103)
        assert last["valid"]
    assert table_ledger(source / "archive.db") == before
    repository = Path(policy["repository"])
    assert not (repository / first["snapshot"]).exists()
    assert (repository / event["snapshot"]).exists()
    verified = verify_task_state(repository / last["snapshot"])
    assert verified["manifest"]["tables"] == before
    assert verified["manifest"]["backup_job_state"]["current_run"]["run_id"] == last["run_id"]
    assert len(backup.backup_status(repository, policy["policy_id"])["runs"]) == 3
    assert "manifest" not in last["verification"]


def test_state_retention_leaves_foreign_packages_and_receipts(tmp_path):
    policy = setup_policy(tmp_path, keep_count=1)
    first = backup.run_backup(policy, now=100)
    root = Path(policy["repository"])
    foreign = root / "unregistered-private.zip"
    foreign.write_bytes((root / first["snapshot"]).read_bytes())
    second = backup.run_backup(policy, now=101)
    assert second["valid"] and foreign.exists()
    receipts = backup.backup_status(root, policy["policy_id"])["runs"]
    assert len(receipts) == 2
    assert retain_state_packages(root, policy, receipts, now=101)["valid"]
    for change, message in (({"snapshot": "../evil.zip"}, "unsafe"),
                            ({"snapshot": "wrong.zip"}, "own"),
                            ({"policy_id": "0" * 64}, "own")):
        with pytest.raises(ValueError, match=message):
            retain_state_packages(root, policy, [{**second, **change}], now=101)


def test_state_failure_alert_and_corrupt_published_resume_never_report_success(tmp_path, monkeypatch):
    policy = setup_policy(tmp_path, alert_after=2)
    root = Path(policy["repository"])
    first = backup.run_backup(policy, now=100)
    assert first["valid"], first.get('error', first)
    from bili_asr.services import maintenance_task_state
    def no_space(*args, **kwargs):
        raise OSError("No space left on device")
    monkeypatch.setattr(maintenance_task_state, "save_task_state", no_space)
    failed = backup.run_backup(policy, now=101)
    assert failed["notification"] == "backup_failure"
    alerted = backup.run_backup(policy, now=102)
    assert alerted["notification"] == "backup_failure_alert"
    state = package._json(root / "runs" / policy["policy_id"] / "state.json")
    assert state["latest"] == first["snapshot"] and state["last_success"] == 100
    (root / first["snapshot"]).write_bytes(b"corrupt")
    with pytest.raises(Exception):
        backup.run_backup(policy, now=100)


def test_state_policy_requires_explicit_controls_and_stored_full_history(tmp_path):
    source, repository = tmp_path / "source", tmp_path / "repository"
    for kwargs in ({"profile": "unknown"}, {"no_external_control_state": True},
                   {"profile": "task-state", "mode": "full"},
                   {"profile": "task-state", "no_external_control_state": True},
                   {"profile": "task-state", "mode": "full", "external_components": ["bad"]},
                   {"profile": "task-state", "mode": "full", "external_components": [{"kind": "unknown", "path": str(source)}]},
                   {"profile": "task-state", "mode": "full", "external_components": [{"kind": "retry-control/v1", "path": "relative"}]}):
        with pytest.raises(ValueError):
            backup.policy(source, repository, **kwargs)
    ops = tmp_path / "ops"
    ops.mkdir()
    control = ops / "control.json"
    control.write_text(json.dumps({"format": "retry-control/v1", "schema_version": 1,
                                  "retry_holds": {"held": "quality"},
                                  "retry_ledger": {"spent": {"retries": 1, "last_retry_at": 100}}}))
    frozen_archive(source)
    policy = backup.policy(source, repository, mode="full", profile="task-state",
                           external_components=[{"kind": "retry-control/v1", "path": str(control), "lock_path": str(ops / 'control.lock')}])
    result = backup.run_backup(policy, now=100)
    assert result["valid"], result.get('error', result)
    manifest = verify_task_state(repository / result["snapshot"])["manifest"]
    assert manifest["observation"]["component_versions"][0]["sha256"] == package._record("x", control)["sha256"]


def test_running_cancelled_paused_jobs_are_history_without_reactivation(tmp_path):
    policy = setup_policy(tmp_path)
    source = Path(policy["source_root"])
    with sqlite3.connect(source / "archive.db") as conn:
        conn.execute("UPDATE workflow_jobs SET status='running',lease_owner='old-worker',lease_expires_at=99999999999 WHERE job_id=(SELECT job_id FROM workflow_jobs LIMIT 1)")
    before = table_ledger(source / "archive.db")
    result = backup.run_backup(policy, now=100)
    assert result["valid"] and result["restoration_verified"] is False
    assert table_ledger(source / "archive.db") == before


def test_atomic_state_publication_resumes_after_receipt_interrupt(tmp_path, monkeypatch):
    policy = setup_policy(tmp_path)
    save = backup._save
    def interrupted(path, document):
        if document.get("status") == "success":
            raise OSError("simulated completed-receipt interruption")
        save(path, document)
    monkeypatch.setattr(backup, "_save", interrupted)
    with pytest.raises(OSError, match="interruption"):
        backup.run_backup(policy, now=100)
    root = Path(policy["repository"])
    published = next(root.glob("*.zip"))
    frozen = package._record("published", published)
    monkeypatch.setattr(backup, "_save", save)
    resumed = backup.run_backup(policy, now=100)
    assert resumed["valid"]
    assert package._record("published", published) == frozen


def test_other_invocation_or_policy_package_cannot_be_adopted(tmp_path):
    policy = setup_policy(tmp_path)
    first = backup.run_backup(policy, now=100)
    root = Path(policy["repository"])
    from datetime import UTC, datetime
    identity = package.digest({"policy": policy["policy_id"], "timestamp": 101, "trigger": "manual", "event": None})
    name = datetime.fromtimestamp(101, UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + identity[:16] + ".zip"
    shutil.copyfile(root / first["snapshot"], root / name)
    result = backup.run_backup(policy, now=101)
    assert not result["valid"] and "different invocation" in result["error"]
    other = {**policy, "policy_id": "0" * 64}
    with pytest.raises(ValueError, match="foreign policy"):
        retain_state_packages(root, other, [{**first, "policy_id": other["policy_id"]}], now=101)
    (root / name).unlink()
    recovered = backup.run_backup(policy, now=102)
    assert recovered["valid"]
    # Previous failed receipts remain auditable but are never deletion inputs.
    status = backup.backup_status(root, policy["policy_id"])
    assert any(run["status"] == "failed" for run in status["runs"])


def test_scheduler_restart_preserves_interrupted_running_receipt(tmp_path, monkeypatch):
    policy = setup_policy(tmp_path)
    original = backup._run_task_state
    def crash(*args, **kwargs):
        raise RuntimeError("simulated process death after durable start")
    monkeypatch.setattr(backup, "_run_task_state", crash)
    with pytest.raises(RuntimeError, match="process death"):
        backup.run_backup(policy, now=100)
    monkeypatch.setattr(backup, "_run_task_state", original)
    result = backup.run_backup(policy, now=101)
    assert result["valid"], result.get("error", result)
    manifest = verify_task_state(Path(policy["repository"]) / result["snapshot"])["manifest"]
    prior = manifest["backup_job_state"]["prior_runs"]
    assert len(prior) == 1 and prior[0]["status"] == "running" and prior[0]["started_at"] == 100


def test_retention_failure_keeps_published_success_and_recovery_pointer(tmp_path, monkeypatch):
    from bili_asr.services import maintenance_state_retention
    policy = setup_policy(tmp_path, keep_count=1)
    first = backup.run_backup(policy, now=100)
    def unavailable(*args, **kwargs):
        raise OSError("retention filesystem unavailable")
    monkeypatch.setattr(maintenance_state_retention, "retain_state_packages", unavailable)
    second = backup.run_backup(policy, now=101)
    assert second["valid"] and second["status"] == "success"
    assert second["notification"] == "backup_retention_failure" and not second["retention"]["valid"]
    root = Path(policy["repository"])
    assert (root / first["snapshot"]).exists() and (root / second["snapshot"]).exists()
    assert package._json(root / "runs" / policy["policy_id"] / "state.json")["latest"] == second["snapshot"]


def test_cli_task_policy_and_invalid_component_options(tmp_path, capsys):
    from tests.test_maintenance_cli_review import invoke
    source = tmp_path / "source"
    frozen_archive(source)
    args = ("maintenance", "backup-policy", "--source-root", source,
            "--repository", tmp_path / "backups", "--profile", "task-state", "--mode", "full")
    output = tmp_path / "policy.json"
    code, result, _ = invoke(capsys, *args, "--no-external-control-state", "--output", output)
    assert code == 0 and result["profile"] == "task-state"
    assert invoke(capsys, "maintenance", "backup-run", "--policy", output)[1]["state_evidence_complete"]
    code, result, _ = invoke(capsys, *args, "--status", "failed", "--output", tmp_path / "bad-scope.json")
    assert code == 1 and not result["valid"]
    declarations = tmp_path / "declarations.json"
    declarations.write_text('{"unsupported":[]}')
    code, result, _ = invoke(capsys, *args, "--external-components", declarations, "--output", tmp_path / "bad-components.json")
    assert code == 1 and not result["valid"]
    declarations.write_text(json.dumps({"components": [{"kind": "retry-control/v1",
                            "path": str(tmp_path / "ops" / "controls.json"),
                            "lock_path": str(tmp_path / "ops" / "controls.lock")}]}))
    code, result, _ = invoke(capsys, *args, "--external-components", declarations, "--output", tmp_path / "typed-policy.json")
    assert code == 0 and len(result["external_components"]) == 1
