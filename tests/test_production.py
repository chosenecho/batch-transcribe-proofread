"""Ordinary production operations keep queue ownership and recovery semantics."""
import json
import hashlib

import pytest

from bili_asr.cli import main
from bili_asr.production_backup import (
    restore_production_backup,
    save_production_backup,
    verify_production_backup,
)
from bili_asr.production_profile import (
    default_profile,
    load_profile,
    resolved_profile,
    save_profile,
    update_profile,
)
from bili_asr.production_runtime import (
    resume_production,
    runtime_status,
    start_production,
    stop_production,
)
from bili_asr.storage import open_database
from bili_asr.storage.workflow_outputs import record_audio
from tests.test_workflow_control_plane import _seed_part


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path / "archive"
    connection = open_database(root)
    _seed_part(connection)
    media = root / "audio" / "fixture.wav"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"offline planning fixture; no model inference")
    with connection:
        connection.execute("BEGIN")
        record_audio(connection, part_id=1, sha256=hashlib.sha256(media.read_bytes()).hexdigest(),
            byte_size=media.stat().st_size, media_format="wav", duration_ms=1000,
            storage_key="audio/fixture.wav", target_id="local")
    connection.close()
    path = tmp_path / "production.json"
    profile = default_profile(archive_root="archive", external_controls=[])
    profile["workers"] = {"asr": 0, "cpu": 1, "editorial": 0}
    profile["runtime"].update(poll_interval=0.05, drain_timeout=1)
    save_profile(path, profile, create=True)
    return path, root


def test_configuration_edits_validate_as_a_batch_and_reject_credentials(deployment):
    path, _ = deployment
    original = path.read_bytes()
    with pytest.raises(ValueError, match="worker counts"):
        update_profile(path, ["workers.cpu=0"])
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match="credentials"):
        update_profile(path, ["editorial.api_key=secret"])
    assert path.read_bytes() == original
    changed = update_profile(path, ["workers.asr=2", "asr.model_name=./models/new-asr", "asr.model_revision=rev-2"])
    assert changed["workers"]["asr"] == 2
    assert resolved_profile(path)["asr"]["model_name"] == str((path.parent / "models/new-asr").resolve())


def test_model_switch_only_affects_new_plans(deployment, capsys):
    path, root = deployment
    command = ["production", "plan", "--config", str(path), "--part-id", "1"]
    assert main(command) == 0
    first = json.loads(capsys.readouterr().out)
    connection = open_database(root)
    frozen = [tuple(row) for row in connection.execute("SELECT job_id,payload_json FROM workflow_jobs ORDER BY job_id")]
    connection.close()
    update_profile(path, ["asr.model_name=./models/different", "asr.model_revision=rev-2"])
    assert main(command) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["profile_id"] != second["profile_id"]
    connection = open_database(root)
    try:
        for identity, payload in frozen:
            assert connection.execute("SELECT payload_json FROM workflow_jobs WHERE job_id=?", (identity,)).fetchone()[0] == payload
        assert not connection.execute("SELECT 1 FROM workflow_attempts").fetchone()
    finally:
        connection.close()


def test_full_backup_round_trip_is_offline_and_does_not_start_workers(deployment, tmp_path, capsys):
    path, root = deployment
    backup = tmp_path / "backup"
    receipt = save_production_backup(path, backup)
    assert verify_production_backup(backup)["manifest"]["bundle_id"] == receipt["bundle_id"]
    assert main(["production", "inspect", "--directory", str(backup), "--jobs"]) == 0
    assert json.loads(capsys.readouterr().out)["archive_state"]["data_complete"] is True
    target = tmp_path / "restored"
    plan = restore_production_backup(backup, target, dry_run=True)
    assert plan["dry_run"] and not target.exists()
    result = restore_production_backup(backup, target)
    assert result["installed"] and result["workers_started"] is False
    assert load_profile(target / "production.json")["archive_root"] == "archive"
    assert runtime_status(target / "production.json")["state"] == "stopped"
    assert (root / "archive.db").exists()
    with pytest.raises(ValueError, match="new destination"):
        restore_production_backup(backup, target)


