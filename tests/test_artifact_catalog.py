from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import closing

import pytest

from bili_asr.archive_maintenance import ArchiveBusyError
from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.services.archive_migration import migrate_archive
from bili_asr.services.archive_snapshot import check_snapshot, save_snapshot
from bili_asr.services.artifact_catalog_upgrade import (
    ArtifactCatalogUpgradeError,
    authority_fingerprints,
    upgrade_artifact_catalog,
)
from bili_asr.storage.archive_contracts import (
    _resource,
    bootstrap_contract,
    runtime_contract,
)
from bili_asr.storage.artifact_catalog import (
    ArtifactCatalog,
    ArtifactCatalogError,
    require_artifact_catalog,
    storage_key_parts,
)
from bili_asr.storage.database import (
    SchemaContractError,
    connect_database,
    require_archive_schema,
)
from bili_asr.storage.snapshots import (
    _current_contract,
    required_artifacts,
    validate_snapshot_database,
)
from tests.fixtures.frozen_migration_archive import frozen_archive


@pytest.fixture
def catalog(tmp_path):
    with closing(connect_database(tmp_path / "archive.db")) as connection:
        bootstrap_contract(connection)
        connection.executescript(_resource("schema-artifact-storage.sql"))
        yield ArtifactCatalog(connection)


def test_objects_groups_replicas_and_pins_keep_business_and_byte_identity_separate(catalog):
    identity = hashlib.sha256(b"audio").hexdigest()
    with catalog.connection:
        assert catalog.register_object(identity, 5) == identity
        assert catalog.register_object(identity, 5) == identity
        with pytest.raises(ArtifactCatalogError, match="different byte size"):
            catalog.register_object(identity, 6)
        catalog.register_target("local", kind="local")
        catalog.register_target("cold-disc")
        local = catalog.record_verified_replica(identity, "local", "audio/test.m4a", sha256=identity, byte_size=5, verified_at=1)
        package_id = "b" * 64
        catalog.record_package(package_id, "cold-disc", "packages/audio.zip", sha256="c" * 64,
                               byte_size=123, manifest_sha256="d" * 64)
        external = catalog.record_verified_replica(identity, "cold-disc", "packages/audio.zip", sha256=identity,
                                                   byte_size=5, package_id=package_id, member_key=f"objects/{identity}")
        assert external != local
        for owner in ("1", "2"):
            catalog.register_group(f"part-{owner}", owner_kind="part", owner_id=owner, version="v1", role="audio", members={"audio": identity})
        pin = catalog.pin_object(identity, "preserve investigation", pin_id="manual-hold")
        assert catalog.pinned(identity)
        catalog.release_pin(pin)
        assert not catalog.pinned(identity)
        catalog.observe_replica(local, presence="released", observed_at=2)
        replicas = catalog.replicas_for_object(identity)
        assert len(replicas) == 2
        released = next(item for item in replicas if item["replica_id"] == local)
        assert released["verified_at"] == 1 and released["presence"] == "released"
        require_archive_schema(catalog.connection)


def test_replica_generation_and_package_location_cannot_be_rebound(catalog):
    identity = catalog.register_object("a" * 64, 5)
    other = catalog.register_object("b" * 64, 5)
    catalog.register_target("local", kind="local")
    catalog.register_target("cold")
    replica = catalog.record_verified_replica(identity, "local", "audio/test.m4a", sha256=identity, byte_size=5)
    with pytest.raises(ArtifactCatalogError, match="different bytes"):
        catalog.record_verified_replica(other, "local", "audio/test.m4a", sha256=other, byte_size=5)
    assert catalog.record_verified_replica(other, "local", "audio/test.m4a", sha256=other, byte_size=5, generation=2) != replica
    assert catalog.replicas_for_object(identity)[0]["presence"] == "unknown"
    with pytest.raises(ArtifactCatalogError, match="supplied together"):
        catalog.record_verified_replica(identity, "cold", "packages/x.zip", sha256=identity, byte_size=5, package_id="package")
    catalog.record_package("package", "cold", "packages/x.zip", sha256="c" * 64, byte_size=90, manifest_sha256="d" * 64)
    with pytest.raises(ArtifactCatalogError, match="another storage location"):
        catalog.record_verified_replica(identity, "local", "packages/x.zip", sha256=identity, byte_size=5, package_id="package", member_key="objects/x")


@pytest.mark.parametrize("key", ["/absolute", "C:/absolute", "../escape", "audio/../escape", "audio\\name", "audio/NUL.m4a", "http://remote/x", "", "x//y"])
def test_storage_keys_are_portable_and_confined(key):
    with pytest.raises(ValueError):
        storage_key_parts(key)


