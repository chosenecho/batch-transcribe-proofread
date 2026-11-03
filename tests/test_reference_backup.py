"""Reference backups preserve state while reporting unavailable execution inputs."""
from __future__ import annotations

import json
import socket
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from bili_asr import cli
from bili_asr.services import reference_backup as service
from bili_asr.services.archive_snapshot import SnapshotError, check_snapshot
from bili_asr.services.artifact_catalog_upgrade import authority_fingerprints
from bili_asr.storage.database import connect_database
from tests.test_artifact_restore import _authority, _setup


def _rewrite(source, destination, mutate):
    with zipfile.ZipFile(source) as bundle:
        entries = {info.filename: bundle.read(info) for info in bundle.infolist()}
    manifest = json.loads(entries[service.MANIFEST])
    mutate(manifest)
    entries[service.MANIFEST] = json.dumps(manifest).encode()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as bundle:
        for key, data in entries.items():
            bundle.writestr(key, data)


def test_reference_offline_roundtrip_preserves_every_typed_row_and_hold(tmp_path, monkeypatch):
    roots, target, audio, *_ = _setup(tmp_path)
    with connect_database(roots.archive_root / "archive.db", readonly=True) as connection:
        before = authority_fingerprints(connection)
    original = (roots.archive_root / "archive.db").read_bytes()
    output = tmp_path / "reference.zip"
    holds = {"version": 1, "holds": {"part:1": ["publication_contract_hold"], "job-retained": ["operator_investigation"]}}
    def no_network(*args, **kwargs):
        raise AssertionError("offline backup must not open a socket")
    monkeypatch.setattr(socket, "socket", no_network)
    saved = service.save_reference_backup(roots, output, holds=holds)
    assert saved["self_contained"] is False and saved["dependency_count"] == 1
    assert saved["external_state"] == "unverified" and saved["external_verified"] is False
    target.rename(tmp_path / "offline-target")
    checked = service.check_reference_backup(output)
    assert checked["blocking_objects"][0]["object_id"] == audio["sha256"]
    destination = tmp_path / "restored"
    report = service.restore_reference_backup(output, destination, source_workers_stopped=True)
    assert report["installed"] and not report["ready_to_run"]
    assert report["workflow_recovery_changes"] == [] and not report["worker_started"] and not report["retry_started"]
    assert not (destination / audio["storage_key"]).exists()
    assert not report["doctor"]["data_complete"]
    with connect_database(destination / "archive.db", readonly=True) as connection:
        assert authority_fingerprints(connection) == before
    controls = json.loads(next((destination / "documents/reference-recovery").rglob("holds.json")).read_bytes())
    assert controls == holds
    assert (roots.archive_root / "archive.db").read_bytes() == original
    with pytest.raises(SnapshotError):
        check_snapshot(output)


@pytest.mark.parametrize("failure", ["unbound", "offline", "identity", "corrupt"])
def test_explicit_online_check_names_blocked_objects_and_jobs(tmp_path, failure):
    roots, target, audio, _source, package, _replica = _setup(tmp_path)
    output = tmp_path / "reference.zip"
    service.save_reference_backup(roots, output)
    bindings = {"cold": target}
    if failure == "unbound":
        bindings = {}
    elif failure == "offline":
        target.rename(tmp_path / "offline")
    elif failure == "identity":
        (target / ".bili-asr-storage-target.json").write_text('{"version":1,"target_id":"other"}')
    else:
        path = Path(package["package_path"])
        data = path.read_bytes()
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    report = service.check_reference_backup(output, online=True, local_targets=bindings)
    assert report["valid"] and report["external_state"] == "unverifiable"
    assert not report["external_verified"]
    blocked = report["blocking_objects"][0]
    assert blocked["object_id"] == audio["sha256"] and blocked["path"] == audio["storage_key"]
    assert blocked["jobs"] and all("job_id" in job for job in blocked["jobs"])
    assert report["checked_at"] > 0


def test_online_verifies_current_bytes_but_does_not_grant_worker_execution(tmp_path):
    roots, target, _audio, *_ = _setup(tmp_path)
    output = tmp_path / "reference.zip"
    original = _authority(roots)
    service.save_reference_backup(roots, output)
    report = service.check_reference_backup(output, online=True, local_targets={"cold": target})
    assert report["external_state"] == "complete" and report["external_verified"]
    assert not report["blocking_objects"] and report["execution_handoff_required"]
    assert _authority(roots) == original


@pytest.mark.parametrize("mutation", [
    lambda m: m.update(self_contained=True),
    lambda m: m.update(format_version=True),
    lambda m: m["dependencies"][0].update(object_id="f" * 64),
    lambda m: m["dependencies"][0]["replicas"][0].update(package_sha256="f" * 64),
    lambda m: m["dependencies"][0]["replicas"][0].update(relative_key="../escape"),
    lambda m: m.update(dependencies=[]),
    lambda m: m["dependencies"].append(m["dependencies"][0]),
    lambda m: m.update(affected_jobs={}),
    lambda m: m.update(operational_state={"verified": True, "holds": {}, "password": "secret"}),
])
def test_forged_references_never_install_or_get_blessed_offline(tmp_path, mutation):
    roots, _target, *_ = _setup(tmp_path)
    output, bad = tmp_path / "reference.zip", tmp_path / "bad.zip"
    service.save_reference_backup(roots, output)
    _rewrite(output, bad, mutation)
    target = tmp_path / "restored"
    with pytest.raises(ValueError):
        service.restore_reference_backup(bad, target)
    assert not target.exists()


