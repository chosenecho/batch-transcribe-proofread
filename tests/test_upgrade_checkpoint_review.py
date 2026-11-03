"""Crash publication and hard-link aliases never damage retained evidence."""
import os
from pathlib import Path

import pytest

from bili_asr.services import archive_upgrade as upgrade
from bili_asr.services import upgrade_checkpoint as checkpoint
from tests.test_archive_upgrade_resume import planned


def test_first_checkpoint_publication_interrupt_is_resumable(tmp_path, monkeypatch):
    source, target, before, plan = planned(tmp_path)
    replace = Path.replace
    seen = False

    def interrupt(path, destination):
        nonlocal seen
        if path.name == "checkpoint.json.pending" and not seen:
            seen = True
            raise OSError("interrupted checkpoint journal publication")
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", interrupt)
    with pytest.raises(OSError, match="publication"):
        upgrade.apply_upgrade(plan)
    monkeypatch.setattr(Path, "replace", replace)
    assert not target.exists()
    assert (source / "archive.db").read_bytes() == before
    assert upgrade.apply_upgrade(plan)["valid"]


def test_save_pending_hardlink_never_changes_external_file(tmp_path):
    external = tmp_path / "external-evidence.txt"
    external.write_bytes(b"original evidence")
    destination = tmp_path / "checkpoint.json"
    os.link(external, destination.with_name(destination.name + ".pending"))
    try:
        checkpoint.save(destination, {"valid": "replacement"})
    except (ValueError, OSError):
        pass
    assert external.read_bytes() == b"original evidence"


def test_initial_copy_partial_hardlink_never_changes_external_file(tmp_path):
    original = tmp_path / "original"
    original.write_bytes(b"archive-source")
    external = tmp_path / "external"
    external.write_bytes(b"old-external-evidence")
    destination = tmp_path / "destination"
    os.link(external, destination.with_name(destination.name + ".upgrade-partial"))
    size, sha256, _ = upgrade._hash_file(original)
    try:
        upgrade._copy_file(original, destination, {"size": size, "sha256": sha256})
    except (ValueError, OSError):
        pass
    assert external.read_bytes() == b"old-external-evidence"


def test_completed_checkpoint_hardlink_refuses_resume(tmp_path, monkeypatch):
    source, target, before, plan = planned(tmp_path)
    upgrade.apply_upgrade(plan)
    work = target.parent / ("." + target.name + ".upgrade-" + plan["plan_id"])
    os.link(work / "phase-0" / "archive.db", tmp_path / "external-phase-alias.db")
    shutil = __import__("shutil")
    shutil.rmtree(target)
    with pytest.raises((ValueError, OSError)):
        upgrade.apply_upgrade(plan)
    assert not target.exists()
    assert (source / "archive.db").read_bytes() == before


def test_audit_external_control_drift_never_allows_baseline_reuse(tmp_path):
    source, target, _before, plan = planned(tmp_path)
    control = tmp_path / "operations.json"
    control.write_text('{"retry_holds": {}}', encoding="utf-8")
    # Re-plan with a bound external control record, then install the target.
    plan = upgrade.plan_upgrade(source, target,
        target_contracts=tuple(plan["target_contracts"]),
        control_state=(control,))
    upgrade.apply_upgrade(plan)
    control.write_text('{"retry_holds": {}, "operator_note": "changed"}', encoding="utf-8")
    report = upgrade.audit_upgrade(target, expected_plan_id=plan["plan_id"])
    assert report["external_control_state"][0]["changed"] is True
    assert report["baseline_reuse_allowed"] is False
