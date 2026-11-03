"""Interrupted real upgrades retain validated phases and never install halfway."""
from pathlib import Path

import pytest

from bili_asr.contracts.registry import IMPORT_EXTENSION, SOURCE_SUPPLEMENT_POLICY, UNIVERSAL_V2
from bili_asr.services import archive_upgrade as upgrade
from tests.fixtures.frozen_migration_archive import frozen_archive


def planned(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    frozen_archive(source)
    before = (source / "archive.db").read_bytes()
    plan = upgrade.plan_upgrade(source, target,
        target_contracts=(UNIVERSAL_V2, IMPORT_EXTENSION, SOURCE_SUPPLEMENT_POLICY),
        no_external_control_state=True)
    return source, target, before, plan


def test_multistage_conversion_resumes_completed_legacy_phase(tmp_path, monkeypatch):
    source, target, before, plan = planned(tmp_path)
    original = upgrade._convert
    seen = []

    def interrupt(edge, stage):
        seen.append(edge.identity)
        if len(seen) == 2:
            raise OSError("power loss in second conversion")
        original(edge, stage)

    monkeypatch.setattr(upgrade, "_convert", interrupt)
    with pytest.raises(OSError, match="power loss"):
        upgrade.apply_upgrade(plan)
    assert not target.exists()
    assert (source / "archive.db").read_bytes() == before
    work = target.parent / ("." + target.name + ".upgrade-" + plan["plan_id"])
    assert (work / "phase-0" / "archive.db").is_file()
    assert (work / "phase-1" / "archive.db").is_file()
    monkeypatch.setattr(upgrade, "_convert", original)
    result = upgrade.apply_upgrade(plan)
    assert result["valid"] and result["resumed_phases"] == 2
    assert upgrade.check_upgrade(target, expected_plan_id=plan["plan_id"])["valid"]
    assert upgrade.apply_upgrade(plan)["reused"]
    assert (source / "archive.db").read_bytes() == before


def test_completed_phase_tamper_refuses_resume_without_touching_target(tmp_path, monkeypatch):
    source, target, before, plan = planned(tmp_path)
    original = upgrade._convert
    monkeypatch.setattr(upgrade, "_convert", lambda *_: (_ for _ in ()).throw(OSError("interrupt")))
    with pytest.raises(OSError):
        upgrade.apply_upgrade(plan)
    work = target.parent / ("." + target.name + ".upgrade-" + plan["plan_id"])
    with (work / "phase-0" / "archive.db").open("ab") as stream:
        stream.write(b"modified")
    monkeypatch.setattr(upgrade, "_convert", original)
    with pytest.raises(ValueError, match="phase changed"):
        upgrade.apply_upgrade(plan)
    assert not target.exists()
    assert (source / "archive.db").read_bytes() == before


def test_checkpoint_published_before_phase_rename_recovers_exact_pending_bytes(tmp_path, monkeypatch):
    _, target, _, plan = planned(tmp_path)
    rename = Path.rename

    def interrupt(path, destination):
        if path.name == "pending-0":
            raise OSError("power loss after checkpoint publication")
        return rename(path, destination)

    monkeypatch.setattr(Path, "rename", interrupt)
    with pytest.raises(OSError, match="power loss"):
        upgrade.apply_upgrade(plan)
    assert not target.exists()
    monkeypatch.setattr(Path, "rename", rename)
    assert upgrade.apply_upgrade(plan)["resumed_phases"] == 1


def test_checkpoint_identity_change_and_source_drift_refuse_resume(tmp_path, monkeypatch):
    source, target, _, plan = planned(tmp_path)
    monkeypatch.setattr(upgrade, "_convert", lambda *_: (_ for _ in ()).throw(OSError("interrupt")))
    with pytest.raises(OSError):
        upgrade.apply_upgrade(plan)
    (source / "documents" / "new-fact.txt").write_text("changed source")
    with pytest.raises(ValueError, match="source, converter or policy changed"):
        upgrade.apply_upgrade(plan)
    assert not target.exists()


def test_audit_separates_current_fact_and_artifact_changes_from_preserved_history(tmp_path):
    from contextlib import closing
    from bili_asr.storage.database import connect_database
    _, target, _, plan = planned(tmp_path)
    upgrade.apply_upgrade(plan)
    with closing(connect_database(target / "archive.db")) as connection:
        with connection:
            connection.execute("UPDATE workflow_jobs SET status='cancelled' WHERE status!='cancelled'")
    (target / "documents" / "new-fact.txt").write_text("new work")
    report = upgrade.audit_upgrade(target, expected_plan_id=plan["plan_id"])
    assert not report["installation_database_matches"]
    assert not report["baseline_reuse_allowed"]
    assert any(row["table"] == "workflow_jobs" and row["changed"] for row in report["current_facts"])
    assert all(not row["changed"] for row in report["immutable_history"])
    assert report["added_artifacts"] == ["documents/new-fact.txt"]
    with pytest.raises(ValueError, match="baseline changed"):
        upgrade.check_upgrade(target, expected_plan_id=plan["plan_id"])


def test_audit_artifact_only_drift_requires_new_snapshot(tmp_path):
    _, target, _, plan = planned(tmp_path)
    upgrade.apply_upgrade(plan)
    report = upgrade.audit_upgrade(target, expected_plan_id=plan["plan_id"])
    assert report["installation_database_matches"] and report["baseline_reuse_allowed"]
    (target / "documents" / "operator-fact.txt").write_text("new work")
    report = upgrade.audit_upgrade(target, expected_plan_id=plan["plan_id"])
    assert report["installation_database_matches"]
    assert not report["baseline_reuse_allowed"]
    assert report["next_action"] == "preserve new work and create a new snapshot"
