"""Manual recovery retains byte identity, consumer paths and production history."""
from __future__ import annotations

import hashlib
import shutil
import struct
import zipfile
from pathlib import Path

import pytest

from bili_asr.archive_maintenance import ArchiveBusyError, archive_access
from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_packages import (
    PackageSource,
    capture_source_generation,
    create_artifact_package,
)
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.services import artifact_restore as restore_service
from bili_asr.services.artifact_catalog_upgrade import (
    authority_fingerprints,
    upgrade_artifact_catalog,
)
from bili_asr.storage.artifact_catalog import ArtifactCatalog
from bili_asr.storage.database import SchemaContractError
from bili_asr.storage_targets import MARKER, bind_directory_target
from tests.fixtures.frozen_migration_archive import frozen_archive


def _setup(tmp_path: Path, *, configured=False):
    source, archive, target = tmp_path / "legacy", tmp_path / "archive", tmp_path / "cold"
    frozen_archive(source)
    upgrade_artifact_catalog(source, archive)
    product_root = tmp_path / "products"
    if configured:
        product_root.mkdir()
    roots = ArtifactRoots.of(archive, product_root if configured else None)
    target.mkdir()
    bind_directory_target(target, "cold", roots=roots)
    with ArchiveSession(archive, mode=ArchiveAccessMode.READ) as session:
        audio = dict(session.connection.execute("SELECT * FROM audio_objects").fetchone())
    path = archive / audio["storage_key"]
    source_item = PackageSource(audio["sha256"], path, audio["storage_key"], audio["sha256"],
                                audio["byte_size"], capture_source_generation(path))
    package = create_artifact_package(target, [source_item], operation_id="fixture-one",
                                       plan_sha256="a" * 64, batch_index=0)
    replica = _record_package(roots, package, verified_at=1)
    with ArchiveSession(archive, mode=ArchiveAccessMode.WRITE) as session, session.connection:
        catalog = ArtifactCatalog(session.connection)
        for copy in catalog.replicas_for_object(audio["sha256"]):
            if copy["target_id"] == "local":
                catalog.observe_replica(copy["replica_id"], presence="released")
    path.unlink()
    return roots, target, audio, source_item, package, replica


def _record_package(roots, package, *, verified_at):
    entry = package["objects"][0]
    with ArchiveSession(roots.archive_root, mode=ArchiveAccessMode.WRITE) as session, session.connection:
        catalog = ArtifactCatalog(session.connection)
        catalog.register_target("cold")
        catalog.record_package(package["package_id"], "cold", package["package_key"],
                               sha256=package["package_sha256"], byte_size=package["package_size_bytes"],
                               manifest_sha256=package["manifest_sha256"], verified_at=verified_at)
        return catalog.record_verified_replica(entry["object_id"], "cold", package["package_key"],
                                                sha256=entry["sha256"], byte_size=entry["size_bytes"],
                                                package_id=package["package_id"], member_key=entry["member"],
                                                verified_at=verified_at)


def _authority(roots):
    with ArchiveSession(roots.archive_root, mode=ArchiveAccessMode.READ) as session:
        return [item for item in authority_fingerprints(session.connection) if not item["table"].startswith("artifact_")]


def _restore(roots, target, audio, **kwargs):
    return restore_service.restore_artifact(roots, audio["sha256"], target_id="cold",
                                            target_root=target, storage_key=audio["storage_key"], **kwargs)


@pytest.mark.parametrize("configured", [False, True])
def test_restoration_preserves_all_production_facts_and_registers_correct_local_target(tmp_path, configured):
    roots, target, audio, _source, _package, _replica = _setup(tmp_path, configured=configured)
    before = _authority(roots)
    result = _restore(roots, target, audio)
    assert _authority(roots) == before
    restored = roots.write_base / audio["storage_key"]
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == audio["sha256"]
    assert result["restored_bytes"] == audio["byte_size"] and result["verified_bytes"] == audio["byte_size"]
    assert result["container_reverified"] is False
    with ArchiveSession(roots.archive_root, mode=ArchiveAccessMode.READ) as session:
        catalog = ArtifactCatalog(session.connection)
        local = next(copy for copy in catalog.replicas_for_object(audio["sha256"])
                     if copy["replica_id"] == result["local_replica_id"])
        assert local["target_id"] == ("local-artifacts" if configured else "local")
        assert local["generation"] == (1 if configured else 2)
        assert local["presence"] == "present"
        assert session.connection.execute("SELECT state FROM artifact_transfers WHERE transfer_id=?",
                                           (result["operation_id"],)).fetchone()[0] == "complete"
        assert session.connection.execute("SELECT state FROM artifact_transfer_items WHERE transfer_id=?",
                                           (result["operation_id"],)).fetchone()[0] == "restored"


