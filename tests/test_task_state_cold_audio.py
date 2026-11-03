"""Legal audio offload remains compatible with explicit inactive state capture."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from bili_asr.artifact_inventory import stream_hash
from bili_asr.remote_storage import RemoteStorageError
from bili_asr.services import maintenance_backup as backup
from bili_asr.services import maintenance_media as media
from bili_asr.services import maintenance_task_state as state
from bili_asr.services.maintenance_state_reader import (
    StateQuery,
    read_task_records,
    read_task_state,
)
from bili_asr.services.remote_artifacts import push_remote_package
from bili_asr.storage.maintenance_selection import table_ledger
from tests.support.artifact_online import archive as online_archive
from tests.test_artifact_inventory_service import attempt, job
from tests.test_audio_retention import configure, run
from tests.test_task_state_backup import rewrite


@pytest.fixture
def cold(tmp_path):
    archive = online_archive.__wrapped__(tmp_path)
    configure(archive, allow_delete=True)
    report = run(archive)
    assert report["released_bytes"] == len(archive.data)
    assert not (archive.root / "audio/input.m4a").exists()
    return archive


def _save(cold, output, **kwargs):
    return state.save_task_state(cold.root, output, no_external_control_state=True,
                                 media_policy="verified-references", local_targets={"cold": cold.target}, **kwargs)


def _policy(cold, repository, **kwargs):
    return backup.policy(cold.root, repository, mode="full", profile="task-state",
                         no_external_control_state=True, media_policy="verified-references", **kwargs)


def test_real_offload_then_automatic_state_backup_is_stored_and_offline_readable(cold, tmp_path, monkeypatch):
    with closing(sqlite3.connect(cold.root / "archive.db")) as connection:
        job(connection, "failed-asr", status="failed")
        job(connection, "queued-asr", status="queued")
        attempt(connection, "failed-attempt", "failed-asr", key="audio/input.m4a", content=cold.data, outcome="failed")
    control_root = tmp_path / "controls"
    control_root.mkdir()
    controls = control_root / "retry.json"
    raw = (json.dumps({"format": "retry-control/v1", "schema_version": 1,
                      "retry_holds": {"failed-asr": {"reason": "keep failed audio investigation"}},
                      "retry_ledger": {"failed-asr": {"retries": 3, "max_retries": 3, "last_retry_at": 100,
                         "next_retry_at": 200, "last_error_code": "timeout", "dispatch_outcome": "reserved"}}},
                     ensure_ascii=False, indent=3) + "\n").encode()
    controls.write_bytes(raw)
    component = {"kind": "retry-control/v1", "path": str(controls), "lock_path": str(control_root / "retry.lock")}
    document = backup.policy(cold.root, tmp_path / "repository", mode="full", profile="task-state",
                             media_policy="verified-references", external_components=[component])
    before = table_ledger(cold.root / "archive.db")
    result = backup.run_backup(document, now=2_000_000_000, local_targets={"cold": cold.target})
    assert result["valid"], result
    assert result["state_evidence_complete"] and result["media_restore_required"]
    assert result["media_restored"] is False and result["external_media_verified_now"] is False
    snapshot = Path(document["repository"]) / result["snapshot"]
    verified = state.verify_task_state(snapshot)
    manifest = verified["manifest"]
    assert manifest["format"] == state.REFERENCE_FORMAT
    reference = manifest["media_dependencies"][0]
    assert (reference["sha256"], reference["size"], reference["available"]) == (cold.digest, len(cold.data), False)
    replica = reference["replicas"][0]
    assert replica["generation"] == 1 and replica["presence"] == "present"
    with zipfile.ZipFile(snapshot) as bundle:
        assert all(item.compress_type == zipfile.ZIP_STORED for item in bundle.infolist())
        assert not any(item.filename.startswith("audio/") for item in bundle.infolist())
        assert bundle.read("control-state/component-0.json") == raw
    # Neither the active source nor any external target is needed by the reader.
    shutil.rmtree(cold.root)
    shutil.rmtree(cold.target)
    def refuse_external(*args, **kwargs):
        raise AssertionError("offline state reads must not open an external package")
    monkeypatch.setattr(media, "open_directory_target", refuse_external)
    monkeypatch.setattr(media, "check_artifact_package", refuse_external)
    report = read_task_state(snapshot, query=StateQuery())
    assert {row["values"]["job_id"]: row["values"]["status"] for row in report["jobs"]} == {"failed-asr": "failed", "queued-asr": "queued"}
    assert report["external_audio_references"] == 1 and report["media_restored"] is False
    failed = read_task_records(snapshot, "workflow_attempts", query=StateQuery())
    assert failed["records"][0]["values"]["outcome"] == "failed"
    stage = tmp_path / "read-evidence"
    stage.mkdir()
    assert state.extract_task_state(snapshot, stage)["tables"] == before
    assert table_ledger(stage / "archive.db") == before
    assert not (stage / "audio").exists()


def test_v1_does_not_silently_acquire_cold_reference_semantics(cold, tmp_path):
    legacy = backup.policy(cold.root, tmp_path / "repository", mode="full", profile="task-state",
                           no_external_control_state=True)
    assert "media_policy" not in legacy
    failed = backup.run_backup(legacy, now=2_000_000_000)
    assert not failed["valid"] and "dependency is missing" in failed["error"]
    with pytest.raises(ValueError, match="explicit"):
        backup.run_backup(legacy, local_targets={"cold": cold.target})
    restored = cold.root / "audio/input.m4a"
    restored.write_bytes(cold.data)
    original = tmp_path / "legacy.zip"
    state.save_task_state(cold.root, original, no_external_control_state=True)
    assert state.verify_task_state(original)["manifest"]["format"] == state.FORMAT
    assert _save(cold, tmp_path / "v2-local.zip")["manifest"]["media_dependencies"][0]["available"] is True


@pytest.mark.parametrize("damage", ["missing", "corrupt", "unknown", "unverified", "unbound", "wrong-target"])
def test_capture_refuses_missing_corrupt_unverified_or_unbound_cold_audio(cold, tmp_path, damage):
    package_path = next(cold.target.glob("packages/*.zip"))
    options = {"local_targets": {"cold": cold.target}}
    if damage == "missing":
        package_path.unlink()
    elif damage == "corrupt":
        package_path.write_bytes(b"corrupt compressed package")
    elif damage in {"unknown", "unverified"}:
        with sqlite3.connect(cold.root / "archive.db") as connection:
            if damage == "unknown":
                connection.execute("UPDATE artifact_replicas SET presence='unknown' WHERE target_id='cold'")
            else:
                connection.execute("UPDATE artifact_replicas SET presence='released' WHERE target_id='cold'")
    elif damage == "unbound":
        options = {}
    else:
        (cold.target / ".bili-asr-storage-target.json").write_text('{"version":1,"target_id":"wrong"}')
    before = table_ledger(cold.root / "archive.db")
    with pytest.raises(ValueError, match="currently verified replica"):
        state.save_task_state(cold.root, tmp_path / "bad.zip", no_external_control_state=True,
                              media_policy="verified-references", **options)
    assert table_ledger(cold.root / "archive.db") == before
    assert not (tmp_path / "bad.zip").exists() and not (cold.root / "audio/input.m4a").exists()


@pytest.mark.parametrize("change", [
    lambda m: m["media_dependencies"][0].update(size=999),
    lambda m: m["media_dependencies"][0].update(sha256="0" * 64),
    lambda m: m["media_dependencies"][0]["replicas"][0].update(generation=2),
    lambda m: m["media_dependencies"][0]["replicas"][0].update(package_sha256="0" * 64),
    lambda m: m["media_dependencies"][0]["replicas"][0].update(member_key="objects/other"),
    lambda m: m["media_dependencies"][0]["replicas"][0].update(presence="unknown"),
    lambda m: m["media_dependencies"][0]["replicas"][0].update(instance_id="a" * 32),
    lambda m: m["media_dependencies"][0]["replicas"][0].update(relative_key="../outside.zip"),
    lambda m: m.update(format=state.FORMAT),
    lambda m: m.update(media_policy="future"),
])
def test_rehashed_cold_reference_cannot_change_original_or_replica_identity(cold, tmp_path, change):
    original = tmp_path / "state.zip"
    _save(cold, original)
    rewritten = rewrite(original, tmp_path / "changed.zip", change)
    with pytest.raises(ValueError):
        state.verify_task_state(rewritten)


def test_package_generation_change_during_capture_is_refused(cold, tmp_path, monkeypatch):
    original = state._copy_file
    def drift(*args, **kwargs):
        original(*args, **kwargs)
        package_path = next(cold.target.glob("packages/*.zip"))
        package_path.write_bytes(package_path.read_bytes() + b"changed")
    # Include nonmedia evidence so the mutation occurs after package readback.
    documents = cold.root / "documents"
    documents.mkdir(exist_ok=True)
    (documents / "note.txt").write_text("evidence")
    monkeypatch.setattr(state, "_copy_file", drift)
    with pytest.raises(ValueError, match="changed across capture"):
        _save(cold, tmp_path / "raced.zip")
    assert not (tmp_path / "raced.zip").exists()


def test_current_container_sha_is_required_even_when_original_members_still_validate(cold, tmp_path):
    package_path = next(cold.target.glob("packages/*.zip"))
    altered = bytearray(package_path.read_bytes())
    assert altered[:4] == b"PK\x03\x04"
    # An advisory local ZIP timestamp changes container bytes without changing
    # length, manifest, compression or the original audio member's digest.
    altered[10] ^= 1
    package_path.write_bytes(altered)
    assert media.check_artifact_package(package_path)["valid"]
    with pytest.raises(ValueError, match="currently verified replica"):
        _save(cold, tmp_path / "changed-container.zip")
    assert not (tmp_path / "changed-container.zip").exists()


class RemoteReadback:
    """A real compressed byte transport with explicit remote instance identity."""
    def __init__(self, root):
        self.root = root
        self.binding = SimpleNamespace(target_id="remote-cold", instance_id="a" * 32)
        self.downloads = 0

    def upload(self, path, *, key, sha256, size):
        destination = self.root / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        assert (destination.stat().st_size, hashlib.sha256(destination.read_bytes()).hexdigest()) == (size, sha256)
        return {"verified": True}

    def download(self, *, key, sha256, size, destination):
        self.downloads += 1
        with (self.root / key).open("rb") as reader:
            observed = stream_hash(reader, destination)
        if observed != (size, sha256):
            raise RemoteStorageError("package_hash_mismatch")

    def probe(self):
        return {"verified": True, "instance_id": self.binding.instance_id}


@pytest.mark.parametrize("damage", [None, "corrupt", "unbound-instance"])
def test_remote_compressed_copy_is_explicitly_verified_without_restoring_audio(cold, tmp_path, damage):
    backend = RemoteReadback(tmp_path / "remote")
    package_path = next(cold.target.glob("packages/*.zip"))
    push_remote_package(cold.roots, package_path, backend)
    # A broken local alternative must not shadow the retained remote copy.
    package_path.write_bytes(b"unavailable local alternative")
    if damage == "corrupt":
        next(backend.root.glob("packages/*.zip")).write_bytes(b"corrupt remote")
    elif damage == "unbound-instance":
        backend.binding.instance_id = None
    options = {"media_policy": "verified-references", "local_targets": {"cold": cold.target},
               "remote_backends": {"remote-cold": backend}, "no_external_control_state": True}
    before = table_ledger(cold.root / "archive.db")
    snapshot = tmp_path / "state.zip"
    if damage:
        with pytest.raises(ValueError, match="currently verified replica"):
            state.save_task_state(cold.root, snapshot, **options)
        assert not snapshot.exists()
    else:
        manifest = state.save_task_state(cold.root, snapshot, **options)["manifest"]
        replica = manifest["media_dependencies"][0]["replicas"][0]
        assert (replica["backend"], replica["instance_id"]) == ("ssh-directory/v1", "a" * 32)
        assert backend.downloads == 1
        shutil.rmtree(backend.root)
        assert state.verify_task_state(snapshot)["valid"]
    assert table_ledger(cold.root / "archive.db") == before
    assert not (cold.root / "audio/input.m4a").exists()


def test_automatic_retention_and_repeated_runs_read_cold_state_without_live_bindings(cold, tmp_path):
    document = _policy(cold, tmp_path / "repository", interval_seconds=1, keep_count=1, keep_days=0)
    first = backup.run_backup(document, now=100, local_targets={"cold": cold.target})
    assert first["valid"], first
    second = backup.run_backup(document, now=101, local_targets={"cold": cold.target})
    assert second["valid"], second
    repository = Path(document["repository"])
    assert not (repository / first["snapshot"]).exists()
    shutil.rmtree(cold.target)
    reused = backup.run_backup(document, now=101)
    assert reused["valid"] and reused["reused"]
    assert reused["external_media_verified_now"] is False and reused["media_restored"] is False


@pytest.mark.parametrize("drift", ["binding", "probe"])
def test_remote_instance_change_across_capture_refuses_publication(cold, tmp_path, monkeypatch, drift):
    backend = RemoteReadback(tmp_path / "remote")
    push_remote_package(cold.roots, next(cold.target.glob("packages/*.zip")), backend)
    documents = cold.root / "documents"
    documents.mkdir(exist_ok=True)
    (documents / "note.txt").write_text("evidence")
    original = state._copy_file
    def change(*args, **kwargs):
        original(*args, **kwargs)
        if drift == "binding":
            backend.binding.instance_id = "b" * 32
        else:
            backend.probe = lambda: {"instance_id": "b" * 32}
    monkeypatch.setattr(state, "_copy_file", change)
    with pytest.raises(ValueError, match="changed across capture"):
        state.save_task_state(cold.root, tmp_path / "changed.zip", no_external_control_state=True,
                             media_policy="verified-references", remote_backends={"remote-cold": backend})
    assert not (tmp_path / "changed.zip").exists()


def test_scheduler_cli_captures_offloaded_audio_and_legacy_policy_rejects_bindings(cold, tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("cold_state_scheduler", Path(__file__).resolve().parents[1]
                                                 / "scripts/deployment/run_archive_backup.py")
    scheduler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scheduler)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(_policy(cold, tmp_path / "repository")))
    class OnePoll:
        stopped = False
        def is_set(self):
            return self.stopped
        def set(self):
            self.stopped = True
        def wait(self, seconds):
            self.set()
    monkeypatch.setattr(scheduler.threading, "Event", OnePoll)
    monkeypatch.setattr(scheduler.signal, "signal", lambda *_: None)
    monkeypatch.setattr(sys, "argv", ["scheduler", "--policy", str(policy_path), "--storage-target", "cold=" + str(cold.target)])
    assert scheduler.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] and result["media_restored"] is False and result["external_audio_references"] == 1
    legacy = backup.policy(cold.root, tmp_path / "legacy", mode="full", profile="task-state", no_external_control_state=True)
    policy_path.write_text(json.dumps(legacy))
    assert scheduler.main() == 0
    failure = json.loads(capsys.readouterr().out)
    assert failure["valid"] is False and "explicit" in failure["error"]


def test_standalone_cli_uses_explicit_cold_binding_and_reads_without_source_or_target(cold, tmp_path):
    policy_path = tmp_path / "policy.json"
    def invoke(*args):
        result = subprocess.run([sys.executable, "-m", "bili_asr", *map(str, args)], cwd=tmp_path,
                                capture_output=True, text=True, encoding="utf-8", timeout=60, check=False)
        assert result.returncode == 0, result.stderr + result.stdout
        return json.loads(result.stdout)
    invoke("maintenance", "backup-policy", "--source-root", cold.root, "--repository", tmp_path / "repository",
           "--profile", "task-state", "--mode", "full", "--media-policy", "verified-references",
           "--no-external-control-state", "--output", policy_path)
    result = invoke("maintenance", "backup-run", "--policy", policy_path, "--storage-target", "cold=" + str(cold.target))
    assert result["state_evidence_complete"] and result["external_audio_references"] == 1
    snapshot = tmp_path / "repository" / result["snapshot"]
    shutil.rmtree(cold.root)
    shutil.rmtree(cold.target)
    verified = invoke("task-state", "verify", "--snapshot", snapshot)
    assert verified["media_restored"] is False and verified["external_media_verified_now"] is False
    report = invoke("task-state", "read", "--snapshot", snapshot)
    assert report["external_audio_references"] == 1 and report["media_restore_required"] is True
