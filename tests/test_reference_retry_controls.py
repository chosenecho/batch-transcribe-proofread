"""Portable controls retain holds and spent budget through real recovery paths."""
import hashlib
import json
import time
import zipfile
from pathlib import Path

import pytest

from bili_asr.services import maintenance_backup as backup
from bili_asr.services import reference_backup as reference
from bili_asr.services import retry_control as controls
from bili_asr.services.artifact_catalog_upgrade import authority_fingerprints
from bili_asr.services.maintenance_task_state import extract_task_state
from bili_asr.services.retry_maintenance import tick
from bili_asr.services.retry_maintenance_io import (
    MaintenanceBusyError,
    maintenance_lock,
    write_json,
)
from bili_asr.services.retry_preflight import preflight
from bili_asr.services.workflow_retry import load_control_state
from bili_asr.storage.database import connect_database
from tests.test_artifact_restore import _setup


def configured(tmp_path, case="held"):
    roots, *_ = _setup(tmp_path)
    now = int(time.time()) + 10
    with connect_database(roots.archive_root / "archive.db") as connection:
        job = connection.execute("SELECT job_id FROM workflow_jobs WHERE kind='audio' LIMIT 1").fetchone()[0]
        connection.execute("UPDATE workflow_jobs SET status='failed',last_error_code='TimeoutError' WHERE job_id=?", (job,))
        connection.execute("UPDATE workflow_attempts SET outcome='failed',error_code='TimeoutError' WHERE job_id=?", (job,))
        connection.commit()
        original = authority_fingerprints(connection)
    operational = {"retry_holds": {}, "retry_ledger": {}, "credentials": "must-not-enter-backup"}
    if case == "held":
        operational["retry_holds"][job] = {"reason": "publication contract investigation", "source": "operator",
                                          "paused": True, "investigation_ref": "investigation-1", "held_at": now - 10}
        operational["retry_ledger"][job] = {"retries": 3, "max_retries": 3, "last_retry_at": now - 1}
    elif case == "exhausted":
        operational["retry_ledger"][job] = {"retries": 3, "max_retries": 3, "last_retry_at": now - 1}
    elif case == "backoff":
        operational["retry_ledger"][job] = {"retries": 0, "max_retries": 3, "last_retry_at": 0, "next_retry_at": now + 1000}
    owner = tmp_path / "controls"
    owner.mkdir()
    source = owner / "state.json"
    write_json(source, operational)
    policy = {"schema_version": 1, "job_ids": [job], "part_ids": [], "kinds": ["audio"],
              "retryable_errors": {"audio": ["TimeoutError"]}, "max_retries": 3,
              "delays_seconds": [0, 0, 0], "interval_seconds": 30}
    policy_path = owner / "policy.json"
    write_json(policy_path, policy)
    files = {"policy_path": policy_path, "state_path": source, "lock_path": owner / "owner.lock", "health_path": owner / "health.json"}
    return roots, files, now, original


