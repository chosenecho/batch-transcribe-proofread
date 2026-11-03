"""Interrupted journals and retained phases reject ambiguous ownership."""
import json
from pathlib import Path

import pytest

from bili_asr.canonical_json import digest
from bili_asr.services import archive_upgrade as upgrade
from bili_asr.services import upgrade_checkpoint as checkpoint
from tests.fixtures.frozen_migration_archive import frozen_archive


def signed(value):
    return {**value, "checkpoint_id": digest(value)}


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("corruption", ["plan", "digest", "format"])
def test_changed_checkpoint_identity_is_never_reused(tmp_path, pending, corruption):
    target = tmp_path / "target"
    owner = checkpoint.UpgradeCheckpoint(target, "a" * 64)
    value = json.loads(owner.path.read_bytes())
    if corruption == "plan":
        value["plan_id"] = "b" * 64
    elif corruption == "format":
        value["format"] = "unowned-format"
    else:
        value["checkpoint_id"] = "0" * 64
    if pending:
        owner.path.unlink()
        path = owner.path.with_name(owner.path.name + ".pending")
    else:
        path = owner.path
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="identity changed"):
        checkpoint.UpgradeCheckpoint(target, "a" * 64)
    assert not target.exists()


@pytest.mark.parametrize("phases", [None, [None] * 65])
def test_invalid_phase_ledger_is_rejected_before_processing(tmp_path, phases):
    owner = checkpoint.UpgradeCheckpoint(tmp_path / "target", "a" * 64)
    owner.path.write_text(json.dumps(signed({"format": "archive-upgrade-checkpoint/v1",
                                           "plan_id": "a" * 64, "phases": phases})))
    with pytest.raises(ValueError, match="invalid upgrade checkpoint phases"):
        checkpoint.UpgradeCheckpoint(tmp_path / "target", "a" * 64)


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("shape", [None, [], "invalid journal"])
def test_malformed_journal_reports_bounded_identity_error(tmp_path, pending, shape):
    owner = checkpoint.UpgradeCheckpoint(tmp_path / "target", "a" * 64)
    path = owner.path
    if pending:
        path.unlink()
        path = path.with_name(path.name + ".pending")
    path.write_text(json.dumps(shape))
    with pytest.raises(ValueError, match="identity changed"):
        checkpoint.UpgradeCheckpoint(tmp_path / "target", "a" * 64)


@pytest.mark.parametrize("phase", [None, {}, {"index": 0, "label": "source", "files": "invalid"}])
def test_malformed_phase_reports_bounded_error_before_file_access(tmp_path, phase):
    owner = checkpoint.UpgradeCheckpoint(tmp_path / "target", "a" * 64)
    owner.path.write_text(json.dumps(signed({"format": "archive-upgrade-checkpoint/v1",
                                           "plan_id": "a" * 64, "phases": [phase]})))
    with pytest.raises(ValueError, match="invalid completed upgrade phase"):
        checkpoint.UpgradeCheckpoint(tmp_path / "target", "a" * 64)


@pytest.mark.parametrize("pending", [False, True])
def test_oversized_journal_is_rejected_without_loading(tmp_path, pending):
    owner = checkpoint.UpgradeCheckpoint(tmp_path / "target", "a" * 64)
    path = owner.path
    if pending:
        path.unlink()
        path = path.with_name(path.name + ".pending")
    with path.open("wb") as stream:
        stream.truncate(16 * 1024**2 + 1)
    with pytest.raises(ValueError, match="checkpoint"):
        checkpoint.UpgradeCheckpoint(tmp_path / "target", "a" * 64)


def test_unowned_work_directory_is_preserved_and_rejected(tmp_path):
    root = tmp_path / (".target.upgrade-" + "a" * 64)
    root.mkdir()
    evidence = root / "operator-evidence"
    evidence.write_bytes(b"keep")
    with pytest.raises(ValueError, match="unowned"):
        checkpoint.UpgradeCheckpoint(tmp_path / "target", "a" * 64)
    assert evidence.read_bytes() == b"keep"


def test_stale_owned_partial_copy_restarts_without_accepting_old_bytes(tmp_path):
    source, output = tmp_path / "source", tmp_path / "output"
    source.write_bytes(b"correct original")
    partial = output.with_name(output.name + ".upgrade-partial")
    partial.write_bytes(b"interrupted bytes")
    size, sha256, _ = upgrade._hash_file(source)
    expected = {"size": size, "sha256": sha256}
    upgrade._copy_file(source, output, expected)
    upgrade._copy_file(source, output, expected)
    assert output.read_bytes() == source.read_bytes()
    assert not partial.exists()
    output.write_bytes(b"corrupt completed copy")
    with pytest.raises(ValueError, match="completed upgrade copy changed"):
        upgrade._copy_file(source, output, expected)


def test_copy_source_drift_does_not_publish_completed_file(tmp_path):
    source, output = tmp_path / "source", tmp_path / "output"
    source.write_bytes(b"original")
    size, sha256, _ = upgrade._hash_file(source)
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="source file changed"):
        upgrade._copy_file(source, output, {"size": size, "sha256": sha256})
    assert not output.exists()


def test_journal_recovery_rejects_changed_pending_phase(tmp_path, monkeypatch):
    owner = checkpoint.UpgradeCheckpoint(tmp_path / "target", "a" * 64)
    pending = owner.pending(0, rebuild=False)
    frozen_archive(pending)
    original = Path.rename

    def interrupt(path, destination):
        if path == pending:
            raise OSError("power loss")
        return original(path, destination)

    monkeypatch.setattr(Path, "rename", interrupt)
    with pytest.raises(OSError, match="power loss"):
        owner.seal(0, pending, "source")
    monkeypatch.setattr(Path, "rename", original)
    (pending / "documents" / "extra.txt").write_text("tampered")
    with pytest.raises(ValueError, match="uncommitted upgrade phase changed"):
        checkpoint.UpgradeCheckpoint(tmp_path / "target", "a" * 64)


def test_completed_phase_is_immutable_and_interrupted_conversion_is_reset(tmp_path):
    owner = checkpoint.UpgradeCheckpoint(tmp_path / "target", "a" * 64)
    pending = owner.pending(0, rebuild=False)
    frozen_archive(pending)
    complete = owner.seal(0, pending, "source")
    owner.pending(1, rebuild=False).mkdir()
    (owner.pending(1, rebuild=False) / "uncommitted").write_text("partial")
    next_stage = owner.pending(1, rebuild=True)
    assert not next_stage.exists()
    owner.copy_completed(complete, next_stage)
    assert checkpoint.inventory(next_stage) == checkpoint.inventory(complete)
    with pytest.raises(ValueError, match="not reset"):
        owner.copy_completed(complete, next_stage)
    with pytest.raises(ValueError, match="unexpected completed"):
        owner.seal(0, next_stage, "duplicate")


@pytest.mark.parametrize("identity", ["x", "g" * 64])
def test_audit_rejects_invalid_plan_identity_before_opening_target(tmp_path, identity):
    with pytest.raises(ValueError, match="invalid expected upgrade plan identity"):
        upgrade.audit_upgrade(tmp_path / "missing", expected_plan_id=identity)
