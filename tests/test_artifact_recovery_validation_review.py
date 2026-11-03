"""Replica fallback and text reads retain real bytes and explicit fences."""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from bili_asr.archive_maintenance import ArchiveBusyError
from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.services import artifact_consumer as consumer
from bili_asr.services.artifact_coordination import consumer_pin, object_fence
from bili_asr.services.artifact_inventory_service import (
    ArtifactSelection,
    inventory_artifacts,
    plan_artifact_offload,
)
from bili_asr.services.artifact_policy_configuration import validate_policy
from bili_asr.services.artifact_policy_defaults import DEFAULT_POLICY
from bili_asr.services.artifact_readers import read_retained_text
from bili_asr.services.artifact_transfer import transfer_artifacts
from bili_asr.services.workflow_audio_access import retained_audio
from bili_asr.storage_targets import bind_directory_target
from tests.support.artifact_online import archive as online_archive
from tests.test_artifact_transfer import plan, transfer


@pytest.mark.parametrize("invalid", [{"unknown": object()}, {"cycle": None}])
def test_non_json_policy_does_not_install_an_unreadable_configuration(invalid):
    if invalid.get("cycle") is None and "cycle" in invalid:
        invalid["cycle"] = invalid
    with pytest.raises(ValueError, match="must be a JSON value"):
        validate_policy(invalid)


def test_enabled_policy_requires_a_target_before_any_artifact_execution():
    value = deepcopy(DEFAULT_POLICY)
    value["mode"] = "offload"
    with pytest.raises(ValueError, match="requires a target identity"):
        validate_policy(value)
    assert value["target_id"] is None


@pytest.mark.parametrize("alternative", [False, True])
def test_restore_io_failure_uses_another_verified_target_or_reports_unavailable(tmp_path, monkeypatch, alternative):
    archive = online_archive.__wrapped__(tmp_path)
    transfer(archive, plan(archive), mode="copy")
    bindings = {"cold": archive.target}
    if alternative:
        target = tmp_path / "alternative"
        target.mkdir()
        bind_directory_target(target, "alternate", roots=archive.roots)
        inventory = inventory_artifacts(archive.root / "archive.db", archive.roots, deep=True,
                                       selection=ArtifactSelection(kinds=("audio",)), external_holds={})
        frozen = plan_artifact_offload(inventory, target_id="alternate")
        transfer_artifacts(archive.roots, frozen, target_root=target, external_holds={})
        bindings["alternate"] = target
    (archive.root / "audio/input.m4a").unlink()
    attempts = []
    original = consumer.restore_artifact

    def fail_first(*arguments, **options):
        attempts.append(options["target_id"])
        if len(attempts) == 1:
            raise OSError("injected I/O failure after locating a verified package")
        return original(*arguments, **options)

    monkeypatch.setattr(consumer, "restore_artifact", fail_first)
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session, consumer_pin(
        session.connection, archive.roots, archive.digest, owner="validation-reader"
    ):
        retained = retained_audio(session.connection, 1)
        if alternative:
            restored = consumer.ensure_local(session.connection, archive.roots, retained, storage_targets=bindings)
            assert restored.read_bytes() == archive.data
            assert len(attempts) == 2 and len(set(attempts)) == 2
        else:
            with pytest.raises(ValueError, match="no supplied replica could restore exact audio; cold:OSError"):
                consumer.ensure_local(session.connection, archive.roots, retained, storage_targets=bindings)
            assert attempts == ["cold"]
            assert not (archive.root / "audio/input.m4a").exists()
        assert session.connection.execute("SELECT count(*) FROM workflow_attempts").fetchone()[0] == 0
        assert session.connection.execute("SELECT count(*) FROM artifact_reservations WHERE released_at IS NULL").fetchone()[0] == 0


def test_online_text_read_obeys_object_fence_and_original_hash(tmp_path):
    archive = online_archive.__wrapped__(tmp_path)
    text = "原始冻结稿\n".encode()
    digest = hashlib.sha256(text).hexdigest()
    path = archive.root / "documents/frozen.txt"
    path.parent.mkdir()
    path.write_bytes(text)
    def competing_reader():
        with ArchiveSession(archive.root, mode=ArchiveAccessMode.READ) as reader:
            return read_retained_text(reader.connection, "documents/frozen.txt", digest, (archive.root,))

    with (object_fence(archive.roots, digest, exclusive=True), ThreadPoolExecutor(max_workers=1) as workers,
          pytest.raises(ArchiveBusyError)):
        workers.submit(competing_reader).result(timeout=5)
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.READ) as session:
        assert read_retained_text(session.connection, "documents/frozen.txt", digest, (archive.root,), max_bytes=100) == text
        path.write_bytes(b"changed")
        with pytest.raises(ValueError, match="integrity"):
            read_retained_text(session.connection, "documents/frozen.txt", digest, (archive.root,))