def test_repeated_restore_reuses_verified_local_bytes_even_when_package_goes_missing(tmp_path):
    roots, target, audio, _source, package, _replica = _setup(tmp_path)
    first = _restore(roots, target, audio)
    Path(package["package_path"]).unlink()
    second = _restore(roots, target, audio)
    assert second["reused_local"] and second["restored_bytes"] == 0
    assert second["local_replica_id"] == first["local_replica_id"]
    assert _authority(roots)


@pytest.mark.parametrize("unavailable", ["missing", "corrupt"])
def test_unavailable_replica_falls_back_to_another_currently_verified_member(tmp_path, unavailable):
    roots, target, audio, source, package, first_replica = _setup(tmp_path)
    # Use the exact retained content instead of guessing a replacement download.
    with zipfile.ZipFile(package["package_path"]) as bundle:
        source.source_path.write_bytes(bundle.read(package["objects"][0]["member"]))
    selected = PackageSource(source.object_id, source.source_path, source.storage_key, source.sha256,
                              source.size_bytes, capture_source_generation(source.source_path))
    later = create_artifact_package(target, [selected], operation_id="fixture-two", plan_sha256="b" * 64, batch_index=0)
    second_replica = _record_package(roots, later, verified_at=2)
    source.source_path.unlink()
    later_path = Path(later["package_path"])
    if unavailable == "missing":
        later_path.unlink()
    else:
        with zipfile.ZipFile(later_path) as bundle:
            member = bundle.getinfo(later["objects"][0]["member"])
        with later_path.open("r+b") as writer:
            writer.seek(member.header_offset)
            header = struct.unpack("<4s5H3I2H", writer.read(30))
            writer.seek(member.header_offset + 30 + header[-2] + header[-1])
            writer.write(b"!")
    result = _restore(roots, target, audio)
    assert result["failed_replicas"][0]["replica_id"] == second_replica
    with ArchiveSession(roots.archive_root, mode=ArchiveAccessMode.READ) as session:
        copies = {copy["replica_id"]: copy for copy in ArtifactCatalog(session.connection).replicas_for_object(audio["sha256"])}
        assert copies[second_replica]["presence"] == ("missing" if unavailable == "missing" else "unknown")
        assert copies[first_replica]["presence"] == "present"


def test_corrupt_member_is_never_installed_and_restore_failure_is_recorded(tmp_path):
    roots, target, audio, _source, package, replica = _setup(tmp_path)
    path = Path(package["package_path"])
    corrupted = target / "corrupted.zip"
    with zipfile.ZipFile(path) as incoming, zipfile.ZipFile(corrupted, "w") as outgoing:
        for member in incoming.infolist():
            content = incoming.read(member)
            if member.filename.startswith("objects/"):
                content = b"!" + content[1:]
            outgoing.writestr(member, content)
    path.write_bytes(corrupted.read_bytes())
    before = _authority(roots)
    with pytest.raises(restore_service.ArtifactRestoreError, match="no accessible valid package"):
        _restore(roots, target, audio)
    assert not (roots.write_base / audio["storage_key"]).exists()
    assert _authority(roots) == before
    with ArchiveSession(roots.archive_root, mode=ArchiveAccessMode.READ) as session:
        assert session.connection.execute("SELECT state,error_code FROM artifact_transfers").fetchone()[:] == ("failed", "restore_unavailable")
        assert session.connection.execute("SELECT presence FROM artifact_replicas WHERE replica_id=?", (replica,)).fetchone()[0] == "unknown"


