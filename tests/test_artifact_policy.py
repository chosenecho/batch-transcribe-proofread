"""Explicit bounded policies, crash resumption and the public CLI."""
from __future__ import annotations

import json
import multiprocessing
import sqlite3
from copy import deepcopy
from types import SimpleNamespace

import pytest

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.services.artifact_policy import (
    DEFAULT_POLICY,
    configure_policy,
    run_policy_once,
)
from tests.support.artifact_online import archive as online_archive
from tests.support.artifact_online import configure
from tests.test_artifact_transfer import archive as audio_archive


@pytest.fixture
def archive(tmp_path):
    return online_archive.__wrapped__(tmp_path)


def test_default_off_and_copy_never_release_then_offload_uses_same_executor(archive):
    assert run_policy_once(archive.roots)["state"] == "off"
    configure(archive, mode="copy")
    copied = run_policy_once(archive.roots, target_root=archive.target, external_holds={})
    assert copied["state"] == "complete"
    assert copied["released_bytes"] == 0
    assert copied["io"]["bytes_read"] > len(archive.data)
    assert (archive.root / "audio/input.m4a").exists()
    configure(archive)
    with sqlite3.connect(archive.root / "archive.db") as connection:
        connection.execute("UPDATE artifact_policy_runs SET retry_after=0")
    released = run_policy_once(archive.roots, target_root=archive.target, external_holds={})
    assert released["state"] == "complete"
    assert released["released_bytes"] == len(archive.data)
    assert not (archive.root / "audio/input.m4a").exists()



def test_hold_and_unavailable_target_back_off_without_changing_jobs(archive):
    configure(archive)
    held = run_policy_once(archive.roots, target_root=archive.target, external_holds={f"sha256:{archive.digest}": ["manual"]})
    assert held["state"] == "blocked"
    assert run_policy_once(archive.roots, target_root=archive.target, external_holds={})["state"] == "backoff"
    with sqlite3.connect(archive.root / "archive.db") as connection:
        connection.execute("UPDATE artifact_policy_runs SET retry_after=0")
    archive.target.rename(archive.target.with_name("offline"))
    failed = run_policy_once(archive.roots, target_root=archive.target, external_holds={})
    assert failed["state"] == "failed"
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data



def test_disabling_policy_during_copy_prevents_release(archive, monkeypatch):
    from bili_asr.services import artifact_transfer
    configure(archive)
    original = artifact_transfer.create_artifact_package
    def disable(*args, **kwargs):
        result = original(*args, **kwargs)
        with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
            configure_policy(session.connection, "default", deepcopy(DEFAULT_POLICY))
        return result
    monkeypatch.setattr(artifact_transfer, "create_artifact_package", disable)
    report = run_policy_once(archive.roots, target_root=archive.target, external_holds={})
    assert report["state"] == "failed"
    assert "artifact_policy_changed" in report["reason"]
    retained = [path.read_bytes() for path in (archive.root / "audio").iterdir() if path.is_file()]
    assert archive.data in retained



def test_hysteresis_continues_between_watermarks_and_stops_at_low_pressure(archive, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=150))
    configure(archive, trigger_free_bytes=100, stop_free_bytes=200, minimum_free_bytes=0)
    assert run_policy_once(archive.roots)["state"] == "watermark_satisfied"
    with sqlite3.connect(archive.root / "archive.db") as connection:
        configuration = connection.execute("SELECT config_json FROM artifact_policies").fetchone()[0]
        from bili_asr.canonical_json import digest
        connection.execute("INSERT INTO artifact_policy_runs VALUES ('previous','default',?,?,NULL,?,'blocked',0,1,1)",
            (digest(json.loads(configuration)), configuration, '{"pressure_active":true}'))
    assert run_policy_once(archive.roots, external_holds=None)["state"] == "blocked"
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=200))
    with sqlite3.connect(archive.root / "archive.db") as connection:
        connection.execute("UPDATE artifact_policy_runs SET retry_after=0")
    assert run_policy_once(archive.roots)["state"] == "watermark_satisfied"



