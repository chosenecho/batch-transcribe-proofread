"""Offline release fault injection; no live archive or media services involved."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from bili_asr.artifact_root import ArtifactRoots
from bili_asr.services import artifact_release
from bili_asr.services import artifact_transfer as service
from bili_asr.services.artifact_inventory_service import (
    ArtifactSelection,
    inventory_artifacts,
    plan_artifact_offload,
    plan_artifact_copy,
)
from bili_asr.services.artifact_restore import restore_artifact
from bili_asr.storage.artifact_catalog import ArtifactCatalog
from bili_asr.storage.database import initialize_schema
from bili_asr.storage_targets import bind_directory_target


@pytest.fixture
def archive(tmp_path):
    root, target = tmp_path / "archive", tmp_path / "target"
    root.mkdir()
    target.mkdir()
    connection = sqlite3.connect(root / "archive.db")
    initialize_schema(connection)
    connection.execute("INSERT INTO bilibili_users VALUES (1,'creator',1,1)")
    connection.execute("INSERT INTO videos VALUES ('BVTEST',1,1,'video',1,1,1)")
    connection.execute("INSERT INTO video_parts VALUES (1,'BVTEST',0,1,'part',1000,'discovered',1,1)")
    data = b"retained sound"
    digest = hashlib.sha256(data).hexdigest()
    connection.execute("INSERT INTO audio_objects VALUES (1,?,?, 'm4a',1000,'audio/input.m4a',1)", (digest, len(data)))
    connection.execute("INSERT INTO part_audio_objects VALUES (1,1,1,'test')")
    connection.commit()
    schema = Path(service.__file__).parents[1] / "storage" / "schema-artifact-storage.sql"
    connection.executescript(schema.read_text())
    connection.close()
    (root / "audio").mkdir()
    (root / "audio/input.m4a").write_bytes(data)
    roots = ArtifactRoots.of(root)
    bind_directory_target(target, "cold", roots=roots)
    return SimpleNamespace(root=root, roots=roots, target=target, digest=digest, data=data)


def plan(archive, roots=None):
    roots = roots or archive.roots
    return plan_artifact_offload(inventory_artifacts(archive.root / "archive.db", roots, deep=True,
                                                    selection=ArtifactSelection(kinds=("audio",)), external_holds={}), target_id="cold")


def transfer(archive, frozen, **options):
    return service.transfer_artifacts(archive.roots, frozen, target_root=archive.target, external_holds={}, **options)


def domain_facts(archive):
    with sqlite3.connect(archive.root / "archive.db") as connection:
        return {name: list(connection.execute(f'SELECT * FROM "{name}"')) for name, in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'artifact_%' AND name NOT LIKE 'sqlite_%'")}


def test_copy_offload_restore_reuse_preserves_all_domain_facts(archive):
    frozen = plan(archive)
    before = domain_facts(archive)
    copied = transfer(archive, frozen)
    assert copied["operation"]["kind"] == "copy"
    assert copied["verified_payload_bytes"] == len(archive.data)
    assert copied["released_bytes_this_run"] == 0
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data
    offloaded = transfer(archive, frozen, mode="offload")
    assert offloaded["released_bytes_this_run"] == len(archive.data)
    assert offloaded["released_copies"] == 1
    assert not (archive.root / "audio/input.m4a").exists()
    with sqlite3.connect(archive.root / "archive.db") as connection:
        assert connection.execute("SELECT object_id FROM artifact_audio_bindings WHERE audio_id=1").fetchone()[0] == archive.digest
    again = transfer(archive, frozen, mode="offload")
    assert again["verified_payload_bytes"] == len(archive.data)
    assert again["released_bytes_this_run"] == 0
    restored = restore_artifact(archive.roots, archive.digest, target_id="cold", target_root=archive.target, storage_key="audio/input.m4a")
    assert restored["verified_bytes"] == len(archive.data)
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data
    assert domain_facts(archive) == before


@pytest.mark.parametrize("copy_contract", [False, True])
@pytest.mark.parametrize("transfer_state", ["complete", "failed"])
def test_seeded_copy_release_intent_is_rejected_without_touching_source(archive, copy_contract, transfer_state):
    from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
    from bili_asr.storage.artifact_releases import ArtifactReleaseRepository
    frozen = (plan_artifact_copy(inventory_artifacts(archive.root / "archive.db", archive.roots,
        deep=True, external_holds={}), target_id="cold") if copy_contract else plan(archive))
    copied = transfer(archive, frozen)
    operation_id = copied["operation"]["transfer_id"]
    item = frozen["items"][0]
    generation = service.capture_source_generation(archive.root / item["path"])
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        connection = session.connection
        source_id = connection.execute("SELECT source_replica_id FROM artifact_transfer_items WHERE transfer_id=?", (operation_id,)).fetchone()[0]
        values = dict(transfer_id=operation_id, copy_id=item["copy_id"], object_id=item["sha256"],
            source_target_id="local", source_key=item["path"],
            quarantine_key=service._quarantine_key(item["path"], operation_id, item["copy_id"]),
            source_replica_id=source_id, source_generation=generation)
        with pytest.raises(ValueError, match="require an offload transfer"):
            ArtifactReleaseRepository(connection).plan(**values)
        # Simulate an old/manual invalid journal bypassing the repository API.
        with connection:
            connection.execute("INSERT INTO artifact_release_intents VALUES (?,?,?,?,?,?,?,?, 'planned',1,1)",
                (operation_id, item["copy_id"], item["sha256"], "local", item["path"],
                 values["quarantine_key"], source_id, json.dumps(generation)))
            connection.execute("UPDATE artifact_transfers SET state=? WHERE transfer_id=?", (transfer_state, operation_id))
        with pytest.raises(ValueError, match="copy transfer cannot execute"):
            service._release_intents(ArtifactCatalog(connection), archive.roots, None, frozen,
                operation_id, {}, mode="copy")
    with pytest.raises(ValueError, match="copy transfer cannot execute"):
        transfer(archive, frozen, mode="copy")
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data
    assert not list((archive.root / "audio").glob(".artifact-release-*"))
    with sqlite3.connect(archive.root / "archive.db") as connection:
        assert connection.execute("SELECT state FROM artifact_release_intents WHERE transfer_id=?", (operation_id,)).fetchone()[0] == "planned"


def test_release_helper_defends_schema_and_persisted_transfer_kind(archive):
    from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
    frozen = plan(archive)
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        catalog = ArtifactCatalog(session.connection)
        with session.connection:
            catalog.register_target("cold")
            catalog.begin_transfer("offload-" + frozen["plan_sha256"], kind="copy",
                plan_sha256=frozen["plan_sha256"], target_id="cold")
        with pytest.raises(ValueError, match="matching persisted offload authority"):
            service._release_intents(catalog, archive.roots, None, frozen,
                "offload-" + frozen["plan_sha256"], {}, mode="offload")
        copy = plan_artifact_copy(inventory_artifacts(archive.root / "archive.db", archive.roots,
            deep=True, external_holds={}), target_id="cold")
        with pytest.raises(ValueError, match="copy transfer cannot execute"):
            service._release_intents(catalog, archive.roots, None, copy,
                "offload-" + frozen["plan_sha256"], {}, mode="offload")
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data


@pytest.mark.parametrize("stage", ["copy", "register", "reread"])
def test_fault_before_release_keeps_input_and_same_plan_can_resume(archive, monkeypatch, stage):
    frozen = plan(archive)
    attribute = {"copy": "create_artifact_package", "register": "_register_package", "reread": "check_artifact_package"}[stage]
    original = getattr(service, attribute)

    def fail(*args, **kwargs):
        raise OSError("injected target failure")

    monkeypatch.setattr(service, attribute, fail)
    with pytest.raises(OSError):
        transfer(archive, frozen, mode="offload")
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data
    monkeypatch.setattr(service, attribute, original)
    outcome = transfer(archive, frozen, mode="offload")
    assert outcome["operation"]["state"] == "complete"
    assert outcome["verified_payload_bytes"] == len(archive.data)


def test_crash_after_isolation_reconciles_without_new_download(archive, monkeypatch):
    frozen = plan(archive)
    original = artifact_release.os.unlink
    fired = False

    def fail_once(path, *args, **kwargs):
        nonlocal fired
        if ".artifact-release-" in str(path) and not fired:
            fired = True
            raise OSError("injected isolated unlink failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(artifact_release.os, "unlink", fail_once)
    with pytest.raises(OSError):
        transfer(archive, frozen, mode="offload")
    assert list((archive.root / "audio").glob(".artifact-release-*"))
    monkeypatch.setattr(artifact_release.os, "unlink", original)
    result = service.reconcile_artifact_transfer(archive.roots, frozen, target_root=archive.target, external_holds={})
    assert result["operation"]["state"] == "complete"
    assert not list((archive.root / "audio").iterdir())
    assert result["verified_payload_bytes"] == len(archive.data)


def test_new_hold_after_isolation_restores_retained_source(archive, monkeypatch):
    frozen = plan(archive)
    original = artifact_release.os.unlink

    def fail(path, *args, **kwargs):
        if ".artifact-release-" in str(path):
            raise OSError("injected isolated unlink failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(artifact_release.os, "unlink", fail)
    with pytest.raises(OSError):
        transfer(archive, frozen, mode="offload")
    monkeypatch.setattr(artifact_release.os, "unlink", original)
    result = service.reconcile_artifact_transfer(archive.roots, frozen, target_root=archive.target,
                                               external_holds={f"sha256:{archive.digest}": ("investigation",)})
    assert result["operation"]["state"] == "failed"
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data
    assert not list((archive.root / "audio").glob(".artifact-release-*"))


def test_changed_path_pin_or_hold_rejects_stale_plan(archive):
    frozen = plan(archive)
    with pytest.raises(ValueError, match="retention"):
        service.transfer_artifacts(archive.roots, frozen, target_root=archive.target, mode="offload", external_holds={"part:1": ("retry",)})
    connection = service.open_archive_connection(archive.root, mode=service.ArchiveAccessMode.WRITE)
    catalog = ArtifactCatalog(connection)
    catalog.register_object(archive.digest, len(archive.data))
    pin_id = catalog.pin_object(archive.digest, "consumer")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="pinned|retention"):
        transfer(archive, frozen, mode="offload")
    connection = service.open_archive_connection(archive.root, mode=service.ArchiveAccessMode.WRITE)
    catalog = ArtifactCatalog(connection)
    catalog.release_pin(pin_id)
    connection.commit()
    connection.close()
    (archive.root / "audio/input.m4a").write_bytes(b"new generation")
    with pytest.raises(ValueError, match="changed"):
        transfer(archive, frozen, mode="offload")
    assert (archive.root / "audio/input.m4a").read_bytes() == b"new generation"


def test_live_hold_provider_rechecked_after_target_verification(archive):
    frozen = plan(archive)
    count = 0

    def observe():
        nonlocal count
        count += 1
        return {} if count == 1 else {"part:1": ("new hold",)}

    with pytest.raises(ValueError, match="retention"):
        service.transfer_artifacts(archive.roots, frozen, target_root=archive.target, mode="offload", external_holds=observe)
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data


def test_equal_bytes_in_both_roots_one_payload_two_copy_intents(archive, tmp_path):
    other = tmp_path / "other"
    (other / "audio").mkdir(parents=True)
    (other / "audio/input.m4a").write_bytes(archive.data)
    archive.roots = ArtifactRoots.of(archive.root, other)
    frozen = plan(archive)
    assert len(frozen["items"]) == 2
    result = transfer(archive, frozen, mode="offload")
    assert result["verified_payload_bytes"] == len(archive.data)
    assert result["released_copies"] == 2
    assert result["released_bytes_this_run"] == len(archive.data) * 2
    assert not (other / "audio/input.m4a").exists()
    assert not (archive.root / "audio/input.m4a").exists()


def test_target_capacity_identity_and_missing_mount_reject_without_release(archive, monkeypatch):
    frozen = plan(archive)
    monkeypatch.setattr(service.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(ValueError, match="space"):
        transfer(archive, frozen, mode="offload")
    (archive.target / ".bili-asr-storage-target.json").write_text(json.dumps({"version": 1, "target_id": "wrong"}))
    with pytest.raises(ValueError, match="mismatch"):
        transfer(archive, frozen, mode="offload")
    missing = archive.target / "missing"
    with pytest.raises(ValueError, match="existing"):
        service.transfer_artifacts(archive.roots, frozen, target_root=missing, external_holds={})
    assert not missing.exists()
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data


def test_hardlink_outside_inventory_blocks_release(archive, tmp_path):
    alias = tmp_path / "alias.m4a"
    try:
        os.link(archive.root / "audio/input.m4a", alias)
    except OSError:
        pytest.skip("hardlinks unavailable")
    frozen = plan(archive)
    assert not frozen["items"]
    with pytest.raises(ValueError, match="no eligible"):
        transfer(archive, frozen, mode="offload")
    assert alias.read_bytes() == archive.data


def test_sealed_unregistered_package_resumes_without_requiring_payload_space_twice(archive, monkeypatch):
    frozen = plan(archive)
    register = service._register_package

    def fail_registration(*args, **kwargs):
        raise OSError("package sealed before interrupted registration")

    monkeypatch.setattr(service, "_register_package", fail_registration)
    with pytest.raises(OSError):
        transfer(archive, frozen, mode="offload")
    assert list((archive.target / "packages").glob("*.zip"))
    assert (archive.root / "audio/input.m4a").exists()
    monkeypatch.setattr(service, "_register_package", register)
    monkeypatch.setattr(service.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    outcome = transfer(archive, frozen, mode="offload")
    assert outcome["operation"]["state"] == "complete"
    assert outcome["released_copies"] == 1