def test_current_manifest_must_match_retained_container_evidence(tmp_path):
    roots, target, audio, _source, _package, _replica = _setup(tmp_path)
    with ArchiveSession(roots.archive_root, mode=ArchiveAccessMode.WRITE) as session, session.connection:
        session.connection.execute("UPDATE artifact_packages SET manifest_sha256=?", ("f" * 64,))
    with pytest.raises(restore_service.ArtifactRestoreError, match="no accessible valid package"):
        _restore(roots, target, audio)
    assert not (roots.write_base / audio["storage_key"]).exists()


def test_restore_refuses_other_existing_bytes_and_unretained_audio_paths(tmp_path):
    roots, target, audio, _source, _package, _replica = _setup(tmp_path)
    destination = roots.write_base / audio["storage_key"]
    destination.write_bytes(b"different")
    with pytest.raises(restore_service.ArtifactRestoreError, match="different bytes"):
        _restore(roots, target, audio)
    assert destination.read_bytes() == b"different"
    with pytest.raises(restore_service.ArtifactRestoreError, match="retained audio reference"):
        restore_service.restore_artifact(roots, audio["sha256"], target_id="cold", target_root=target,
                                         storage_key="audio/other.m4a")
    with pytest.raises(restore_service.ArtifactRestoreError, match="retained artifact reference"):
        restore_service.restore_artifact(roots, audio["sha256"], target_id="cold", target_root=target,
                                         storage_key="documents/other.md")


def test_no_space_and_missing_mount_do_not_recreate_or_publish_input(tmp_path, monkeypatch):
    roots, target, audio, _source, _package, _replica = _setup(tmp_path)
    monkeypatch.setattr(restore_service.shutil, "disk_usage", lambda _path: shutil._ntuple_diskusage(10, 10, 0))
    with pytest.raises(restore_service.ArtifactRestoreError, match="insufficient local free space"):
        _restore(roots, target, audio)
    assert not (roots.write_base / audio["storage_key"]).exists()
    missing = tmp_path / "missing-mount"
    with pytest.raises(ValueError, match="existing directory"):
        _restore(roots, missing, audio)
    assert not missing.exists()
    (target / MARKER).write_text('{"version":1,"target_id":"other"}')
    with pytest.raises(ValueError, match="identity mismatch"):
        _restore(roots, target, audio)


def test_restore_requires_explicit_catalog_upgrade_and_exclusive_archive_access(tmp_path):
    legacy = tmp_path / "legacy"
    expected = frozen_archive(legacy)
    target = tmp_path / "cold"
    target.mkdir()
    roots = ArtifactRoots.of(legacy)
    bind_directory_target(target, "cold", roots=roots)
    identity = expected["files"]["audio/BVpreserve.p0.m4a"]
    with pytest.raises(SchemaContractError, match="explicit"):
        restore_service.restore_artifact(roots, identity, target_id="cold", target_root=target,
                                         storage_key="audio/BVpreserve.p0.m4a")
    upgraded = tmp_path / "upgraded"
    upgrade_artifact_catalog(legacy, upgraded)
    roots = ArtifactRoots.of(upgraded)
    with archive_access(upgraded), pytest.raises(ArchiveBusyError):
        restore_service.restore_artifact(roots, identity, target_id="cold", target_root=target,
                                        storage_key="audio/BVpreserve.p0.m4a")


def test_install_before_catalog_commit_can_be_reconciled_by_idempotent_restore(tmp_path, monkeypatch):
    roots, target, audio, _source, _package, _replica = _setup(tmp_path)
    original = restore_service._register_local

    def fail_registration(*args, **kwargs):
        raise restore_service.ArtifactRestoreError("injected registration failure")

    monkeypatch.setattr(restore_service, "_register_local", fail_registration)
    with pytest.raises(restore_service.ArtifactRestoreError, match="injected"):
        _restore(roots, target, audio)
    assert (roots.write_base / audio["storage_key"]).is_file()
    monkeypatch.setattr(restore_service, "_register_local", original)
    result = _restore(roots, target, audio)
    assert result["reused_local"] and result["restored_bytes"] == 0