@pytest.mark.parametrize("case,reason", [("held", "held"), ("exhausted", "budget_exhausted"), ("backoff", "backoff")])
def test_reference_roundtrip_preflight_agrees_and_never_revives_attempts(tmp_path, case, reason):
    roots, files, now, original = configured(tmp_path, case)
    raw = files["state_path"].read_bytes()
    before = preflight(archive_root=roots.archive_root, **files, clock=lambda: now)
    assert before["events"][0]["reason"] == reason
    output = tmp_path / "portable.zip"
    saved = reference.save_reference_backup(roots, output, retry_control_path=files["state_path"], retry_control_lock=files["lock_path"])
    assert saved["format"] == "archive-reference-backup/v2"
    assert saved["control_recovery_capability"] == "scope-bound-retry-controls"
    with zipfile.ZipFile(output) as bundle:
        assert b"must-not-enter-backup" not in bundle.read(reference.MANIFEST)
        document = json.loads(bundle.read(reference.MANIFEST))["operational_state"]["retry_control"]
    assert document["source"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert document["retry_ledger"] == json.loads(raw)["retry_ledger"]
    assert document["retry_holds"] == json.loads(raw)["retry_holds"]
    target = tmp_path / "recovered"
    restored = reference.restore_reference_backup(output, target)
    control_path = Path(restored["control_state_path"])
    new_files = {**files, "state_path": control_path}
    after = preflight(archive_root=target, **new_files, clock=lambda: now)
    assert after["events"] == before["events"]
    assert after["retry_ledger_count"] == before["retry_ledger_count"]
    with connect_database(target / "archive.db", readonly=True) as connection:
        assert authority_fingerprints(connection) == original
        state = load_control_state(control_path, connection=connection, now=now)
        assert state.status == "known"
        assert state.retry_ledger == document["retry_ledger"]
    assert not restored["worker_started"] and not restored["retry_started"]
    assert restored["workflow_recovery_changes"] == []
    assert files["state_path"].read_bytes() == raw


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(schema_version=3),
    lambda d: d["source"].update(sha256="0" * 64),
    lambda d: d["clock"].update(domain="monotonic-process/v1"),
    lambda d: d["retry_ledger"][next(iter(d["retry_ledger"]))].update(retries=0),
    lambda d: d.update(password="secret"),
])
def test_control_damage_fails_closed_before_install_or_retry(tmp_path, mutation):
    roots, files, _now, _ = configured(tmp_path)
    backup = tmp_path / "source.zip"
    reference.save_reference_backup(roots, backup, retry_control_path=files["state_path"], retry_control_lock=files["lock_path"])
    with zipfile.ZipFile(backup) as incoming:
        members = {entry.filename: incoming.read(entry) for entry in incoming.infolist()}
    manifest = json.loads(members[reference.MANIFEST])
    mutation(manifest["operational_state"]["retry_control"])
    members[reference.MANIFEST] = json.dumps(manifest).encode("utf-8")
    damaged = tmp_path / "damaged.zip"
    with zipfile.ZipFile(damaged, "w") as output:
        for key, value in members.items():
            output.writestr(key, value)
    target = tmp_path / "not-installed"
    with pytest.raises(ValueError):
        reference.restore_reference_backup(damaged, target)
    assert not target.exists()


def test_scope_clock_unknown_hold_fields_and_owner_lock_are_not_guessed(tmp_path):
    roots, files, now, _ = configured(tmp_path)
    source = files["state_path"]
    with maintenance_lock(files["lock_path"]), pytest.raises(MaintenanceBusyError):
        reference.save_reference_backup(roots, tmp_path / "locked.zip", retry_control_path=source, retry_control_lock=files["lock_path"])
    with pytest.raises(ValueError, match="declared coordination"):
        reference.save_reference_backup(roots, tmp_path / "no-lock.zip", retry_control_path=source)
    backup = tmp_path / "source.zip"
    reference.save_reference_backup(roots, backup, retry_control_path=source, retry_control_lock=files["lock_path"])
    with zipfile.ZipFile(backup) as bundle:
        document = json.loads(bundle.read(reference.MANIFEST))["operational_state"]["retry_control"]
    path = files["state_path"].parent / "portable.json"
    path.write_bytes(controls.encoded(document))
    with connect_database(roots.archive_root / "archive.db") as connection:
        with pytest.raises(ValueError, match="clock"):
            controls.validate_control(document, connection=connection, now=document["clock"]["captured_at"] - 1)
        with pytest.raises(ValueError, match="clock"):
            controls.validate_control(document, clock_domain="monotonic-process/v1")
        connection.execute("UPDATE workflow_jobs SET payload_json='{}' WHERE job_id=?", (document["archive_scope"]["jobs"][0]["job_id"],))
        with pytest.raises(ValueError, match="archive identity"):
            load_control_state(path, connection=connection, now=now)
        connection.rollback()
    value = json.loads(source.read_bytes())
    value["retry_holds"][next(iter(value["retry_holds"]))]["credentials"] = "reject nested secrets"
    write_json(source, value)
    with pytest.raises(ValueError, match="hold fields"):
        reference.save_reference_backup(roots, tmp_path / "unsafe.zip", retry_control_path=source, retry_control_lock=files["lock_path"])


