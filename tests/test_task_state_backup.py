"""Inactive evidence backups coexist with idle workers and never revive tasks."""
import copy
import json
import sqlite3
import zipfile
from dataclasses import asdict
from pathlib import Path

import pytest

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.services import maintenance_package_core as package
from bili_asr.services import maintenance_task_state as state
from bili_asr.services.archive_snapshot import SnapshotError, save_snapshot
from bili_asr.services.retry_maintenance_io import (
    MaintenanceBusyError,
    maintenance_lock,
)
from bili_asr.storage.maintenance_selection import table_ledger
from bili_asr.storage.migration_source import typed_table_fingerprint
from tests.fixtures.frozen_migration_archive import frozen_archive


def configured(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    controls = tmp_path / "controls"
    controls.mkdir()
    path = controls / "retry.json"
    value = {"format": "retry-control/v1", "schema_version": 1,
             "retry_holds": {"held": {"reason": "preserve original investigation"}},
             "retry_ledger": {"charged": {"retries": 2, "last_retry_at": 100, "next_retry_at": 200,
                                             "max_retries": 2, "reserved_attempt_id": "original-reservation",
                                             "last_error_code": "timeout", "dispatch_outcome": "reserved"}}}
    path.write_text(json.dumps(value, ensure_ascii=False, indent=3))
    component = {"kind": "retry-control/v1", "path": str(path), "lock_path": str(controls / "retry.lock")}
    return source, path, component


def rewrite(snapshot, output, modify, *, extra=None, payload_change=None):
    with zipfile.ZipFile(snapshot) as incoming:
        manifest = json.loads(incoming.read(state.NAME))
        files = {entry.filename: incoming.read(entry) for entry in incoming.infolist() if entry.filename != state.NAME}
    modify(manifest)
    manifest["content_id"] = package.digest({k: v for k, v in manifest.items() if k not in {"content_id", "created_at"}})
    if payload_change:
        files["archive.db"] = b"tampered bytes"
    with zipfile.ZipFile(output, "w") as bundle:
        for name, data in files.items():
            bundle.writestr(name, data)
        bundle.writestr(state.NAME, package.canonical(manifest))
        if extra:
            bundle.writestr(extra, b"undeclared")
    return output


def test_idle_write_session_can_capture_wal_and_running_tasks_without_media_or_actions(tmp_path):
    source, controls, component = configured(tmp_path)
    raw = controls.read_bytes()
    with ArchiveSession(source, mode=ArchiveAccessMode.WRITE).open() as writer:
        writer.connection.execute("PRAGMA journal_mode=WAL")
        writer.connection.execute("UPDATE workflow_jobs SET status='running',lease_owner='real-worker',lease_expires_at=9999999999 WHERE job_id=(SELECT job_id FROM workflow_jobs LIMIT 1)")
        writer.connection.commit()
        before = []
        for (name,) in writer.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            columns = [row[1] for row in writer.connection.execute(f'PRAGMA table_info("{name}")')]
            before.append({**asdict(typed_table_fingerprint(writer.connection, name, columns)), "columns": columns})
        with pytest.raises(SnapshotError, match="active writer"):
            save_snapshot(source, tmp_path / "full.zip")
        result = state.save_task_state(source, tmp_path / "state.zip", external_components=[component])
        assert result["valid"] and not result["media_self_contained"]
        assert writer.connection.execute("SELECT count(*) FROM workflow_jobs WHERE status='running'").fetchone()[0] == 1
        assert result["manifest"]["tables"] == before
    checked = state.verify_task_state(tmp_path / "state.zip")
    assert checked["manifest"]["tables"] == before
    assert not checked["restoration_verified"]
    with zipfile.ZipFile(tmp_path / "state.zip") as bundle:
        assert not any(key.startswith("audio/") for key in bundle.namelist())
        assert bundle.read("control-state/component-0.json") == raw
    assert controls.read_bytes() == raw


def test_cancelled_queued_failed_and_retry_budget_remain_original_frozen_facts(tmp_path):
    source, controls, component = configured(tmp_path)
    with sqlite3.connect(source / "archive.db") as connection:
        jobs = [row[0] for row in connection.execute("SELECT job_id FROM workflow_jobs LIMIT 3")]
        for identity, status in zip(jobs, ("cancelled", "queued", "failed"), strict=True):
            connection.execute("UPDATE workflow_jobs SET status=? WHERE job_id=?", (status, identity))
    before = table_ledger(source / "archive.db")
    state.save_task_state(source, tmp_path / "state.zip", external_components=[component])
    stage = tmp_path / "reader"
    stage.mkdir()
    manifest = state.extract_task_state(tmp_path / "state.zip", stage)
    assert table_ledger(stage / "archive.db") == before == manifest["tables"]
    with sqlite3.connect((stage / "archive.db").as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
        assert {row[0] for row in connection.execute("SELECT status FROM workflow_jobs WHERE job_id IN (?,?,?)", jobs)} == {"cancelled", "queued", "failed"}
    assert (stage / "control-state/component-0.json").read_bytes() == controls.read_bytes()


def test_no_control_declaration_unknown_versions_and_controller_lock_fail_closed(tmp_path):
    source, controls, component = configured(tmp_path)
    with pytest.raises(ValueError, match="declare typed"):
        state.save_task_state(source, tmp_path / "undeclared.zip")
    with maintenance_lock(Path(component["lock_path"])), pytest.raises(MaintenanceBusyError):
        state.save_task_state(source, tmp_path / "held.zip", external_components=[component])
    value = json.loads(controls.read_bytes())
    value["schema_version"] = 2
    controls.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="version"):
        state.save_task_state(source, tmp_path / "unknown.zip", external_components=[component])
    assert not list(tmp_path.glob("*.zip"))


@pytest.mark.parametrize("components", [None, ["raw"], [{"kind": "unknown", "path": "/source"}],
    [{"kind": "retry-control/v1", "path": "relative", "lock_path": "/lock"}],
    [{"kind": "retry-control/v1", "path": "/same", "lock_path": "/same"}]])
def test_component_declarations_cannot_guess_layout_or_lock(components):
    with pytest.raises(ValueError):
        state.validate_components(components)


def test_component_changed_during_capture_is_refused_even_when_layout_still_valid(tmp_path, monkeypatch):
    source, controls, component = configured(tmp_path)
    original = state._database
    def drift(*args):
        original(*args)
        value = json.loads(controls.read_bytes())
        value["retry_ledger"]["charged"]["retries"] += 1
        controls.write_text(json.dumps(value))
    monkeypatch.setattr(state, "_database", drift)
    with pytest.raises(ValueError, match="changed"):
        state.save_task_state(source, tmp_path / "drift.zip", external_components=[component])
    assert not (tmp_path / "drift.zip").exists()


@pytest.mark.parametrize("change", [
    lambda m: m.update(format="unknown"), lambda m: m.update(runtime_contract="future"),
    lambda m: m.update(runtime_contract="universal-v2"),
    lambda m: m.update(inactive_archive=False), lambda m: m["container"].update(encryption="aes"),
    lambda m: m["observation"].update(sqlite_snapshot_sha256="0" * 64),
    lambda m: m["observation"].update(finished_at="not-time"),
    lambda m: m["observation"].update(finished_at="2020-01-01T00:00:00+00:00"),
    lambda m: m["source"].update(database_schema_sha256="sha256:" + "0" * 64),
    lambda m: m.update(created_at="2020-01-01T00:00:00+00:00"),
    lambda m: m["observation"]["component_versions"][0].update(path="../outside.json"),
    lambda m: m["observation"]["component_versions"][0].update(path="/tmp/outside.json"),
    lambda m: m.update(no_external_control_state=True),
    lambda m: m["tables"][0].update(row_count=m["tables"][0]["row_count"]+1),
    lambda m: m["observation"]["component_versions"].append(copy.deepcopy(m["observation"]["component_versions"][0])),
])
def test_rehashed_task_state_manifest_cannot_change_capture_authority(tmp_path, change):
    source, _controls, component = configured(tmp_path)
    original = tmp_path / "state.zip"
    state.save_task_state(source, original, external_components=[component])
    damaged = rewrite(original, tmp_path / "damaged.zip", change)
    with pytest.raises(ValueError):
        state.verify_task_state(damaged)


def test_unlisted_or_tampered_payload_and_nonempty_reader_stage_refuse(tmp_path):
    source, _controls, component = configured(tmp_path)
    original = tmp_path / "state.zip"
    state.save_task_state(source, original, external_components=[component])
    for name, options in (("extra", {"extra": "audio/unlisted.wav"}), ("bytes", {"payload_change": True})):
        damaged = rewrite(original, tmp_path / f"{name}.zip", lambda _: None, **options)
        with pytest.raises(ValueError):
            state.verify_task_state(damaged)
    with pytest.raises(ValueError, match="empty directory"):
        state.extract_task_state(original, source)


def test_evidence_missing_or_not_matching_reference_refuses_publish(tmp_path):
    source, _controls, component = configured(tmp_path)
    audio = next((source / "audio").iterdir())
    audio.unlink()
    with pytest.raises(ValueError, match="dependency is missing"):
        state.save_task_state(source, tmp_path / "missing.zip", external_components=[component])


def test_invalid_timeout_full_source_output_and_duplicate_components_refuse(tmp_path):
    source, _controls, component = configured(tmp_path)
    with pytest.raises(ValueError, match="timeout"):
        state.save_task_state(source, tmp_path / "timeout.zip", external_components=[component], timeout_seconds=0)
    with pytest.raises(ValueError, match="outside"):
        state.save_task_state(source, source / "output.zip", external_components=[component])
    with pytest.raises(ValueError, match="duplicate"):
        state.validate_components([component, component])


@pytest.mark.parametrize("payload", [{"schema_version": 2}, {"schema_version": True}, ["unknown layout"]])
def test_unknown_workflow_version_cannot_be_declared_verified_evidence(tmp_path, payload):
    source, _controls, component = configured(tmp_path)
    with sqlite3.connect(source / "archive.db") as connection:
        connection.execute("UPDATE workflow_jobs SET payload_json=? WHERE job_id=(SELECT job_id FROM workflow_jobs LIMIT 1)", (json.dumps(payload),))
    with pytest.raises(ValueError, match="workflow payload version"):
        state.save_task_state(source, tmp_path / "future.zip", external_components=[component])
    assert not (tmp_path / "future.zip").exists()


def test_changed_audio_identity_after_database_observation_is_not_published(tmp_path, monkeypatch):
    source, _controls, component = configured(tmp_path)
    observe = state._observe
    count = 0
    def changed(components):
        nonlocal count
        count += 1
        result = observe(components)
        if count == 2:
            next((source / "audio").iterdir()).write_bytes(b"different recorded media")
        return result
    monkeypatch.setattr(state, "_observe", changed)
    with pytest.raises(ValueError, match="audio identity changed"):
        state.save_task_state(source, tmp_path / "unstable.zip", external_components=[component])
    assert not (tmp_path / "unstable.zip").exists()


@pytest.mark.parametrize("key,value", [("retry_ledger", {"charged": {"retries": -1, "last_retry_at": 100}}),
                                     ("retry_holds", {"held": ["unknown hold layout"]})])
def test_untyped_hold_or_budget_cannot_pass_component_validation(tmp_path, key, value):
    source, controls, component = configured(tmp_path)
    document = json.loads(controls.read_bytes())
    document[key] = value
    controls.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="retry"):
        state.save_task_state(source, tmp_path / "invalid.zip", external_components=[component])


def test_absent_task_manifest_is_actionable_validation_failure(tmp_path):
    damaged = tmp_path / "no-manifest.zip"
    with zipfile.ZipFile(damaged, "w") as bundle:
        bundle.writestr("archive.db", b"not a state archive")
    with pytest.raises(ValueError, match="exactly one manifest"):
        state.verify_task_state(damaged)
