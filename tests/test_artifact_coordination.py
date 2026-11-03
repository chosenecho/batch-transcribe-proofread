"""Kernel lifetime, runtime pin recovery and shared capacity admission."""
from __future__ import annotations

import multiprocessing
from types import SimpleNamespace

import pytest

from bili_asr.archive_maintenance import ArchiveBusyError
from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.services.artifact_coordination import (
    consumer_pin,
    object_fence,
    reconcile_consumer_pins,
    reserve_local_space,
)
from bili_asr.storage.artifact_catalog import ArtifactCatalog
from bili_asr.storage.artifact_online import install_online_in_staged_copy
from tests.test_artifact_transfer import archive as audio_archive


def _fenced_reader(root, ready, finish):
    with object_fence(ArtifactRoots.of(root), "a" * 64, exclusive=False):
        ready.set()
        finish.wait(30)


def test_every_reader_process_must_exit_before_object_can_be_reclaimed(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    context = multiprocessing.get_context("spawn")
    processes = []
    try:
        for _ in range(2):
            ready = context.Event()
            finish = context.Event()
            process = context.Process(target=_fenced_reader, args=(root, ready, finish))
            process.start()
            processes.append((process, finish))
            assert ready.wait(15)
        processes[0][0].kill()
        processes[0][0].join(10)
        with pytest.raises(ArchiveBusyError), object_fence(ArtifactRoots.of(root), "a" * 64, exclusive=True):
            pass
    finally:
        for process, finish in processes:
            if process.is_alive():
                finish.set()
            process.join(15)
            if process.is_alive():
                process.kill()
                process.join(5)
    with object_fence(ArtifactRoots.of(root), "a" * 64, exclusive=True):
        pass


def test_only_dead_runtime_observations_are_retired(tmp_path):
    archive = audio_archive.__wrapped__(tmp_path)
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        catalog = ArtifactCatalog(session.connection)
        with session.connection:
            catalog.register_object(archive.digest, len(archive.data))
            catalog.pin_object(archive.digest, "runtime-consumer:crashed", pin_id="runtime:dead")
            catalog.pin_object(archive.digest, "manual investigation", pin_id="runtime:manual")
        with consumer_pin(session.connection, archive.roots, archive.digest, owner="still-reading"):
            assert reconcile_consumer_pins(session.connection, archive.roots) == 0
        assert reconcile_consumer_pins(session.connection, archive.roots) == 1
        assert catalog.pinned(archive.digest)
        assert session.connection.execute("SELECT released_at FROM artifact_pins WHERE pin_id='runtime:manual'").fetchone()[0] is None


def test_shared_capacity_admission_accounts_for_another_reservation(tmp_path, monkeypatch):
    import shutil
    archive = audio_archive.__wrapped__(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=1000))
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        install_online_in_staged_copy(session.connection)
        with (reserve_local_space(session.connection, archive.roots, 800, owner="restore"),
              pytest.raises(ValueError, match="unreserved"),
              reserve_local_space(session.connection, archive.roots, 300, owner="download")):
            pass
        alternative = tmp_path / "other-local-root"
        alternative.mkdir()
        with (reserve_local_space(session.connection, archive.roots, 800, owner="archive-root"),
              pytest.raises(ValueError, match="unreserved"),
              reserve_local_space(session.connection, ArtifactRoots.of(archive.root, alternative), 300, owner="artifact-root")):
            pass
        with reserve_local_space(session.connection, archive.roots, 1000, owner="after-release"):
            pass