def test_backup_refuses_to_silently_omit_unknown_external_controls(deployment, tmp_path):
    path, _ = deployment
    profile = load_profile(path)
    profile["external_controls"] = None
    save_profile(path, profile)
    with pytest.raises(ValueError, match="declare typed external_controls"):
        save_production_backup(path, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_real_idle_cpu_supervisor_starts_drains_and_requires_explicit_resume(deployment):
    path, _ = deployment
    try:
        started = start_production(path, startup_timeout=15)
        assert started["state"] == "running"
        status = runtime_status(path)
        assert status["state"] == "running" and status["supervisor_lock_held"]
        assert status["observed"]["run_id"] == started["run_id"]
        with pytest.raises(ValueError, match="already has a supervisor"):
            start_production(path)
    finally:
        stop_production(path, timeout=15)
    assert runtime_status(path)["state"] == "stopped"
    with pytest.raises(ValueError, match="resume"):
        start_production(path)
    resume_production(path)
    assert not runtime_status(path)["stop_requested"]


def test_backup_retains_declared_holds_and_spent_retry_budget(deployment, tmp_path):
    path, _ = deployment
    control = {"format": "retry-control/v1", "schema_version": 1,
               "retry_holds": {"job-one": {"reason": "preserve original timestamp evidence"}},
               "retry_ledger": {"job-one": {"retries": 4, "last_retry_at": 1791500000}}}
    content = json.dumps(control, ensure_ascii=False, indent=2).encode("utf-8")
    (path.parent / "control.json").write_bytes(content)
    profile = load_profile(path)
    profile["external_controls"] = [{"kind": "retry-control/v1", "path": "./control.json",
                                     "lock_path": "./control.lock"}]
    save_profile(path, profile)
    save_production_backup(path, tmp_path / "backup")
    verify_production_backup(tmp_path / "backup")
    restore_production_backup(tmp_path / "backup", tmp_path / "restored")
    assert (tmp_path / "restored/controls/component-0.json").read_bytes() == content
    restored = resolved_profile(tmp_path / "restored/production.json")
    assert restored["external_controls"][0]["path"] == str((tmp_path / "restored/controls/component-0.json").resolve())
    assert json.loads(content)["retry_ledger"]["job-one"]["retries"] == 4


def test_restore_preflight_reports_insufficient_disk_without_installing(deployment, tmp_path, monkeypatch, capsys):
    from bili_asr.services import archive_recovery
    path, _ = deployment
    save_production_backup(path, tmp_path / "backup")
    monkeypatch.setattr(archive_recovery, "plan_restore", lambda *args, **kwargs:
                        {"data_complete": True, "target_ready": False, "required_free_bytes": 100, "available_free_bytes": 1})
    assert main(["production", "restore", "--directory", str(tmp_path / "backup"),
                 "--target", str(tmp_path / "not-installed"), "--dry-run"]) == 1
    assert json.loads(capsys.readouterr().out)["target_ready"] is False
    assert not (tmp_path / "not-installed").exists()


@pytest.mark.parametrize(("keys", "replacement"), [
    (("format",), "unknown"),
    (("archive_root",), ""),
    (("artifact_root",), 5),
    (("asr",), []),
    (("asr", "not_a_setting"), True),
    (("editorial", "not_a_setting"), True),
    (("runtime",), {}),
    (("runtime", "poll_interval"), float("inf")),
    (("runtime", "cache_max_bytes"), -1),
    (("runtime", "gpu_session"), "legacy"),
    (("runtime", "cache_root"), ""),
    (("runtime", "storage_targets"), ["missing-equals"]),
    (("external_controls",), {}),
    (("external_controls",), [{"kind": "unknown", "path": "state", "lock_path": "lock"}]),
])
def test_invalid_profile_cannot_replace_the_last_valid_configuration(deployment, keys, replacement):
    path, _ = deployment
    before = path.read_bytes()
    profile = load_profile(path)
    target = profile
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = replacement
    with pytest.raises(ValueError):
        save_profile(path, profile)
    assert path.read_bytes() == before


@pytest.mark.parametrize("assignment", ["missing-equals", "format=changed", "unknown.child=1"])
def test_invalid_setting_syntax_leaves_the_profile_untouched(deployment, assignment):
    path, _ = deployment
    before = path.read_bytes()
    with pytest.raises(ValueError):
        update_profile(path, [assignment])
    assert path.read_bytes() == before


def test_cli_configuration_and_backup_restore_use_json_receipts(deployment, tmp_path, capsys):
    path, root = deployment
    declaration = tmp_path / "controls.json"
    declaration.write_text('{"components":[]}', encoding="utf-8")
    other = tmp_path / "other.json"
    assert main(["production", "init", "--config", str(other), "--archive-root", str(root),
                 "--controls", str(declaration)]) == 0
    assert json.loads(capsys.readouterr().out)["archive_created"] is False
    assert main(["production", "config", "--config", str(path), "--set", "workers.cpu=2"]) == 0
    assert json.loads(capsys.readouterr().out)["workers"]["cpu"] == 2
    assert main(["production", "config", "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["workers"]["cpu"] == 2
    assert main(["production", "status", "--config", str(path), "--jobs", "--failed"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "stopped"
    # Doctor may find an unprovisioned runtime; it still reports rather than starting work.
    code = main(["production", "doctor", "--config", str(path)])
    report = json.loads(capsys.readouterr().out)
    assert code == int(report.get("data_complete") is False or report.get("runtime_ready") is False)
    assert report["production"]["state"] == "stopped"
    backup, restored = tmp_path / "cli-backup", tmp_path / "cli-restored"
    assert main(["production", "backup", "--config", str(path), "--out", str(backup)]) == 0
    identity = json.loads(capsys.readouterr().out)["bundle_id"]
    assert main(["production", "inspect", "--directory", str(backup)]) == 0
    assert json.loads(capsys.readouterr().out)["manifest"]["bundle_id"] == identity
    assert main(["production", "restore", "--directory", str(backup), "--target", str(restored)]) == 0
    assert json.loads(capsys.readouterr().out)["workers_started"] is False
    connection = open_database(root)
    try:
        assert not connection.execute("SELECT 1 FROM workflow_attempts").fetchone()
    finally:
        connection.close()


@pytest.mark.parametrize("arguments", [
    ["start", "--startup-timeout", "0"],
    ["stop", "--timeout", "0"],
    ["config", "--set", "unknown=1"],
])
def test_operator_errors_return_nonzero_with_diagnostics(deployment, capsys, arguments):
    path, _ = deployment
    assert main(["production", *arguments, "--config", str(path)]) == 1
    captured = capsys.readouterr()
    assert not captured.out and "production " in captured.err


def test_cli_foreground_heartbeat_and_detached_restart_drain(deployment, capsys):
    import threading

    path, _ = deployment
    timer = threading.Timer(1.5, lambda: stop_production(path, wait=False))
    timer.start()
    try:
        assert main(["production", "run", "--config", str(path)]) == 0
    finally:
        stop_production(path, timeout=15)
        timer.join(timeout=3)
    assert json.loads(capsys.readouterr().out)["state"] == "stopped"
    assert runtime_status(path)["observed"]["state"] == "stopped"
    assert main(["production", "resume", "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["workers_started"] is False
    try:
        assert main(["production", "start", "--config", str(path)]) == 0
        assert json.loads(capsys.readouterr().out)["state"] == "running"
        assert main(["production", "restart", "--config", str(path)]) == 0
        assert json.loads(capsys.readouterr().out)["state"] == "running"
    finally:
        assert main(["production", "stop", "--config", str(path), "--timeout", "15"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "stopped"


def test_cli_proofread_freezes_an_existing_source_without_provider_calls(deployment, capsys):
    from tests.test_ai_editorial import insert_record, record

    path, root = deployment
    connection = open_database(root)
    try:
        insert_record(connection, record())
    finally:
        connection.close()
    assert main(["production", "proofread", "--config", str(path),
                 "--base-transcript-id", "1", "--no-reference"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["input_id"] and result["job_id"] and result["render_job_id"]
    connection = open_database(root)
    try:
        assert not connection.execute("SELECT 1 FROM editorial_model_calls").fetchone()
    finally:
        connection.close()