def _interrupted_policy(root, target, ready):
    from bili_asr.services import artifact_transfer as service
    original = service.release_copy
    def interrupt(*args, **kwargs):
        callback = kwargs["isolated"]
        def isolated():
            callback()
            ready.set()
            multiprocessing.Event().wait(60)
        kwargs["isolated"] = isolated
        return original(*args, **kwargs)
    service.release_copy = interrupt
    run_policy_once(ArtifactRoots.of(root), target_root=target, external_holds={})



def test_killed_policy_restarts_exact_journal_without_mutating_business(archive):
    from tests.test_artifact_transfer import domain_facts
    before = domain_facts(archive)
    configure(archive)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(target=_interrupted_policy, args=(archive.root, archive.target, ready))
    process.start()
    try:
        assert ready.wait(20)
    finally:
        process.kill()
        process.join(10)
    assert archive.data in [path.read_bytes() for path in (archive.root / "audio").iterdir() if path.is_file()]
    with sqlite3.connect(archive.root / "archive.db") as connection:
        frozen = connection.execute("SELECT plan_json FROM artifact_policy_runs").fetchone()[0]
        assert connection.execute("SELECT state FROM artifact_release_intents").fetchone()[0] == "isolated"
        connection.execute("UPDATE artifact_policy_runs SET retry_after=0")
    resumed = run_policy_once(archive.roots, target_root=archive.target, external_holds={})
    assert resumed["state"] == "complete"
    assert resumed["released_bytes"] == len(archive.data)
    with sqlite3.connect(archive.root / "archive.db") as connection:
        assert {row[0] for row in connection.execute("SELECT plan_json FROM artifact_policy_runs")} == {frozen}
    assert domain_facts(archive) == before



def test_policy_public_cli_requires_explicit_extension_and_supports_default_off(archive, capsys, tmp_path):
    from tests.test_artifacts_cli import call
    assert call(archive.root, "policy-show") == 0
    defaults = json.loads(capsys.readouterr().out)
    assert defaults["mode"] == "off"
    configuration = tmp_path / "policy.json"
    configuration.write_text(json.dumps(defaults))
    assert call(archive.root, "policy-set", "--config", configuration) == 0
    configured = json.loads(capsys.readouterr().out)
    assert configured["configuration"] == defaults
    assert call(archive.root, "policy-run", "--watch") == 0
    assert json.loads(capsys.readouterr().out)["state"] == "off"
    with sqlite3.connect(archive.root / "archive.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_policy_runs").fetchone()[0] == 0



@pytest.mark.parametrize("changed", [{"mode": []}, {"batch_objects": True}, {"backoff_seconds": 1}, {"unknown": 1}])
def test_invalid_policy_is_rejected_before_persistence(archive, changed):
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        with pytest.raises(ValueError):
            configure_policy(session.connection, "default", {**DEFAULT_POLICY, **changed})
        assert session.connection.execute("SELECT COUNT(*) FROM artifact_policies").fetchone()[0] == 0



def test_policy_commands_never_install_online_extension_implicitly(tmp_path, capsys):
    from tests.test_artifacts_cli import call
    fixture = audio_archive.__wrapped__(tmp_path)
    before = (fixture.root / "archive.db").read_bytes()
    assert call(fixture.root, "policy-show") == 1
    assert "artifact-online-v1" in capsys.readouterr().err
    assert (fixture.root / "archive.db").read_bytes() == before



def test_policy_never_splits_an_oversized_complete_text_group(archive):
    from bili_asr.services.artifact_groups import capture_artifact_groups
    from tests.test_artifact_groups import publish
    paths = publish(archive)
    capture_artifact_groups(archive.roots)
    selection = deepcopy(DEFAULT_POLICY["selection"])
    selection["kinds"] = ["bundle"]
    configure(archive, selection=selection, batch_objects=1)
    assert run_policy_once(archive.roots, target_root=archive.target, external_holds={})["state"] == "blocked"
    assert b"first" in (archive.root / paths["raw_path"]).read_bytes()



def test_io_meter_limits_aggregate_repeated_read_bytes(monkeypatch):
    from bili_asr.services import artifact_io
    clock = [0.0]
    monkeypatch.setattr(artifact_io.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(artifact_io.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    meter = artifact_io.ArtifactIOMeter(100, started=0)
    meter.observe(100)
    meter.observe(150)
    assert meter.report() == {"bytes_read": 250, "elapsed_seconds": 2.5, "bytes_per_second_limit": 100}
