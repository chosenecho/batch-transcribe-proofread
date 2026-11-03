"""Recovery planning is derived and read-only; application records row repairs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bili_asr.storage import open_database
from bili_asr.services.archive_snapshot import save_snapshot, restore_snapshot, SnapshotError
from bili_asr.services.archive_recovery import RuntimeProbe, inspect_snapshot, plan_restore, doctor_archive


PROBE = RuntimeProbe(frozenset({"ffmpeg", "ffprobe"}), frozenset(), frozenset({"cpu"}), frozenset())


def seed(root: Path):
    connection = open_database(root)
    with connection:
        connection.execute("INSERT INTO bilibili_users VALUES (1,'creator',1,1)")
        connection.execute("INSERT INTO videos VALUES ('BVtest',1,1,'video',1,1,1)")
        connection.execute("INSERT INTO video_parts VALUES (1,'BVtest',0,42,'part',1000,'metadata_collected',1,1)")
        for job, kind, status in (("audio", "audio", "queued"), ("subtitle", "subtitle", "queued"),
                                  ("failed", "audio", "failed"), ("cancelled", "audio", "cancelled")):
            connection.execute("INSERT INTO workflow_jobs(job_id,kind,video_part_id,dedupe_key,payload_json,status,available_at,created_at,updated_at) "
                               "VALUES (?,?,1,?,'{\"video_part_id\":1}',?,1,1,1)", (job, kind, job, status))
    connection.close()


def test_plan_and_doctor_never_change_facts_or_create_target(tmp_path):
    root = tmp_path / "archive"
    seed(root)
    before = hashlib.sha256((root / "archive.db").read_bytes()).hexdigest()
    doctor = doctor_archive(root, probe=PROBE)
    assert doctor["data_complete"]
    assert doctor["job_states"] == {"ready_now": 1, "blocked_environment": 1, "manual_retry": 1, "cancelled_terminal": 1}
    assert "credential:BILI_SESSDATA" in next(job for job in doctor["pending_jobs"] if job["kind"] == "subtitle")["missing"]
    assert hashlib.sha256((root / "archive.db").read_bytes()).hexdigest() == before
    snapshot = tmp_path / "snapshot.zip"
    save_snapshot(root, snapshot)
    target = tmp_path / "new-parent" / "restored"
    plan = plan_restore(snapshot, target, probe=PROBE)
    assert plan["data_complete"] and plan["target_ready"]
    assert len(plan["snapshot_sha256"]) == len(plan["plan_id"]) == 64
    assert not target.parent.exists()
    assert inspect_snapshot(snapshot, probe=PROBE)["job_states"] == doctor["job_states"]


def test_restore_audit_preserves_attempt_count_and_cancelled_terminal(tmp_path):
    root = tmp_path / "archive"
    seed(root)
    connection = open_database(root)
    with connection:
        connection.execute("UPDATE workflow_jobs SET status='running',attempt_count=4,lease_owner='old-worker',lease_expires_at=1 WHERE job_id='audio'")
        connection.execute("INSERT INTO workflow_attempts VALUES ('attempt','audio','old-worker',1,NULL,'running',NULL,NULL)")
    connection.close()
    snapshot = tmp_path / "snapshot.zip"
    save_snapshot(root, snapshot)
    preview = inspect_snapshot(snapshot, probe=PROBE)
    assert {row["entity"] for row in preview["recovery_changes"]} == {"workflow_jobs", "workflow_attempts"}
    target = tmp_path / "target"
    report = tmp_path / "recovery.ndjson"
    restore_snapshot(snapshot, target, report_path=report)
    events = [json.loads(line) for line in report.read_text().splitlines()]
    assert events[-1]["type"] == "archive_installed"
    assert {row["identity"] for row in events if row["type"] == "recovery_change"} == {"audio", "attempt"}
    connection = open_database(target)
    assert tuple(connection.execute("SELECT status,attempt_count,lease_owner FROM workflow_jobs WHERE job_id='audio'").fetchone()) == ("queued", 4, None)
    assert connection.execute("SELECT status FROM workflow_jobs WHERE job_id='cancelled'").fetchone()[0] == "cancelled"
    connection.close()


def test_doctor_missing_artifact_is_data_failure_and_missing_root_is_not_created(tmp_path):
    root = tmp_path / "archive"
    seed(root)
    connection = open_database(root)
    with connection:
        connection.execute("INSERT INTO audio_objects VALUES (1,?,3,'m4a',1000,'audio/missing.m4a',1)", ("0" * 64,))
    connection.close()
    report = doctor_archive(root, probe=PROBE)
    assert not report["data_complete"]
    assert report["artifact_failures"] == [{"artifact": "audio/missing.m4a", "code": "missing_artifact"}]
    absent = tmp_path / "absent"
    with pytest.raises(FileNotFoundError):
        doctor_archive(absent, probe=PROBE)
    assert not absent.exists()


def test_plan_and_report_refuse_existing_and_linked_targets(tmp_path):
    root = tmp_path / "archive"
    seed(root)
    snapshot = tmp_path / "snapshot.zip"
    save_snapshot(root, snapshot)
    with pytest.raises(SnapshotError):
        plan_restore(snapshot, root, probe=PROBE)
    with pytest.raises(SnapshotError):
        restore_snapshot(snapshot, tmp_path / "target", report_path=snapshot)
    assert not (tmp_path / "target").exists()


def test_secret_environment_values_do_not_enter_reports(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    seed(root)
    monkeypatch.setenv("BILI_SESSDATA", "private-cookie-value")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-api-value")
    report = doctor_archive(root, probe=RuntimeProbe.local())
    encoded = json.dumps(report)
    assert "private-cookie-value" not in encoded and "private-api-value" not in encoded
    assert report["credentials_verified_online"] is False


def test_invalid_payload_is_never_reported_ready(tmp_path):
    root = tmp_path / "archive"
    seed(root)
    connection = open_database(root)
    with connection:
        connection.execute("UPDATE workflow_jobs SET payload_json='{}' WHERE job_id='audio'")
    connection.close()
    job = next(row for row in doctor_archive(root, probe=PROBE)["pending_jobs"] if row["job_id"] == "audio")
    assert job["derived_state"] == "blocked_invalid_payload"
    assert "payload:invalid" in job["missing"]


def test_post_install_audit_failure_reports_installed_fact(tmp_path, monkeypatch):
    from bili_asr.services import archive_snapshot
    root = tmp_path / "archive"
    seed(root)
    snapshot = tmp_path / "snapshot.zip"
    save_snapshot(root, snapshot)
    target = tmp_path / "restored"
    audit = tmp_path / "report.ndjson"
    original = archive_snapshot._sync_file
    calls = 0
    def fault(stream):
        nonlocal calls
        if str(stream.name) == str(audit):
            calls += 1
            if calls == 2:
                raise OSError("fault during final audit sync")
        return original(stream)
    monkeypatch.setattr(archive_snapshot, "_sync_file", fault)
    report = restore_snapshot(snapshot, target, report_path=audit)
    assert report["installed"] is True and report["recovery_report_written"] is False
    assert report["warnings"] == ["installed_recovery_report_failed"]
    assert (target / "archive.db").is_file()


def test_cli_inspect_plan_doctor_never_bootstrap_missing_archive(tmp_path, capsys):
    from bili_asr.cli import main
    root = tmp_path / "archive"
    seed(root)
    snapshot = tmp_path / "snapshot.zip"
    save_snapshot(root, snapshot)
    assert main(["snapshot", "inspect", "--file", str(snapshot)]) == 0
    assert json.loads(capsys.readouterr().out)["data_complete"] is True
    target = tmp_path / "new-parent" / "target"
    assert main(["snapshot", "plan", "--file", str(snapshot), "--archive-root", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["target_ready"] is True
    assert not target.parent.exists()
    missing = tmp_path / "missing"
    assert main(["snapshot", "doctor", "--archive-root", str(missing)]) == 1
    assert not missing.exists()


def test_doctor_rejects_corrupt_bundle_against_marker(tmp_path):
    from tests.test_archive_snapshot import _seed_publication
    root = tmp_path / "archive"
    seed(root)
    damaged = _seed_publication(root)
    damaged.write_bytes(b"tampered")
    result = doctor_archive(root, probe=PROBE)
    assert not result["data_complete"]
    assert any(failure["code"] == "bundle_integrity_failed" for failure in result["artifact_failures"])
