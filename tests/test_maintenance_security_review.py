"""Independent adversarial review of maintenance boundaries, using private fixtures."""
import json
import os
import sqlite3
from datetime import UTC, datetime

import pytest

from bili_asr.services import maintenance_backup as backup
from bili_asr.services import maintenance_chain as chain
from bili_asr.services import maintenance_package as package
from bili_asr.storage.maintenance_scope import scope
from tests.fixtures.frozen_migration_archive import frozen_archive


def test_retention_cannot_rehash_plan_to_delete_every_restore_point(tmp_path):
    source, repository = tmp_path / "source", tmp_path / "repository"
    frozen_archive(source)
    repository.mkdir()
    archive = repository / "full.zip"
    package.export_package(source, archive)
    original = archive.read_bytes()
    planned = chain.retention_plan(repository, keep_count=1, keep_days=0)
    planned.update(selected_restore_points=[], protected_dependencies=[], delete=planned["files"])
    planned["plan_id"] = package.digest({key: value for key, value in planned.items() if key != "plan_id"})
    with pytest.raises(ValueError):
        chain.apply_retention(planned)
    assert archive.read_bytes() == original


@pytest.mark.skipif(os.name == "nt", reason="unprivileged directory symlink fixture runs on Linux")
def test_backup_rejects_runs_symlink_before_creating_external_directory(tmp_path):
    source, repository, external = tmp_path / "source", tmp_path / "repository", tmp_path / "external"
    frozen_archive(source)
    repository.mkdir()
    external.mkdir()
    (repository / "runs").symlink_to(external, target_is_directory=True)
    configured = backup.policy(source, repository)
    with pytest.raises((ValueError, OSError)):
        backup.run_backup(configured, now=100)
    assert list(external.iterdir()) == []


def test_backup_rejects_tampered_success_receipt_before_reusing(tmp_path):
    source, repository = tmp_path / "source", tmp_path / "repository"
    frozen_archive(source)
    configured = backup.policy(source, repository, mode="full")
    completed = backup.run_backup(configured, now=100)
    assert completed["valid"], completed.get("error")
    receipt = repository / "runs" / configured["policy_id"] / (completed["run_id"] + ".json")
    document = json.loads(receipt.read_bytes())
    document["policy_id"] = "0" * 64
    receipt.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError):
        backup.run_backup(configured, now=100)


def test_public_backup_does_not_reuse_private_full_database_package(tmp_path):
    source, repository = tmp_path / "source", tmp_path / "repository"
    frozen_archive(source)
    repository.mkdir()
    configured = backup.policy(source, repository, mode="full", selection=scope(profile="publication"))
    identity = package.digest({"policy": configured["policy_id"], "timestamp": 100, "trigger": "manual", "event": None})
    output = repository / (datetime.fromtimestamp(100, UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + identity[:16] + ".zip")
    package.export_package(source, output)
    original = output.read_bytes()
    result = backup.run_backup(configured, now=100)
    assert not result["valid"], "public policy accepted a recoverable private database as a public backup"
    assert output.read_bytes() == original


def test_backup_policy_rejects_repository_inside_external_artifact_root(tmp_path):
    source, artifacts = tmp_path / "source", tmp_path / "artifacts"
    frozen_archive(source)
    artifacts.mkdir()
    with pytest.raises(ValueError):
        backup.policy(source, artifacts / "backups", artifact_root=artifacts)


@pytest.mark.parametrize("tampering", ["missing-digest", "foreign-policy"])
def test_event_backup_requires_authenticated_success_receipt(tmp_path, tampering):
    source, repository = tmp_path / "source", tmp_path / "repository"
    frozen_archive(source)
    configured = backup.policy(source, repository, mode="full")
    completed = backup.run_backup(configured, now=100)
    assert completed["valid"], completed.get("error")
    receipt = repository / "runs" / configured["policy_id"] / (completed["run_id"] + ".json")
    document = json.loads(receipt.read_bytes())
    document["event_id"] = "forged-release"
    if tampering == "missing-digest":
        document.pop("receipt_id")
    else:
        document["policy_id"] = "0" * 64
        document["receipt_id"] = package.digest({key: value for key, value in document.items() if key != "receipt_id"})
    receipt.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError):
        backup.run_backup(configured, trigger="event", event_id="forged-release", now=101)


def test_import_resume_does_not_accept_rehashed_plan_for_unrelated_converted_bytes(tmp_path):
    source, unrelated = tmp_path / "source", tmp_path / "unrelated"
    frozen_archive(source)
    frozen_archive(unrelated)
    with sqlite3.connect(unrelated / "archive.db") as connection:
        connection.execute("UPDATE videos SET updated_at=updated_at+1")
    incoming, impostor = tmp_path / "incoming.zip", tmp_path / "impostor.zip"
    package.export_package(source, incoming)
    package.export_package(unrelated, impostor)
    target = tmp_path / "target"
    planned = package.plan_import(incoming, target, resumable=True)
    forged_manifest = package.inspect_package(impostor)["manifest"]
    assert forged_manifest["content_id"] != planned["content_id"]
    planned.update(result_manifest=forged_manifest, content_id=forged_manifest["content_id"])
    planned["plan_id"] = package.digest({key: value for key, value in planned.items() if key != "plan_id"})
    saved = tmp_path / "forged-plan.json"
    package.write_plan(planned, saved)
    converted = tmp_path / (".target.maintenance-" + planned["plan_id"]) / "converted"
    converted.mkdir(parents=True)
    package._extract(impostor, converted)
    original = (source / "archive.db").read_bytes()
    with pytest.raises(ValueError):
        package.apply_import(saved)
    assert not target.exists()
    assert (source / "archive.db").read_bytes() == original
