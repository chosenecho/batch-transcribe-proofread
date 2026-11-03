"""Frozen historical bytes pass through real registered conversion and consumers."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from bili_asr.canonical_json import digest
from bili_asr.contracts.registry import (
    ARTIFACT_ONLINE,
    ARTIFACT_STORAGE,
    BILIBILI_V1,
    IMPORT_EXTENSION,
    SOURCE_SUPPLEMENT_POLICY,
    UNIVERSAL_V2,
    UPGRADE_EDGES,
    upgrade_path,
)
from bili_asr.services import archive_upgrade as upgrade
from bili_asr.services.archive_migration import initialize_archive, migrate_archive
from bili_asr.services.archive_snapshot import (
    check_snapshot,
    restore_snapshot,
    save_snapshot,
)
from bili_asr.publication import get_edition
from bili_asr.storage.database import connect_database
from tests.fixtures.frozen_migration_archive import frozen_archive
from tests.support.content_permissions import grant_test_permission

CURRENT = (UNIVERSAL_V2, IMPORT_EXTENSION, SOURCE_SUPPLEMENT_POLICY)


def _plan(source, target, contracts=CURRENT, **kwargs):
    return upgrade.plan_upgrade(source, target, target_contracts=contracts, **kwargs)


def _bytes(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("target_contracts", [(UNIVERSAL_V2,), (UNIVERSAL_V2, IMPORT_EXTENSION), CURRENT],
                         ids=["native", "preserved", "supplemented"])
def test_frozen_legacy_upgrades_all_registered_combinations_and_snapshots(tmp_path, target_contracts):
    source, target = tmp_path / "source", tmp_path / "target"
    expected = frozen_archive(source)
    before = _bytes(source)
    plan = _plan(source, target, target_contracts, no_external_control_state=True)
    assert not target.exists()
    assert plan["actions"]["expensive_recomputation"] == []
    result = upgrade.apply_upgrade(plan)
    assert result["valid"] and not result["reused"]
    assert upgrade.apply_upgrade(plan)["reused"]
    assert upgrade.check_upgrade(target, expected_plan_id=plan["plan_id"])["valid"]
    assert _bytes(source) == before
    for relative, sha256 in expected["files"].items():
        assert hashlib.sha256((target / relative).read_bytes()).hexdigest() == sha256
    archive = tmp_path / "upgraded.zip"
    save_snapshot(target, archive)
    assert check_snapshot(archive)["valid"]
    restored = tmp_path / "restored"
    restore_snapshot(archive, restored)
    with closing(connect_database(restored / "archive.db")) as writable:
        editions = [row[0] for row in writable.execute("SELECT edition_id FROM publication_editions")]
        for edition_id in editions:
            grant_test_permission(writable, get_edition(writable, edition_id), (restored,))
    from bili_asr.publication import verify_release
    from bili_asr.publication_export import (
        export_publication_drafts,
        export_publications,
    )
    with closing(connect_database(restored / "archive.db", readonly=True)) as connection:
        for status in ("current", "superseded", "withdrawn"):
            verify_release(connection, expected["ids"]["release_" + status], (restored,))
        assert export_publications(connection, artifact_roots=(restored,), output=tmp_path / "public") == 1
        assert export_publication_drafts(connection, artifact_roots=(restored,), output=tmp_path / "drafts") == 2


@pytest.mark.parametrize("migrated", [False, True])
def test_native_and_previously_migrated_universal_sources_keep_existing_rows(tmp_path, migrated):
    source, target = tmp_path / "source", tmp_path / "target"
    if migrated:
        legacy = tmp_path / "legacy"
        frozen_archive(legacy)
        migrate_archive(legacy, source)
    else:
        initialize_archive(source)
    before = _bytes(source)
    plan = _plan(source, target)
    assert plan["control_state"]["handoff_required"]
    assert not plan["control_state"]["continuation_allowed"]
    assert upgrade.apply_upgrade(plan)["valid"]
    assert _bytes(source) == before


def test_unknown_and_ambiguous_paths_never_guess_by_version():
    assert len(upgrade_path((BILIBILI_V1,), CURRENT)) == 3
    duplicate = replace(UPGRADE_EDGES[0], identity="alternate/v1")
    with pytest.raises(ValueError, match="ambiguous"):
        upgrade_path((BILIBILI_V1,), CURRENT, edges=(*UPGRADE_EDGES, duplicate))
    assert upgrade_path((BILIBILI_V1,), (UNIVERSAL_V2,), selected=("alternate/v1",),
                        edges=(*UPGRADE_EDGES, duplicate)) == (duplicate,)
    with pytest.raises(ValueError, match="unsupported contract"):
        upgrade_path((BILIBILI_V1,), ("universal-v99",))
    with pytest.raises(ValueError, match="no registered"):
        upgrade_path(CURRENT, (UNIVERSAL_V2,))


@pytest.mark.parametrize("combination", [(UNIVERSAL_V2,), (UNIVERSAL_V2, IMPORT_EXTENSION), CURRENT],
                         ids=["native", "preserved", "supplemented"])
@pytest.mark.parametrize("native", [False, True], ids=["historical", "native"])
@pytest.mark.parametrize("online", [False, True], ids=["stored", "online"])
def test_artifact_catalog_is_reachable_from_each_actual_universal_combination(tmp_path, combination, native, online):
    legacy, source, target = tmp_path / "legacy", tmp_path / "source", tmp_path / "target"
    if native:
        initialize_archive(source)
        if IMPORT_EXTENSION in combination:
            from bili_asr.services.preserved_body_import import install_preserved_body_extension
            install_preserved_body_extension(source)
        if SOURCE_SUPPLEMENT_POLICY in combination:
            from bili_asr.services.source_supplement import install_source_supplement_extension
            install_source_supplement_extension(source)
    else:
        frozen_archive(legacy)
        upgrade.apply_upgrade(_plan(legacy, source, combination))
    before = _bytes(source)
    destination_contracts = (*combination, ARTIFACT_STORAGE, *((ARTIFACT_ONLINE,) if online else ()))
    plan = _plan(source, target, destination_contracts)
    assert upgrade.apply_upgrade(plan)["valid"]
    assert upgrade.check_upgrade(target, expected_plan_id=plan["plan_id"])["valid"]
    assert _bytes(source) == before
    with closing(connect_database(target / "archive.db", readonly=True)) as connection:
        from bili_asr.storage.artifact_catalog import require_artifact_catalog
        from bili_asr.storage.artifact_online import require_artifact_online
        assert require_artifact_catalog(connection)
        assert require_artifact_online(connection) == online
    package = tmp_path / "catalog.zip"
    save_snapshot(target, package)
    assert check_snapshot(package)["valid"]
    restore_snapshot(package, tmp_path / "restored")


@pytest.mark.parametrize("native", [False, True])
def test_upgrade_retains_different_shadowed_source_bytes(tmp_path, native):
    source, product, target = tmp_path / "source", tmp_path / "artifacts", tmp_path / "target"
    if native:
        initialize_archive(source)
    else:
        frozen_archive(source)
    original = source / "documents" / "historical-unused.txt"
    selected = product / "documents" / "historical-unused.txt"
    original.parent.mkdir(exist_ok=True)
    selected.parent.mkdir(parents=True)
    original.write_bytes(b"historical version in archive root")
    selected.write_bytes(b"currently selected artifact root version")
    plan = _plan(source, target, artifact_root=product)
    assert len(plan["actions"]["preserve_shadowed"]) == 1
    alternate = plan["actions"]["preserve_shadowed"][0]
    upgrade.apply_upgrade(plan)
    assert (target / alternate["path"]).read_bytes() == original.read_bytes()
    assert (target / "documents/historical-unused.txt").read_bytes() == selected.read_bytes()
    (target / alternate["path"]).unlink()
    with pytest.raises(ValueError):
        upgrade.check_upgrade(target, expected_plan_id=plan["plan_id"])


@pytest.mark.parametrize("damage", ["source", "plan", "converter", "wal", "unknown-extension", "missing-file"])
def test_changed_or_invalid_inputs_fail_before_target_install(tmp_path, monkeypatch, damage):
    source, target = tmp_path / "source", tmp_path / "target"
    frozen_archive(source)
    plan = _plan(source, target)
    if damage == "source":
        with sqlite3.connect(source / "archive.db") as connection:
            connection.execute("UPDATE videos SET title='later observation'")
    elif damage == "plan":
        plan["actions"]["review_and_heads"] = "auto-approve"
        plan["plan_id"] = digest({key: value for key, value in plan.items() if key != "plan_id"})
    elif damage == "converter":
        monkeypatch.setattr(upgrade, "_build_digest", lambda: "0" * 64)
    elif damage == "wal":
        (source / "archive.db-wal").write_bytes(b"pending")
    elif damage == "unknown-extension":
        with sqlite3.connect(source / "archive.db") as connection:
            connection.execute("CREATE TABLE unknown_extension(x)")
    else:
        (source / "audio/BVpreserve.p0.m4a").unlink()
    before = _bytes(source)
    with pytest.raises((ValueError, OSError)):
        upgrade.apply_upgrade(plan)
    assert _bytes(source) == before
    assert not target.exists()


@pytest.mark.parametrize("point", ["copy", "convert", "verify", "rename"])
def test_interrupted_upgrade_never_publishes_half_target(tmp_path, monkeypatch, point):
    source, target = tmp_path / "source", tmp_path / "target"
    initialize_archive(source)
    plan = _plan(source, target)
    before = _bytes(source)
    def fail(*args, **kwargs):
        raise OSError("injected interruption")
    if point == "copy":
        monkeypatch.setattr(upgrade, "_copy_file", fail)
    elif point == "convert":
        monkeypatch.setattr(upgrade, "_convert", fail)
    elif point == "verify":
        monkeypatch.setattr(upgrade, "verify_preserved_tables", fail)
    else:
        monkeypatch.setattr(Path, "rename", fail)
    with pytest.raises(OSError, match="injected"):
        upgrade.apply_upgrade(plan)
    assert not target.exists()
    assert _bytes(source) == before


def test_space_overlaps_and_nonempty_targets_are_rejected(tmp_path, monkeypatch):
    source = tmp_path / "source"
    initialize_archive(source)
    with pytest.raises(ValueError, match="disjoint"):
        _plan(source, source / "nested")
    target = tmp_path / "target"
    plan = _plan(source, target)
    disk = upgrade.shutil.disk_usage(tmp_path)
    monkeypatch.setattr(upgrade.shutil, "disk_usage", lambda _: disk._replace(free=0))
    with pytest.raises(ValueError, match="insufficient space"):
        upgrade.apply_upgrade(plan)
    assert not target.exists()


def test_changed_receipt_or_target_is_not_reused_and_new_work_is_preserved(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    initialize_archive(source)
    plan = _plan(source, target)
    upgrade.apply_upgrade(plan)
    receipt_path = target / "documents/upgrades" / plan["plan_id"] / "receipt.json"
    original = receipt_path.read_bytes()
    receipt = json.loads(original)
    receipt["asr_calls"] = 100
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="receipt digest"):
        upgrade.apply_upgrade(plan)
    receipt_path.write_bytes(original)
    with sqlite3.connect(target / "archive.db") as connection:
        connection.execute("CREATE TABLE new_business_work(value)")
        connection.execute("INSERT INTO new_business_work VALUES ('keep me')")
    before = _bytes(target)
    with pytest.raises(ValueError, match="baseline changed"):
        upgrade.apply_upgrade(plan)
    assert _bytes(target) == before


def test_external_holds_remain_external_and_block_automatic_continuation(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    expected = frozen_archive(source)
    state = tmp_path / "operations.json"
    state.write_text(json.dumps({"retry_holds": {expected["ids"]["cancelled_job"]: {"reason": "manual hold"}}}), encoding="utf-8")
    before = state.read_bytes()
    plan = _plan(source, target, control_state=(state,))
    assert not plan["control_state"]["continuation_allowed"]
    assert upgrade.apply_upgrade(plan)["valid"]
    assert state.read_bytes() == before
    assert not any(path.name == state.name for path in target.rglob("*"))
    state.write_text('{"retry_holds":{"unknown-job":{}}}', encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be bound"):
        upgrade.apply_upgrade(plan)


@pytest.mark.parametrize("damage", ["remove", "change"])
def test_converter_cannot_drop_unreferenced_historical_file(tmp_path, monkeypatch, damage):
    source, target = tmp_path / "source", tmp_path / "target"
    initialize_archive(source)
    (source / "documents").mkdir()
    history = source / "documents/operator-history.json"
    history.write_text('{"historical":"keep exact bytes"}', encoding="utf-8")
    plan = _plan(source, target)
    original = upgrade._convert
    def damaged(edge, stage):
        original(edge, stage)
        path = stage / "documents/operator-history.json"
        if damage == "remove":
            path.unlink(missing_ok=True)
        else:
            path.write_text("changed", encoding="utf-8")
    monkeypatch.setattr(upgrade, "_convert", damaged)
    with pytest.raises(ValueError, match="protected source file"):
        upgrade.apply_upgrade(plan)
    assert not target.exists()
    assert history.read_text(encoding="utf-8") == '{"historical":"keep exact bytes"}'


def test_receipt_cannot_omit_planned_source_file_or_hide_added_file(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    initialize_archive(source)
    (source / "documents").mkdir()
    (source / "documents/retained.json").write_text("{}", encoding="utf-8")
    plan = _plan(source, target)
    upgrade.apply_upgrade(plan)
    receipt_path = target / "documents/upgrades" / plan["plan_id"] / "receipt.json"
    original = receipt_path.read_bytes()
    receipt = json.loads(original)
    receipt["files"] = [item for item in receipt["files"] if item["path"] != "documents/retained.json"]
    receipt["receipt_id"] = digest({key: value for key, value in receipt.items() if key != "receipt_id"})
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="protected source file"):
        upgrade.check_upgrade(target, expected_plan_id=plan["plan_id"])
    receipt_path.write_bytes(original)
    (target / "documents/new-business-file.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="file set changed"):
        upgrade.check_upgrade(target, expected_plan_id=plan["plan_id"])
