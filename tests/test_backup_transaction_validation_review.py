"""Mismatched container semantics cannot move a completed backup pointer."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bili_asr.services import maintenance_backup as backup
from bili_asr.services import maintenance_task_state as task_state
from bili_asr.storage.maintenance_selection import table_ledger
from tests.support.artifact_online import archive as online_archive


@pytest.mark.parametrize("profile,media_policy", [("archive", "verified-references"), ("task-state", "future-policy")])
def test_reference_policy_cannot_silently_change_another_backup_profile(tmp_path, profile, media_policy):
    with pytest.raises(ValueError, match="requires task-state profile"):
        backup.policy(tmp_path / "source", tmp_path / "backups", profile=profile, media_policy=media_policy,
                      mode="full", no_external_control_state=profile == "task-state")
    assert not (tmp_path / "backups").exists()


def test_valid_v1_package_cannot_complete_an_explicit_v2_run_or_replace_last_success(tmp_path, monkeypatch):
    archive = online_archive.__wrapped__(tmp_path)
    document = backup.policy(archive.root, tmp_path / "repository", profile="task-state", mode="full",
                             media_policy="verified-references", no_external_control_state=True)
    before = table_ledger(archive.root / "archive.db")
    first = backup.run_backup(document, now=100)
    assert first["valid"] and first["status"] == "success"
    original = task_state.save_task_state

    def produce_registered_but_wrong_media_version(*arguments, **options):
        options["media_policy"] = "local-required"
        return original(*arguments, **options)

    monkeypatch.setattr(task_state, "save_task_state", produce_registered_but_wrong_media_version)
    failed = backup.run_backup(document, now=101)
    assert not failed["valid"] and failed["status"] == "failed"
    assert "media version does not match its explicit policy" in failed["error"]
    repository = Path(document["repository"])
    state = json.loads((repository / "runs" / document["policy_id"] / "state.json").read_bytes())
    assert state["last_success"] == 100 and state["latest"] == first["snapshot"]
    assert state["failures"] == 1
    assert task_state.verify_task_state(repository / first["snapshot"])["manifest"]["format"] == task_state.REFERENCE_FORMAT
    # Wrong format is itself valid inactive evidence; validity cannot imply policy compatibility.
    assert task_state.verify_task_state(repository / failed["snapshot"])["manifest"]["format"] == task_state.FORMAT
    assert table_ledger(archive.root / "archive.db") == before
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data