@pytest.mark.parametrize("extra", [{"env": {"SECRET": "sentinel"}}, {"credentials": "sentinel"}, {"retry": []}])
def test_operational_state_is_an_exact_whitelist(tmp_path, extra):
    with pytest.raises(ValueError):
        service.operational_holds({"version": 1, "holds": {}, **extra})
    with pytest.raises(ValueError):
        service.operational_holds({"version": 1, "holds": {"job": ["https://private/?token=secret"]}})


def test_reference_cli_offline_ignores_remote_binding_and_preserves_local_source(tmp_path, capsys):
    roots, _target, *_ = _setup(tmp_path)
    output = tmp_path / "reference.zip"
    assert cli.main(["reference", "save", "--archive-root", str(roots.archive_root), "--out", str(output)]) == 0
    capsys.readouterr()
    assert cli.main(["reference", "check", "--file", str(output), "--remote-binding", str(tmp_path / "must-not-read.json")]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["external_verified"] is False
    assert cli.main(["reference", "plan", "--file", str(output), "--archive-root", str(tmp_path / "new")]) == 0
    assert not (tmp_path / "new").exists()


def test_partial_remote_failure_does_not_shadow_another_valid_replica(tmp_path):
    from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
    from bili_asr.remote_storage import RemoteStorageError
    from bili_asr.storage.artifact_catalog import ArtifactCatalog
    roots, _target, audio, _source, package, _replica = _setup(tmp_path)
    with ArchiveSession(roots.archive_root, mode=ArchiveAccessMode.WRITE) as session, session.connection:
        catalog = ArtifactCatalog(session.connection)
        catalog.register_target("second")
        catalog.record_package(package["package_id"], "second", package["package_key"],
                               sha256=package["package_sha256"], byte_size=package["package_size_bytes"],
                               manifest_sha256=package["manifest_sha256"], verified_at=1)
        catalog.record_verified_replica(audio["sha256"], "second", package["package_key"], sha256=audio["sha256"],
                                        byte_size=audio["byte_size"], package_id=package["package_id"],
                                        member_key=package["objects"][0]["member"], verified_at=1)
    class Broken:
        binding = SimpleNamespace(instance_id="a" * 32)
        def download(self, *, destination, **_kwargs):
            destination.write(b"unfinished")
            raise RemoteStorageError("download_interrupted")
    class Good:
        binding = SimpleNamespace(instance_id="b" * 32)
        def download(self, *, destination, **_kwargs):
            destination.write(Path(package["package_path"]).read_bytes())
    output = tmp_path / "reference.zip"
    service.save_reference_backup(roots, output, target_instances={"cold": "a" * 32, "second": "b" * 32})
    report = service.check_reference_backup(output, online=True, remote_backends={"cold": Broken(), "second": Good()})
    assert report["external_state"] == "complete" and report["external_verified"]
    assert report["external_checks"][0]["target_id"] == "second"


def test_recorded_running_attempts_remain_exact_without_automatic_recovery(tmp_path):
    roots, _target, *_ = _setup(tmp_path)
    with connect_database(roots.archive_root / "archive.db") as connection:
        connection.execute("UPDATE workflow_jobs SET status='running',lease_owner='old-worker',lease_expires_at=1 WHERE kind='audio'")
        connection.execute("UPDATE workflow_attempts SET outcome='running',finished_at=NULL WHERE job_id IN (SELECT job_id FROM workflow_jobs WHERE kind='audio')")
        connection.commit()
        original = authority_fingerprints(connection)
    output = tmp_path / "running-records.zip"
    service.save_reference_backup(roots, output)
    destination = tmp_path / "restored"
    service.restore_reference_backup(output, destination)
    with connect_database(destination / "archive.db", readonly=True) as connection:
        assert authority_fingerprints(connection) == original


def test_restore_refuses_low_disk_before_creating_target_and_preserves_source(tmp_path, monkeypatch):
    roots, _target, *_ = _setup(tmp_path)
    backup = tmp_path / "reference.zip"
    service.save_reference_backup(roots, backup)
    original = backup.read_bytes()
    destination = tmp_path / "not-created" / "restored"
    monkeypatch.setattr(service.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    with pytest.raises(service.ReferenceBackupError, match="insufficient restore space"):
        service.restore_reference_backup(backup, destination)
    assert not destination.parent.exists()
    assert backup.read_bytes() == original


def test_restore_never_overwrites_nonempty_destination(tmp_path):
    roots, _target, *_ = _setup(tmp_path)
    backup = tmp_path / "reference.zip"
    service.save_reference_backup(roots, backup)
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "important.txt"
    sentinel.write_bytes(b"original archive must survive")
    with pytest.raises((ValueError, OSError)):
        service.restore_reference_backup(backup, destination)
    assert sentinel.read_bytes() == b"original archive must survive"
    assert list(destination.iterdir()) == [sentinel]