def test_transfer_replica_binding_and_caller_transaction_rollback(catalog):
    identity = catalog.register_object("a" * 64, 5)
    other = catalog.register_object("b" * 64, 5)
    catalog.register_target("local", kind="local")
    replica = catalog.record_verified_replica(other, "local", "audio/b.m4a", sha256=other, byte_size=5)
    catalog.begin_transfer("one", kind="offload", plan_sha256="c" * 64)
    with pytest.raises(ArtifactCatalogError, match="different bytes"):
        catalog.record_transfer_item("one", identity, state="verified", target_replica_id=replica)
    catalog.connection.commit()
    with pytest.raises(RuntimeError), catalog.connection:
        catalog.register_group("rollback", owner_kind="part", owner_id="1", version="v1", role="audio", members={"audio": identity})
        raise RuntimeError("caller failed")
    assert catalog.connection.execute("SELECT 1 FROM artifact_groups WHERE group_id='rollback'").fetchone() is None


def test_existing_contract_digest_and_open_do_not_implicitly_install_catalog(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    expected = _current_contract()[1]
    before = (source / "archive.db").read_bytes()
    with ArchiveSession(source, mode=ArchiveAccessMode.WRITE) as session:
        assert not require_artifact_catalog(session.connection)
        with pytest.raises(SchemaContractError, match="explicit"):
            ArtifactCatalog(session.connection)
    assert validate_snapshot_database(source / "archive.db") == expected
    assert (source / "archive.db").read_bytes() == before


@pytest.mark.parametrize("breakage", ["missing-table", "unknown-version", "altered-table", "unsafe-key"])
def test_partial_unknown_or_altered_catalog_is_rejected_read_only(catalog, breakage):
    connection = catalog.connection
    if breakage == "missing-table":
        connection.execute("DROP TABLE artifact_policies")
    elif breakage == "unknown-version":
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE artifact_storage_contract SET version=9")
    elif breakage == "altered-table":
        connection.execute("ALTER TABLE artifact_objects ADD COLUMN surprise TEXT")
    else:
        catalog.register_object("a" * 64, 1)
        catalog.register_target("local", kind="local")
        connection.execute("INSERT INTO artifact_replicas VALUES (1,?,'local','../escape',NULL,'',1,?,1,1,'present',1)", ("a" * 64, "a" * 64))
    connection.commit()
    before = connection.total_changes
    with pytest.raises(SchemaContractError):
        require_archive_schema(connection)
    assert connection.total_changes == before


@pytest.mark.parametrize("contract", ["legacy", "universal"])
def test_isolated_upgrade_preserves_complete_history_rows_and_files(tmp_path, contract):
    original, source, target = tmp_path / "legacy", tmp_path / "source", tmp_path / "upgraded"
    expected = frozen_archive(original)
    if contract == "universal":
        migrate_archive(original, source)
    else:
        source = original
    before_db = (source / "archive.db").read_bytes()
    with closing(connect_database(source / "archive.db", readonly=True)) as connection:
        before_rows = authority_fingerprints(connection)
        before_kind = runtime_contract(connection)
    dry = upgrade_artifact_catalog(source, target, dry_run=True)
    assert not dry["installed"] and not target.exists()
    report = upgrade_artifact_catalog(source, target)
    assert report["installed"] and report["authority_tables"] == before_rows
    assert report["recovery_changes"] == []
    assert (source / "archive.db").read_bytes() == before_db
    with ArchiveSession(target, mode=ArchiveAccessMode.READ) as session:
        assert runtime_contract(session.connection) == before_kind
        catalog = ArtifactCatalog(session.connection)
        audio = session.connection.execute("SELECT audio_id,sha256 FROM audio_objects").fetchone()
        assert catalog.audio_object(audio[0]) == audio[1]
        assert len(catalog.replicas_for_object(audio[1])) >= 1
        assert validate_snapshot_database(target / "archive.db") != validate_snapshot_database(source / "archive.db")
    for key, digest in expected["files"].items():
        assert hashlib.sha256(target.joinpath(*key.split("/")).read_bytes()).hexdigest() == digest
    assert upgrade_artifact_catalog(source, target)["reused"]
    with ArchiveSession(target, mode=ArchiveAccessMode.READ) as session:
        row = session.connection.execute("SELECT report_key,report_sha256 FROM artifact_catalog_upgrades").fetchone()
        assert required_artifacts(target / "archive.db")[row[0]] == row[1]


@pytest.mark.parametrize("missing", [True, False])
def test_backfill_does_not_claim_missing_or_overwritten_audio_is_verified(tmp_path, missing):
    source, target = tmp_path / "source", tmp_path / "target"
    frozen_archive(source)
    audio = source / "audio/BVpreserve.p0.m4a"
    if missing:
        audio.unlink()
    else:
        audio.write_bytes(b"different generation")
    report = upgrade_artifact_catalog(source, target)
    assert report["audio_findings"][0]["state"] == ("missing" if missing else "identity_mismatch")
    assert not report["snapshot_readiness"]["reference_bytes_available"]
    assert report["snapshot_readiness"]["scope"] == "declared-reference-bytes"
    assert report["snapshot_readiness"]["bundle_integrity_verified"] is False
    with ArchiveSession(target, mode=ArchiveAccessMode.READ) as session:
        assert session.connection.execute("SELECT COUNT(*) FROM artifact_replicas").fetchone()[0] == 0
        assert session.connection.execute("SELECT COUNT(*) FROM artifact_audio_bindings").fetchone()[0] == 1


def test_upgrade_failure_before_install_keeps_source_and_target_untouched(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    frozen_archive(source)
    before = (source / "archive.db").read_bytes()
    def fail_sync(fd):
        raise OSError("disk failure")
    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(OSError, match="disk failure"):
        upgrade_artifact_catalog(source, target)
    assert (source / "archive.db").read_bytes() == before and not target.exists()
    assert not list(tmp_path.glob(".target.artifact-upgrade-*"))


def test_upgrade_refuses_overlapping_or_nonempty_target(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    with pytest.raises(ArtifactCatalogUpgradeError, match="disjoint"):
        upgrade_artifact_catalog(source, source / "new")
    target = tmp_path / "nonempty"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    with pytest.raises(ArtifactCatalogUpgradeError, match="empty"):
        upgrade_artifact_catalog(source, target)
    assert (target / "keep.txt").read_text() == "keep"


def test_upgrade_refuses_active_shared_writer(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    with ArchiveSession(source, mode=ArchiveAccessMode.WRITE), pytest.raises(ArchiveBusyError):
        upgrade_artifact_catalog(source, tmp_path / "target")
    assert not (tmp_path / "target").exists()


def test_upgrade_preserves_running_and_retry_states_without_recovery(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    frozen_archive(source)
    with closing(connect_database(source / "archive.db")) as connection, connection:
        connection.execute("UPDATE workflow_jobs SET status='running',lease_owner='still-recorded',lease_expires_at=9999999999 WHERE kind='audio'")
        connection.execute("UPDATE workflow_attempts SET outcome='running',finished_at=NULL WHERE job_id IN (SELECT job_id FROM workflow_jobs WHERE kind='audio')")
        original = authority_fingerprints(connection)
    report = upgrade_artifact_catalog(source, target)
    assert report["authority_tables"] == original and report["recovery_changes"] == []
    with ArchiveSession(target, mode=ArchiveAccessMode.READ) as session:
        assert session.connection.execute("SELECT COUNT(*) FROM workflow_jobs WHERE status='running' AND lease_owner='still-recorded'").fetchone()[0] >= 1
        assert session.connection.execute("SELECT COUNT(*) FROM workflow_attempts WHERE outcome='running' AND finished_at IS NULL").fetchone()[0] >= 1


def test_upgrade_preserves_ordered_configured_artifact_roots(tmp_path):
    source, target, configured = tmp_path / "source", tmp_path / "target", tmp_path / "media"
    frozen_archive(source)
    configured.mkdir()
    (configured / "audio").mkdir()
    original_audio = source / "audio/BVpreserve.p0.m4a"
    audio_bytes = original_audio.read_bytes()
    (configured / "audio/BVpreserve.p0.m4a").write_bytes(audio_bytes)
    original_audio.unlink()
    report = upgrade_artifact_catalog(source, target, artifact_roots=ArtifactRoots.of(source, configured))
    assert report["audio_findings"][0]["state"] == "verified"
    assert (target / "audio/BVpreserve.p0.m4a").read_bytes() == audio_bytes


def test_repeated_upgrade_detects_source_byte_changes_and_target_history_forks(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    frozen_archive(source)
    upgrade_artifact_catalog(source, target)
    (source / "audio/BVpreserve.p0.m4a").write_bytes(b"modified")
    with pytest.raises(ArtifactCatalogUpgradeError, match="source artifact bytes differ"):
        upgrade_artifact_catalog(source, target)
    with ArchiveSession(target, mode=ArchiveAccessMode.WRITE) as session, session.connection:
        session.connection.execute("UPDATE workflow_jobs SET priority=priority+1")
    with pytest.raises(ArtifactCatalogUpgradeError, match="diverged"):
        upgrade_artifact_catalog(source, target)


def test_same_immutable_package_can_have_multiple_physical_target_copies(catalog):
    identity = catalog.register_object("a" * 64, 4)
    for target in ("disc-one", "disc-two"):
        catalog.register_target(target)
        catalog.record_package("shared-package", target, "packages/shared.zip", sha256="b" * 64, byte_size=100, manifest_sha256="c" * 64)
        catalog.record_verified_replica(identity, target, "packages/shared.zip", sha256=identity, byte_size=4, package_id="shared-package", member_key="objects/audio")
    assert len(catalog.replicas_for_object(identity)) == 2


def test_upgrade_preserves_shadowed_audio_bytes_and_registers_actual_correct_location(tmp_path):
    source, target, configured = tmp_path / "source", tmp_path / "target", tmp_path / "media"
    frozen_archive(source)
    original_audio = source / "audio/BVpreserve.p0.m4a"
    content = original_audio.read_bytes()
    identity = hashlib.sha256(content).hexdigest()
    configured.mkdir()
    (configured / "audio").mkdir()
    shadow = b"new shadowing generation"
    shadow_sha256 = hashlib.sha256(shadow).hexdigest()
    (configured / "audio/BVpreserve.p0.m4a").write_bytes(shadow)
    report = upgrade_artifact_catalog(source, target, artifact_roots=ArtifactRoots.of(source, configured))
    assert (target / "audio/BVpreserve.p0.m4a").read_bytes() == content
    preserved_key = f"audio/catalog-copy-{shadow_sha256}.m4a"
    assert (target / preserved_key).read_bytes() == shadow
    assert report["audio_findings"][0]["state"] == "verified"
    assert report["snapshot_readiness"]["reference_bytes_available"]
    assert report["snapshot_readiness"]["bundle_integrity_verified"] is False
    with ArchiveSession(target, mode=ArchiveAccessMode.READ) as session:
        replicas = ArtifactCatalog(session.connection).replicas_for_object(identity)
        assert [item["relative_key"] for item in replicas] == ["audio/BVpreserve.p0.m4a"]
    assert required_artifacts(target / "archive.db")[preserved_key] == shadow_sha256
    assert upgrade_artifact_catalog(source, target, artifact_roots=ArtifactRoots.of(source, configured))["reused"]


@pytest.mark.parametrize("invalid", ["source-key", "generation", "replica-binding"])
def test_release_intents_require_safe_paths_and_exact_physical_generation(catalog, invalid):
    catalog.register_object("a" * 64, 4)
    catalog.register_target("local", kind="local")
    replica = catalog.record_verified_replica("a" * 64, "local", "audio/original.m4a", sha256="a" * 64, byte_size=4)
    catalog.begin_transfer("one", kind="offload", plan_sha256="b" * 64)
    key = "../escape" if invalid == "source-key" else "audio/original.m4a"
    generation = {"device": 1, "inode": 1, "size_bytes": 4, "mtime_ns": 1, "ctime_ns": 1}
    if invalid == "generation":
        generation["size_bytes"] = False
    if invalid == "replica-binding":
        key = "audio/wrong.m4a"
    catalog.connection.execute("INSERT INTO artifact_release_intents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               ("one", "copy-1", "a" * 64, "local", key, "audio/quarantine.m4a", replica,
                                json.dumps(generation), "planned", 1, 1))
    catalog.connection.commit()
    with pytest.raises(SchemaContractError):
        require_artifact_catalog(catalog.connection, required=True)


def test_upgrade_preserves_populated_import_and_supplement_extensions(tmp_path):
    from bili_asr.services.preserved_body_import import (
        apply_preserved_body_import,
        install_preserved_body_extension,
        plan_preserved_body_import,
    )
    from bili_asr.services.source_supplement import (
        apply_source_supplement,
        install_source_supplement_extension,
        plan_source_supplement,
    )
    original, source, target = tmp_path / "legacy", tmp_path / "source", tmp_path / "upgraded"
    expected = frozen_archive(original)
    migrate_archive(original, source)
    install_preserved_body_extension(source)
    install_source_supplement_extension(source)
    with ArchiveSession(source, mode=ArchiveAccessMode.WRITE) as session:
        plan = plan_preserved_body_import(session.connection, selectors=[{"kind": "edition", "id": expected["ids"]["edition_current"]}], artifact_roots=(source,))
        result = apply_preserved_body_import(session.connection, plan=plan, artifact_roots=(source,), write_root=source, actor="test-importer")
        edition_id = result["editions"][0]["editionId"]
        with session.connection:
            session.connection.execute("UPDATE video_parts SET title='observed preserved part title' WHERE video_part_id=1")
        plan = plan_source_supplement(session.connection, edition_ids=[edition_id], artifact_roots=(source,))
        apply_source_supplement(session.connection, plan=plan, artifact_roots=(source,), actor="test-operator")
        before = authority_fingerprints(session.connection)
    report = upgrade_artifact_catalog(source, target)
    assert report["authority_tables"] == before
    with ArchiveSession(target, mode=ArchiveAccessMode.READ) as session:
        assert session.connection.execute("SELECT COUNT(*) FROM manuscript_import_baselines").fetchone()[0] == 1
        assert session.connection.execute("SELECT COUNT(*) FROM source_metadata_supplements").fetchone()[0] == 1
    required_artifacts(target / "archive.db")
    assert upgrade_artifact_catalog(source, target)["reused"]


@pytest.mark.parametrize("invalid", ["package", "upgrade-report"])
def test_package_and_upgrade_evidence_keys_cannot_be_absolute(catalog, invalid):
    from bili_asr.canonical_json import digest
    catalog.register_target("cold")
    if invalid == "package":
        catalog.connection.execute("INSERT INTO artifact_packages VALUES ('package','cold','C:/private/package.zip',?,1,?,1)", ("a" * 64, "b" * 64))
    else:
        catalog.connection.execute("INSERT INTO artifact_catalog_upgrades VALUES ('upgrade','source','C:/private/report.json',?,?,1)", ("a" * 64, digest([])))
    catalog.connection.commit()
    with pytest.raises(SchemaContractError):
        require_artifact_catalog(catalog.connection, required=True)


def test_ambiguous_historical_audio_generations_refuse_upgrade_without_modifying_source(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    frozen_archive(source)
    with closing(connect_database(source / "archive.db")) as connection, connection:
        row = connection.execute("SELECT attempt_id,result_json FROM workflow_attempts a JOIN workflow_jobs j USING(job_id) WHERE a.outcome='succeeded' AND j.kind='audio'").fetchone()
        claim = json.loads(row[1])
        claim["sha256"] = "f" * 64
        connection.execute("UPDATE workflow_attempts SET result_json=? WHERE attempt_id=?", (json.dumps(claim), row[0]))
    before = (source / "archive.db").read_bytes()
    with pytest.raises(ArtifactCatalogUpgradeError, match="multiple digests"):
        upgrade_artifact_catalog(source, target)
    assert not target.exists() and (source / "archive.db").read_bytes() == before


def test_preserved_bundle_marker_extras_are_opaque_and_do_not_freeze_mutable_canonical_bundle(tmp_path):
    source, target, configured = tmp_path / "source", tmp_path / "target", tmp_path / "media"
    frozen_archive(source)
    configured.mkdir()
    shutil.copytree(source / "transcripts", configured / "transcripts")
    txt = configured / "transcripts/BVpreserve.p0/bundle.txt"
    txt.write_bytes(b"new preferred mutable bundle")
    marker = configured / "transcripts/BVpreserve.p0/.bundle-ready"
    document = json.loads(marker.read_bytes())
    document["artifacts"]["txt_path"]["sha256"] = hashlib.sha256(txt.read_bytes()).hexdigest()
    marker.write_text(json.dumps(document), encoding="ascii")
    report = upgrade_artifact_catalog(source, target, artifact_roots=ArtifactRoots.of(source, configured))
    extra_marker = next(item["path"] for item in report["preserved_extras"] if item["path"].endswith(".bundle-ready.preserved"))
    required = required_artifacts(target / "archive.db")
    assert required["transcripts/BVpreserve.p0/bundle.txt"] is None
    assert extra_marker in required
    package = tmp_path / "after-upgrade.zip"
    save_snapshot(target, package)
    assert check_snapshot(package)["valid"]
    # A subsequent normal production bundle may replace its mutable slot; the
    # preservation evidence remains a separate immutable extra.
    txt = target / "transcripts/BVpreserve.p0/bundle.txt"
    txt.write_bytes(b"later production bundle")
    marker = target / "transcripts/BVpreserve.p0/.bundle-ready"
    document = json.loads(marker.read_bytes())
    document["artifacts"]["txt_path"]["sha256"] = hashlib.sha256(txt.read_bytes()).hexdigest()
    marker.write_text(json.dumps(document), encoding="ascii")
    later = tmp_path / "later-production.zip"
    save_snapshot(target, later)
    assert check_snapshot(later)["valid"]
