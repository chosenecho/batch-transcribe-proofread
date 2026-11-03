"""Malformed plans and containers must fail before modifying archive authority."""
import copy
import hashlib
import json
import sqlite3
import zipfile
from datetime import UTC, datetime

import pytest

from bili_asr.services import maintenance_backup as backup
from bili_asr.services import maintenance_chain as chain
from bili_asr.services import maintenance_import as imports
from bili_asr.services import maintenance_package as package
from bili_asr.services import maintenance_publication as public
from bili_asr.storage import maintenance_scope as scopes
from tests.fixtures.frozen_migration_archive import frozen_archive


@pytest.mark.parametrize("arguments", [
    {"profile": "unknown"}, {"datasets": ["unknown"]}, {"artifact_types": ["unknown"]},
    {"directories": ["../outside"]}, {"since": -1}, {"until": True},
    {"since": 2, "until": 1}, {"statuses": [""]}, {"part_ids": [0]},
    {"bvids": [""]}, {"profile": "publication", "part_ids": [1]},
])
def test_scope_rejects_invalid_or_permission_changing_selectors(arguments):
    with pytest.raises(ValueError):
        scopes.scope(**arguments)


def test_scope_profiles_and_inclusive_predicates_preserve_required_dependencies():
    assert scopes.scope(profile="workflow-transcripts")["datasets"] == ["transcripts", "workflow"]
    selected, excluded = scopes.filter_artifacts({"subtitles/a": 1, "audio/a": 2}, set(), scopes.scope(profile="database-subtitles"))
    assert selected == {"subtitles/a": 1} and excluded == ["audio/a"]
    expression, values = scopes.predicate(["created_at", "status"], scopes.scope(since=10, until=20, statuses=["done"]))
    assert expression == '"created_at">=? AND "created_at"<=? AND "status" IN (?)'
    assert values == (10, 20, "done")
    with pytest.raises(ValueError, match="referenced dependency"):
        scopes.filter_artifacts({"audio/a": 1}, {"audio/a"}, scopes.scope(profile="database-only"))


@pytest.mark.parametrize("arguments", [
    {"interval_seconds": 0}, {"alert_after": False}, {"keep_days": -1},
    {"selection": {"profile": "unknown"}}, {"mode": "unknown"},
    {"compression": "encrypted"}, {"mode": "full", "compression": "deflate"},
    {"selection": scopes.scope(profile="publication"), "mode": "incremental"},
])
def test_backup_policy_rejects_unsupported_operational_parameters(tmp_path, arguments):
    with pytest.raises((ValueError, TypeError)):
        backup.policy(tmp_path / "source", tmp_path / "repository", **arguments)


def test_backup_rejects_overlapping_repository_and_invalid_run_control(tmp_path):
    with pytest.raises(ValueError, match="disjoint"):
        backup.policy(tmp_path / "source", tmp_path / "source" / "backup")
    document = backup.policy(tmp_path / "source", tmp_path / "repository")
    for arguments in ({"trigger": "unknown"}, {"trigger": "event"}, {"trigger": "event", "event_id": "x" * 257}, {"now": -1}):
        with pytest.raises(ValueError):
            backup.run_backup(document, **arguments)
    changed = {**document, "extra": True}
    changed["policy_id"] = package.digest({k: v for k, v in changed.items() if k != "policy_id"})
    with pytest.raises(ValueError, match="unsupported"):
        backup.run_backup(changed)
    with pytest.raises(ValueError, match="identity"):
        backup.backup_status(tmp_path / "repository", "../escape")


