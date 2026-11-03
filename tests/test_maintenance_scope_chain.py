"""Scoped packages and differential chains expose, preserve and verify closure."""
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from bili_asr.services import maintenance_chain as chain
from bili_asr.services import maintenance_package as package
from bili_asr.storage.maintenance_scope import scope
from bili_asr.storage.upgrade_evidence import readonly
from tests.fixtures.frozen_migration_archive import frozen_archive


def test_metadata_tags_scope_excludes_workflow_and_retains_foreign_keys(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    output = tmp_path / "metadata.zip"
    package.export_package(source, output, part_ids=[1], scope=scope(datasets=["metadata", "tags"], profile="database-only"))
    manifest = package.inspect_package(output)["manifest"]
    assert manifest["format"] == "archive-maintenance-package/v2"
    assert [item["path"] for item in manifest["files"]] == ["archive.db"]
    assert manifest["selection"]["explicit_row_counts"]["video_parts"] == 1
    rows = {item["name"]: item["row_count"] for item in manifest["tables"]}
    assert rows["workflow_jobs"] == rows["transcripts"] == 0
    assert rows["video_parts"] == 1
    assert package.verify_package(output)["valid"]


def test_status_scope_dependencies_may_include_other_status_and_missing_artifacts_fail(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    output = tmp_path / "workflow.zip"
    package.export_package(source, output, part_ids=[1], scope=scope(datasets=["workflow"], statuses=["cancelled"]))
    report = package.inspect_package(output)["manifest"]
    assert report["selection"]["scope"]["statuses"] == ["cancelled"]
    assert package.verify_package(output)["valid"]
    with pytest.raises(ValueError, match="excludes referenced dependency"):
        package.export_package(source, tmp_path / "bad.zip", part_ids=[1],
                               scope=scope(datasets=["publication"], profile="database-only"))


def test_differential_roundtrip_deletion_chain_damage_and_safe_retention(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    repository = tmp_path / "repository"
    repository.mkdir()
    base = repository / "base.zip"
    package.export_package(source, base)
    detached = source / "documents" / "unused.json"
    detached.write_bytes(b"first bytes")
    delta = repository / "delta.zip"
    saved = chain.save_delta(source, delta, base, compression="deflate")
    assert saved["changed_files"] == 1
    assert chain.verify_chain(delta)["valid"]
    detached.unlink()
    third = repository / "third.zip"
    assert chain.save_delta(source, third, delta)["deleted_files"] == 1
    assert len(chain.verify_chain(third)["chain"]) == 3
    full = tmp_path / "materialized.zip"
    chain.materialized_package(third, full)
    assert package.verify_package(full)["valid"]
    assert package.inspect_package(full)["manifest"]["files"] == package.inspect_package(base)["manifest"]["files"]
    future = datetime.now(UTC) + timedelta(days=365)
    planned = chain.retention_plan(repository, keep_count=1, keep_days=0, now=future)
    assert planned["protected_dependencies"] == ["base.zip", "delta.zip", "third.zip"]
    assert planned["delete"] == []
    assert chain.apply_retention(planned)["deleted"] == []
    hidden = tmp_path / "hidden.zip"
    base.rename(hidden)
    with pytest.raises(FileNotFoundError):
        chain.verify_chain(third)
    hidden.rename(base)
    original = base.read_bytes()
    base.write_bytes(original + b"tamper")
    with pytest.raises(ValueError, match="baseline container changed"):
        chain.verify_chain(delta)


def test_retention_rejects_new_restore_point_and_cannot_delete_protected_base(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    repository = tmp_path / "repository"
    repository.mkdir()
    base = repository / "base.zip"
    package.export_package(source, base)
    planned = chain.retention_plan(repository)
    package.export_package(source, repository / "new.zip")
    with pytest.raises(ValueError, match="repository changed"):
        chain.apply_retention(planned)


def test_time_scope_resolves_actual_zero_rows_and_keeps_schema(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    output = tmp_path / "future.zip"
    package.export_package(source, output, scope=scope(datasets=["metadata"], since=99999999999))
    assert package.verify_package(output)["valid"]
    assert package.inspect_package(output)["manifest"]["selection"]["resolved_part_ids"] == []


def test_original_full_snapshot_is_a_supported_differential_baseline(tmp_path):
    from bili_asr.services.archive_snapshot import check_snapshot, save_snapshot
    source = tmp_path / "source"
    frozen_archive(source)
    base = tmp_path / "legacy-full.zip"
    save_snapshot(source, base)
    assert check_snapshot(base)["valid"]
    extra = source / "documents" / "fresh.json"
    extra.write_bytes(b"{}")
    delta = tmp_path / "incremental.zip"
    chain.save_delta(source, delta, base)
    assert len(chain.verify_chain(delta)["chain"]) == 2


def test_import_uses_the_same_scope_and_profile_as_export(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    full = tmp_path / "full.zip"
    package.export_package(source, full)
    target = tmp_path / "target"
    specification = scope(datasets=["metadata", "tags"], profile="database-only")
    plan = package.plan_import(full, target, scope=specification)
    assert not target.exists()
    saved = tmp_path / "import-plan.json"
    package.write_plan(plan, saved)
    package.apply_import(saved)
    assert package.check_import(target, plan_id=plan["plan_id"])["valid"]
    with closing(readonly(target / "archive.db")) as connection:
        assert connection.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM video_parts").fetchone()[0] == 3


def test_part_and_bvid_scope_is_shared_by_import_and_backup(tmp_path):
    from bili_asr.services.maintenance_backup import policy, run_backup
    source = tmp_path / "source"
    frozen_archive(source)
    full = tmp_path / "full.zip"
    package.export_package(source, full)
    selection = scope(datasets=["metadata"], part_ids=[1], profile="database-only")
    plan = package.plan_import(full, tmp_path / "target", scope=selection)
    assert plan["result_manifest"]["selection"]["resolved_part_ids"] == [1]
    configured = policy(source, tmp_path / "backups", selection=selection)
    result = run_backup(configured, now=100)
    assert result["valid"]
    manifest = package.inspect_package(tmp_path / "backups" / result["snapshot"])["manifest"]
    assert manifest["selection"]["resolved_part_ids"] == [1]