def test_old_holds_only_remains_readable_and_explicitly_limited(tmp_path):
    roots, *_ = _setup(tmp_path)
    result = reference.save_reference_backup(roots, tmp_path / "legacy.zip", holds={"version": 1, "holds": {"legacy-job": ["keep historical investigation"]}})
    assert result["format"] == "archive-reference-backup/v1"
    assert result["control_recovery_capability"] == "limited-holds-only"
    assert result["retry_ledger_preserved"] is None


def test_explicit_dispatch_preserves_origin_and_charges_portable_ledger_before_requeue(tmp_path):
    roots, files, now, _ = configured(tmp_path, "eligible")
    output = tmp_path / "portable.zip"
    reference.save_reference_backup(roots, output, retry_control_path=files["state_path"], retry_control_lock=files["lock_path"])
    restored = reference.restore_reference_backup(output, tmp_path / "restored")
    path = Path(restored["control_state_path"])
    original = json.loads(path.read_bytes())
    new_files = {**files, "state_path": path}
    assert preflight(archive_root=tmp_path / "restored", **new_files, clock=lambda: now)["events"][0]["action"] == "would_requeue"
    result = tick(archive_root=tmp_path / "restored", **new_files, clock=lambda: now)
    assert result["events"][0]["action"] == "requeued"
    updated = json.loads(path.read_bytes())
    assert updated["source"] == original["source"]
    assert updated["clock"] == original["clock"]
    assert updated["archive_scope"] == original["archive_scope"]
    assert next(iter(updated["retry_ledger"].values()))["retries"] == 1
    controls.validate_control(updated)
    with connect_database(tmp_path / "restored/archive.db", readonly=True) as connection:
        assert load_control_state(path, connection=connection, now=now).status == "known"
        assert connection.execute("SELECT count(*) FROM workflow_attempts WHERE error_code='TimeoutError'").fetchone()[0] >= 1


def test_source_drift_during_backup_refuses_publication_and_preserves_new_hold(tmp_path, monkeypatch):
    roots, files, _now, _ = configured(tmp_path)
    original = reference.capture_control
    def drifting(*args, **kwargs):
        result = original(*args, **kwargs)
        state = json.loads(files["state_path"].read_bytes())
        state["retry_holds"][next(iter(state["retry_holds"]))]["reason"] = "new investigation must survive"
        write_json(files["state_path"], state)
        return result
    monkeypatch.setattr(reference, "capture_control", drifting)
    output = tmp_path / "never-published.zip"
    with pytest.raises(ValueError, match="changed during capture"):
        reference.save_reference_backup(roots, output, retry_control_path=files["state_path"], retry_control_lock=files["lock_path"])
    assert not output.exists()
    assert b"new investigation must survive" in files["state_path"].read_bytes()


def test_automatic_task_state_captures_v2_control_bytes_and_verifies_archive_scope(tmp_path):
    roots, files, _now, _ = configured(tmp_path)
    output = tmp_path / "reference.zip"
    reference.save_reference_backup(roots, output, retry_control_path=files["state_path"], retry_control_lock=files["lock_path"])
    with zipfile.ZipFile(output) as bundle:
        document = json.loads(bundle.read(reference.MANIFEST))["operational_state"]["retry_control"]
    portable = files["state_path"].parent / "portable.json"
    portable.write_bytes(controls.encoded(document))
    # The fixture offloaded audio; restore its exact bytes before task-state capture.
    from bili_asr.services.artifact_restore import restore_artifact
    with connect_database(roots.archive_root / "archive.db", readonly=True) as connection:
        audio = dict(connection.execute("SELECT * FROM audio_objects LIMIT 1").fetchone())
    original_target = tmp_path / "cold"
    restore_artifact(roots, audio["sha256"], target_id="cold", target_root=original_target, storage_key=audio["storage_key"])
    repository = tmp_path / "automatic-state"
    policy = backup.policy(roots.archive_root, repository, mode="full", profile="task-state", external_components=[
        {"kind": controls.FORMAT, "path": str(portable), "lock_path": str(files["lock_path"])}])
    saved = backup.run_backup(policy, now=int(time.time()))
    assert saved["valid"]
    stage = tmp_path / "reader"
    stage.mkdir()
    extract_task_state(repository / saved["snapshot"], stage)
    assert (stage / "control-state/component-0.json").read_bytes() == portable.read_bytes()