def test_backup_receipt_reuse_and_differential_retention_protect_baseline(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    configured = backup.policy(source, tmp_path / "repository", mode="differential", keep_count=1, keep_days=0)
    first = backup.run_backup(configured, now=100)
    assert first["valid"]
    assert backup.run_backup(configured, now=100)["reused"]
    second = backup.run_backup(configured, now=110)
    third = backup.run_backup(configured, now=120)
    assert second["valid"] and third["valid"]
    assert len(third["verification"]["chain"]) == 2
    assert first["snapshot"] not in third["retention"]["deleted"]
    status = backup.backup_status(tmp_path / "repository", configured["policy_id"])
    assert len(status["runs"]) == 3
    receipt_path = tmp_path / "repository" / "runs" / configured["policy_id"] / (first["run_id"] + ".json")
    value = json.loads(receipt_path.read_bytes())
    value["notification"] = "tampered"
    receipt_path.write_bytes(package.canonical(value))
    with pytest.raises(ValueError, match="changed"):
        backup.backup_status(tmp_path / "repository", configured["policy_id"])


def test_checkpoint_rejects_pending_rollback_and_invalid_timeout(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    with pytest.raises(ValueError, match="timeout"):
        backup.checkpoint_archive(source, timeout_ms=0)
    (source / "archive.db-journal").write_bytes(b"pending recovery")
    with pytest.raises(ValueError, match="rollback journal"):
        backup.checkpoint_archive(source)


def test_corrupt_repository_zip_records_repeated_failure_alerts(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "broken.zip").write_bytes(b"not a zip")
    document = backup.policy(source, repository, mode="full", alert_after=2)
    first = backup.run_backup(document, now=100)
    second = backup.run_backup(document, now=110)
    assert first["status"] == second["status"] == "failed"
    assert first["notification"] == "backup_failure"
    assert second["notification"] == "backup_failure_alert"
    assert "zip" in second["error"].lower()
    assert all(item["status"] == "failed" and item["receipt_id"] for item in backup.backup_status(repository, document["policy_id"])["runs"])


def test_backup_profile_proof_cannot_reuse_scoped_private_container_as_full():
    document = {"scope": None}
    with pytest.raises(ValueError, match="full private history"):
        backup._verify_profile(document, {"selection": {"mode": "scope"}})
    specification = scopes.scope(datasets=["metadata"])
    with pytest.raises(ValueError, match="selectors"):
        backup._verify_profile({"scope": specification}, {"selection": {"scope": scopes.scope(datasets=["tags"])}})


def test_interrupted_published_backup_reuses_exact_source_bound_container(tmp_path, monkeypatch):
    source = tmp_path / "source"
    frozen_archive(source)
    document = backup.policy(source, tmp_path / "repository")
    real_verify = backup.verify_chain

    def interrupt(path):
        raise ValueError("simulated verification process interruption")

    monkeypatch.setattr(backup, "verify_chain", interrupt)
    first = backup.run_backup(document, now=100)
    assert first["status"] == "failed"
    assert (tmp_path / "repository" / first["snapshot"]).is_file()
    monkeypatch.setattr(backup, "verify_chain", real_verify)
    assert backup.run_backup(document, now=100)["status"] == "success"
    # An orphan from another frozen source must never become this run's receipt.
    document2 = backup.policy(source, tmp_path / "repository2")
    monkeypatch.setattr(backup, "verify_chain", interrupt)
    assert backup.run_backup(document2, now=100)["status"] == "failed"
    monkeypatch.setattr(backup, "verify_chain", real_verify)
    with sqlite3.connect(source / "archive.db") as connection:
        connection.execute("UPDATE videos SET updated_at=updated_at+1")
    result = backup.run_backup(document2, now=100)
    assert result["status"] == "failed" and "fingerprint" in result["error"]


def test_checkpoint_requires_readers_to_release_their_wal_snapshot(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    writer = sqlite3.connect(source / "archive.db")
    reader = sqlite3.connect(source / "archive.db")
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM videos").fetchall()
        writer.execute("UPDATE videos SET updated_at=updated_at+1")
        writer.commit()
        with pytest.raises(ValueError, match="checkpoint incomplete"):
            backup.checkpoint_archive(source, timeout_ms=10)
    finally:
        reader.rollback()
        reader.close()
        writer.close()


def test_safe_event_id_mapping_and_natural_key_conflict_are_explicit(tmp_path):
    from bili_asr.storage.maintenance_merge import merge_database
    source, target = tmp_path / "source", tmp_path / "target"
    frozen_archive(source)
    frozen_archive(target)
    with sqlite3.connect(source / "archive.db") as connection:
        # Build an independently frozen history with a colliding event ID,
        # then restore its exact append-only contract before merge validation.
        triggers = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='publication_events'").fetchall()
        for name, _ in triggers:
            connection.execute('DROP TRIGGER "' + name + '"')
        connection.execute("UPDATE publication_events SET actor=actor || '-other'")
        for _, sql in triggers:
            connection.execute(sql)
    report = merge_database(source / "archive.db", target / "archive.db")
    assert report["id_mappings"]["publication_events"]
    with sqlite3.connect(target / "archive.db") as connection:
        assert connection.execute("SELECT count(*) FROM publication_events WHERE actor LIKE '%-other'").fetchone()[0] > 0
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    with sqlite3.connect(source / "archive.db") as connection:
        connection.execute("UPDATE videos SET updated_at=updated_at+1")
    before = (target / "archive.db").read_bytes()
    with pytest.raises(ValueError, match="identity conflict"):
        merge_database(source / "archive.db", target / "archive.db")
    assert (target / "archive.db").read_bytes() == before


def public_zip(path, *, change=None, payload=b"{}", extra=None):
    entry = {"path": "catalog.json", "size": 2, "sha256": hashlib.sha256(b"{}").hexdigest()}
    value = {"format": "publication-maintenance-backup/v1", "profile": "publication", "database_recoverable": False,
             "source_fingerprint": "0" * 64, "files": [entry], "container": {"compression": "stored", "encryption": None}}
    if change:
        change(value)
    manifest = {**value, "content_id": package.digest(value), "created_at": datetime.now(UTC).isoformat()}
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(entry["path"], payload)
        bundle.writestr(public.NAME, package.canonical(manifest))
        if extra:
            bundle.writestr(extra, b"unlisted")
    return path


def test_public_container_rejects_invalid_identity_private_paths_and_payload_drift(tmp_path):
    for index, (change, payload, extra, message) in enumerate([
        (lambda x: x.update(profile="private"), b"{}", None, "identity"),
        (lambda x: x.update(source_fingerprint="invalid"), b"{}", None, "source identity"),
        (lambda x: x.update(container={"compression": "stored", "encryption": "aes"}), b"{}", None, "encryption"),
        (None, b"tampered", None, "payload changed"),
        (None, b"{}", "extra.json", "inventory mismatch"),
        (lambda x: x["files"][0].update(path="archive.db"), b"{}", None, "projection path"),
    ]):
        path = public_zip(tmp_path / f"public-{index}.zip", change=change, payload=payload, extra=extra)
        with pytest.raises(ValueError, match=message):
            public.verify_public_backup(path)
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="outside its source"):
        public.export_public_profile(source, source / "public")
    with pytest.raises(ValueError, match="new file"):
        public.save_public_backup(source, tmp_path / "missing" / "public.zip")


def test_import_rejects_invalid_plan_strategy_and_source_drift(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    incoming = tmp_path / "incoming.zip"
    package.export_package(source, incoming)
    target = tmp_path / "target"
    with pytest.raises(ValueError, match="strategy"):
        imports.plan_import(incoming, target, strategy="unknown")
    with pytest.raises(ValueError, match="inside the target"):
        imports.plan_import(incoming, tmp_path)
    with pytest.raises(ValueError, match="existing archive"):
        imports.plan_import(incoming, target, strategy="replace")
    with pytest.raises(ValueError, match="empty target"):
        imports.plan_import(incoming, source)
    plan = imports.plan_import(incoming, target)
    forged = copy.deepcopy(plan)
    forged["content_id"] = "0" * 64
    forged["plan_id"] = package.digest({k: v for k, v in forged.items() if k != "plan_id"})
    with pytest.raises(ValueError, match="identity mismatch"):
        imports._validate(forged)
    with pytest.raises(ValueError, match="invalid resumable"):
        imports._validate({})
    saved = tmp_path / "plan.json"
    package.write_plan(plan, saved)
    incoming.write_bytes(incoming.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="package changed"):
        imports.apply_import(saved)
    assert not target.exists()


def test_import_checkpoint_mismatch_and_converted_hash_drift_preserve_empty_target(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    incoming = tmp_path / "incoming.zip"
    package.export_package(source, incoming)
    target = tmp_path / "target"
    plan = imports.plan_import(incoming, target)
    saved = tmp_path / "plan.json"
    package.write_plan(plan, saved)
    work = tmp_path / (".target.maintenance-" + plan["plan_id"])
    work.mkdir()
    imports._save(work / "checkpoint.json", {"format": "maintenance-import-checkpoint/v1", "plan_id": "foreign"})
    with pytest.raises(ValueError, match="another plan"):
        imports.apply_import(saved)
    (work / "checkpoint.json").unlink()
    converted = work / "converted"
    converted.mkdir()
    package._extract(incoming, converted)
    (converted / "archive.db").write_bytes(b"unrelated bytes")
    with pytest.raises(ValueError, match="conversion changed"):
        imports.apply_import(saved)
    assert not target.exists()


def test_stage_copy_rejects_source_drift_and_cleans_own_partial(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"source")
    expected = package._record("file", source)
    partial = tmp_path / "output.maintenance-partial"
    partial.write_bytes(b"old partial")
    imports._copy(source, tmp_path / "output", expected)
    assert (tmp_path / "output").read_bytes() == b"source"
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="source changed"):
        imports._copy(source, tmp_path / "second", expected)
    pending = tmp_path / "checkpoint.pending"
    pending.write_bytes(b"old")
    imports._save(tmp_path / "checkpoint", {"phase": "converted"})
    assert json.loads((tmp_path / "checkpoint").read_bytes())["phase"] == "converted"


def test_chain_rejects_invalid_paths_modes_and_modified_retention_inventory(tmp_path):
    for name in ("../base.zip", ".hidden.zip", "base.tar"):
        with pytest.raises(ValueError, match="plain ZIP"):
            chain._name(name)
    source = tmp_path / "source"
    frozen_archive(source)
    repository = tmp_path / "repository"
    repository.mkdir()
    base = repository / "base.zip"
    package.export_package(source, base)
    with pytest.raises(ValueError, match="new and share"):
        chain.save_delta(source, base, base)
    with pytest.raises(ValueError, match="compression"):
        chain.save_delta(source, repository / "delta.zip", base, compression="encrypted")
    with pytest.raises(ValueError, match="already exists"):
        chain.materialized_package(base, base)
    with pytest.raises(ValueError, match="at least one"):
        chain.retention_plan(repository, keep_count=0)
    plan = chain.retention_plan(repository)
    changed = copy.deepcopy(plan)
    changed["plan_id"] = "0" * 64
    with pytest.raises(ValueError, match="identity changed"):
        chain.apply_retention(changed)
    package.export_package(source, repository / "new.zip")
    with pytest.raises(ValueError, match="repository changed"):
        chain.apply_retention(plan)


def test_differential_manifest_rejects_rehashed_invalid_dependency_and_inventory(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    base, delta = tmp_path / "base.zip", tmp_path / "delta.zip"
    package.export_package(source, base)
    chain.save_delta(source, delta, base)
    with zipfile.ZipFile(delta) as bundle:
        original = json.loads(bundle.read(chain.NAME))
        members = {entry.filename: bundle.read(entry) for entry in bundle.infolist() if entry.filename != chain.NAME}
    mutations = [
        lambda d: d.update(format="unknown"),
        lambda d: d.update(base={"name": "base.zip"}),
        lambda d: d["base"].update(sha256="invalid"),
        lambda d: d.update(changed=["unknown"]),
        lambda d: d.update(deleted=["x", "x"]),
        lambda d: d.update(deleted=["archive.db"]),
        lambda d: d.update(container={"compression": "stored", "encryption": "aes"}),
    ]
    for index, mutate in enumerate(mutations):
        document = copy.deepcopy(original)
        mutate(document)
        document["chain_id"] = package.digest({k: v for k, v in document.items() if k not in {"chain_id", "created_at"}})
        damaged = tmp_path / f"damaged-{index}.zip"
        with zipfile.ZipFile(damaged, "w") as bundle:
            for key, value in members.items():
                bundle.writestr(key, value)
            bundle.writestr(chain.NAME, package.canonical(document))
        with pytest.raises(ValueError):
            chain._manifest(damaged)


def test_selection_refuses_absent_parts_and_unknown_payload_identity(tmp_path):
    from bili_asr.storage.maintenance_selection import select_database
    source = tmp_path / "source"
    frozen_archive(source)
    database = source / "archive.db"
    with pytest.raises(ValueError, match="fresh database"):
        select_database(database, database, part_ids=[1], bvids=[])
    with pytest.raises(ValueError, match="no archived parts"):
        select_database(database, tmp_path / "absent.db", part_ids=[], bvids=["BVdoesnotexist"])
    with pytest.raises(ValueError, match="at least one part"):
        select_database(database, tmp_path / "empty.db", part_ids=[], bvids=[])
    with pytest.raises(ValueError, match="part does not exist"):
        select_database(database, tmp_path / "part.db", part_ids=[999999], bvids=[])
    with sqlite3.connect(database) as connection:
        bvid = connection.execute("SELECT bvid FROM video_parts WHERE video_part_id=1").fetchone()[0]
    assert select_database(database, tmp_path / "bvid.db", part_ids=[], bvids=[bvid])["requested_part_ids"]
    with sqlite3.connect(database) as connection:
        key, raw = connection.execute("SELECT job_id,payload_json FROM workflow_jobs WHERE video_part_id=1 LIMIT 1").fetchone()
        payload = json.loads(raw)
        payload["unknown_authority_id"] = 100
        connection.execute("UPDATE workflow_jobs SET payload_json=? WHERE job_id=?", (json.dumps(payload), key))
    with pytest.raises(ValueError, match="payload identity"):
        select_database(database, tmp_path / "payload.db", part_ids=[1], bvids=[])


def test_registered_schemas_cannot_merge_and_untyped_provider_lists_are_not_guessed(tmp_path):
    from bili_asr.storage.archive_contracts import bootstrap_contract
    from bili_asr.storage.maintenance_merge import merge_database
    source, target = tmp_path / "source", tmp_path / "target"
    frozen_archive(source)
    target.mkdir()
    with sqlite3.connect(target / "archive.db") as connection:
        bootstrap_contract(connection)
    with pytest.raises(ValueError, match="identical registered database schemas"):
        merge_database(source / "archive.db", target / "archive.db")
    other = tmp_path / "other"
    frozen_archive(other)
    with sqlite3.connect(source / "archive.db") as connection:
        connection.execute("UPDATE workflow_attempts SET result_json='[]' WHERE result_json IS NOT NULL")
    before = (other / "archive.db").read_bytes()
    with pytest.raises(ValueError, match="identity conflict"):
        merge_database(source / "archive.db", other / "archive.db")
    assert (other / "archive.db").read_bytes() == before


def test_merge_preserves_nested_publication_identity_envelopes(tmp_path):
    from bili_asr.storage.maintenance_merge import merge_database
    source, target = tmp_path / "source", tmp_path / "target"
    frozen_archive(source)
    frozen_archive(target)
    for root in (source, target):
        with sqlite3.connect(root / "archive.db") as connection:
            identity = connection.execute("SELECT publication_id FROM workflow_publications LIMIT 1").fetchone()[0]
            key, raw = connection.execute("SELECT job_id,payload_json FROM workflow_jobs LIMIT 1").fetchone()
            value = json.loads(raw)
            value["preserve_publication"] = {"publication_id": identity}
            connection.execute("UPDATE workflow_jobs SET payload_json=? WHERE job_id=?", (json.dumps(value), key))
    result = merge_database(source / "archive.db", target / "archive.db")
    assert result["id_mappings"] == {}
    with sqlite3.connect(target / "archive.db") as connection:
        actual = json.loads(connection.execute("SELECT payload_json FROM workflow_jobs WHERE job_id=?", (key,)).fetchone()[0])
    assert actual["preserve_publication"] == {"publication_id": identity}


def test_export_scope_contract_rejects_public_private_confusion_and_overlap(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    with pytest.raises(ValueError, match="disjoint"):
        package.export_package(source, tmp_path / "overlap.zip", artifact_root=source / "audio")
    with pytest.raises(ValueError, match="not a recoverable"):
        package.export_package(source, tmp_path / "public.zip", scope=scopes.scope(profile="publication"))
    document_path = tmp_path / "not-object.json"
    document_path.write_bytes(b"[]")
    with pytest.raises(ValueError, match="must be an object"):
        package._json(document_path)
    scoped = tmp_path / "metadata.zip"
    package.export_package(source, scoped, scope=scopes.scope(datasets=["metadata"], profile="database-only"))
    manifest = package.inspect_package(scoped)["manifest"]
    for mutate in (lambda d: d["selection"].update(unregistered=True),
                   lambda d: d["selection"].update(explicit_row_counts={"videos": -1})):
        value = copy.deepcopy(manifest)
        mutate(value)
        value["content_id"] = package.digest({k: v for k, v in value.items() if k not in {"content_id", "created_at", "producer_version"}})
        with pytest.raises(ValueError):
            package._validate_manifest(value)


def test_snapshot_check_dispatches_valid_differential_chain(tmp_path):
    from bili_asr.services.archive_snapshot import check_snapshot
    source = tmp_path / "source"
    frozen_archive(source)
    base, delta = tmp_path / "base.zip", tmp_path / "delta.zip"
    package.export_package(source, base)
    chain.save_delta(source, delta, base)
    report = check_snapshot(delta)
    assert report["valid"] and len(report["chain"]) == 2
