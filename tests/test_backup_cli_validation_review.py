"""CLI failures preserve original archives, redact bindings and obey drain."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from bili_asr.remote_storage import RemoteStorageError, SSHDirectoryBackend
from bili_asr.services import artifact_policy, artifact_transfer
from bili_asr.services import maintenance_backup as backup
from bili_asr.storage.maintenance_selection import table_ledger
from bili_asr.storage.source_observations import require_observations
from tests.support.artifact_online import archive as online_archive
from tests.test_audio_retention import configure as configure_audio
from tests.test_maintenance_cli_review import invoke


def binding_file(tmp_path, *, instance="a" * 32):
    path = tmp_path / "private-binding.json"
    path.write_text(json.dumps({"backend": "ssh-directory/v1", "target_id": "remote-cold",
        "instance_id": instance, "host": "private-binding-host.invalid", "root": "/private-binding/root",
        "key_filename": "/private-binding/key", "known_hosts": "/private-binding/known_hosts"}), encoding="utf-8")
    return path


def test_archive_init_cli_explicitly_installs_raw_observation_extension(tmp_path, capsys):
    root = tmp_path / "initialized"
    code, report, errors = invoke(capsys, "archive", "init", "--target-root", root, "--with-source-observations")
    assert code == 0 and not errors and report["target_contract"] == "universal-v2"
    with closing(sqlite3.connect(root / "archive.db")) as connection:
        assert require_observations(connection)
        assert connection.execute("SELECT count(*) FROM source_metadata_observations").fetchone()[0] == 0
    before = table_ledger(root / "archive.db")
    code, _report, errors = invoke(capsys, "archive", "init", "--target-root", root, "--with-source-observations")
    assert code == 1 and "nonexistent or empty" in errors
    assert table_ledger(root / "archive.db") == before


def test_drain_created_during_a_real_policy_run_stops_watch_after_that_result(tmp_path, capsys, monkeypatch):
    archive = online_archive.__wrapped__(tmp_path)
    drain = tmp_path / "drain"
    before = table_ledger(archive.root / "archive.db")
    original = artifact_policy.run_policy_once
    calls = []

    def create_drain_after_run(*arguments, **options):
        result = original(*arguments, **options)
        calls.append(result)
        drain.write_text("stop after current operation", encoding="utf-8")
        return result

    monkeypatch.setattr(artifact_policy, "run_policy_once", create_drain_after_run)
    code, report, _errors = invoke(capsys, "artifacts", "policy-run", "--archive-root", archive.root,
                                  "--watch", "--drain-file", drain, "--no-external-holds")
    assert code == 0 and report["state"] == "drained" and report["last_run"] == calls[0]
    assert len(calls) == 1 and calls[0]["state"] == "off"
    assert table_ledger(archive.root / "archive.db") == before
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data


def test_interrupted_real_audio_copy_is_journaled_and_cli_drains_without_another_cycle(tmp_path, capsys, monkeypatch):
    archive = online_archive.__wrapped__(tmp_path)
    configure_audio(archive, allow_delete=True)
    calls = []

    def interrupt_before_copy(*arguments, **options):
        calls.append(options)
        raise KeyboardInterrupt

    monkeypatch.setattr(artifact_transfer, "create_artifact_package", interrupt_before_copy)
    code, report, _errors = invoke(capsys, "artifacts", "policy-run", "--archive-root", archive.root,
                                  "--policy-id", "audio", "--target-root", archive.target,
                                  "--watch", "--no-external-holds")
    assert code == 0 and report["state"] == "drained" and len(calls) == 1
    assert report["last_run"]["reason"] == "audio_retention_interrupted"
    assert report["last_run"]["released_bytes"] == 0
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data
    with closing(sqlite3.connect(archive.root / "archive.db")) as connection:
        state, result = connection.execute("SELECT state,result_json FROM artifact_policy_runs").fetchone()
        assert state == "failed" and json.loads(result)["reason"] == "audio_retention_interrupted"
        assert connection.execute("SELECT count(*) FROM workflow_attempts").fetchone()[0] == 0


@pytest.mark.parametrize("case,reason", [("legacy", "explicit verified-references"),
                                       ("unprepared", "exact remote instance"),
                                       ("connection", "connection_or_host_identity")])
def test_backup_cli_rejects_implicit_unprepared_or_unreachable_bindings_before_publication(tmp_path, capsys, monkeypatch, case, reason):
    archive = online_archive.__wrapped__(tmp_path)
    document = backup.policy(archive.root, tmp_path / "repository", profile="task-state", mode="full",
        no_external_control_state=True, media_policy="local-required" if case == "legacy" else "verified-references")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(document), encoding="utf-8")
    before = table_ledger(archive.root / "archive.db")
    attempts = []

    def unavailable(backend):
        attempts.append(backend.binding.target_id)
        raise RemoteStorageError("connection_or_host_identity")

    monkeypatch.setattr(SSHDirectoryBackend, "__enter__", unavailable)
    options = ("--storage-target", "cold=" + str(archive.target)) if case == "legacy" else (
        "--remote-binding", binding_file(tmp_path, instance=None if case == "unprepared" else "a" * 32))
    code, result, _errors = invoke(capsys, "maintenance", "backup-run", "--policy", policy_path, *options)
    assert code == 1 and result["valid"] is False and reason in result["error"]["message"]
    assert attempts == (["remote-cold"] if case == "connection" else [])
    assert table_ledger(archive.root / "archive.db") == before
    assert not (tmp_path / "repository").exists()


def test_remote_push_cli_reports_a_missing_package_without_echoing_private_binding(tmp_path, capsys, monkeypatch):
    archive = online_archive.__wrapped__(tmp_path)
    before = table_ledger(archive.root / "archive.db")
    # Authenticate boundary only: package validation and command failure are
    # real, and this test claims no successful remote transfer.
    monkeypatch.setattr(SSHDirectoryBackend, "__enter__", lambda backend: backend)
    code, report, errors = invoke(capsys, "remote", "push", "--archive-root", archive.root,
                                 "--binding", binding_file(tmp_path), "--package", tmp_path / "missing-package.zip")
    assert code == 1 and report is None and "remote: operation_failed" in errors
    assert "private-binding" not in errors and "missing-package.zip" not in errors
    assert table_ledger(archive.root / "archive.db") == before
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data
