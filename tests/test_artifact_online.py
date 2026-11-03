"""Online fences, policy limits, readiness and peak-space contention."""
from __future__ import annotations

import json
import multiprocessing
import sqlite3
from types import SimpleNamespace

import pytest

from bili_asr.archive_maintenance import ArchiveBusyError
from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.services.artifact_consumer import candidate_artifact_access
from bili_asr.services.artifact_coordination import (
    consumer_pin,
    object_fence,
    reconcile_consumer_pins,
    reserve_local_space,
)
from bili_asr.services.artifact_policy import (
    download_reservation,
)
from bili_asr.services.artifact_transfer import transfer_artifacts
from bili_asr.storage.artifact_catalog import ArtifactCatalog
from bili_asr.storage.workflow import WorkflowRepository
from bili_asr.workflow import WorkflowExecutor
from bili_asr.workflow_models import JobKind
from tests.support.artifact_online import archive as online_archive
from tests.support.artifact_online import configure
from tests.test_artifact_transfer import plan, transfer


@pytest.fixture
def archive(tmp_path):
    return online_archive.__wrapped__(tmp_path)


def _reader(root, identity, ready, finish):
    roots = ArtifactRoots.of(root)
    with ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session, consumer_pin(
        session.connection, roots, identity, owner="expired-worker"
    ):
        ready.set()
        finish.wait(30)


def test_two_live_readers_cannot_be_fenced_or_reclaimed_even_with_old_pin_dates(archive):
    context = multiprocessing.get_context("spawn")
    finish = context.Event()
    readers = []
    try:
        for _ in range(2):
            ready = context.Event()
            process = context.Process(target=_reader, args=(archive.root, archive.digest, ready, finish))
            process.start()
            readers.append(process)
            assert ready.wait(15)
        with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
            with session.connection:
                session.connection.execute("UPDATE artifact_pins SET created_at=0")
            assert reconcile_consumer_pins(session.connection, archive.roots) == 0
            with pytest.raises(ArchiveBusyError), object_fence(archive.roots, archive.digest, exclusive=True):
                pass
        assert (archive.root / "audio/input.m4a").read_bytes() == archive.data
    finally:
        finish.set()
        for process in readers:
            process.join(15)
            if process.is_alive():
                process.kill()
                process.join(5)
    with object_fence(archive.roots, archive.digest, exclusive=True):
        pass


def test_crashed_reader_pin_is_retired_only_after_kernel_releases_fence(archive):
    context = multiprocessing.get_context("spawn")
    ready, finish = context.Event(), context.Event()
    process = context.Process(target=_reader, args=(archive.root, archive.digest, ready, finish))
    process.start()
    assert ready.wait(15)
    process.kill()
    process.join(10)
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        assert reconcile_consumer_pins(session.connection, archive.roots) == 1
        assert not ArtifactCatalog(session.connection).pinned(archive.digest)








def _seed_candidates(archive):
    from tests.test_artifact_inventory_service import attempt, job
    with sqlite3.connect(archive.root / "archive.db") as connection:
        job(connection, "audio", kind="audio", status="succeeded")
        attempt(connection, "acquired", "audio", key="audio/input.m4a", content=archive.data)
        job(connection, "asr", status="queued")
        connection.execute("INSERT INTO workflow_job_dependencies VALUES ('asr','audio')")
        job(connection, "subtitle", kind="subtitle", status="queued")
        connection.execute("UPDATE workflow_jobs SET payload_json=? WHERE kind IN ('audio','subtitle')", (json.dumps({"video_part_id": 1}),))
        connection.execute("UPDATE workflow_jobs SET payload_json=? WHERE kind='asr'", (json.dumps({"video_part_id": 1, "profile_id": 1}),))


def test_missing_retained_input_is_skipped_without_attempt_and_other_work_runs(archive):
    transfer(archive, plan(archive), mode="offload")
    _seed_candidates(archive)
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        repository = WorkflowRepository(session.connection)
        executor = WorkflowExecutor(repository, worker_id="test", handlers={JobKind.ASR: lambda job: pytest.fail("blocked ASR ran"), JobKind.SUBTITLE: lambda job: {}},
            kinds=(JobKind.ASR, JobKind.SUBTITLE), candidate_access=lambda candidate: candidate_artifact_access(
                session.connection, repository, archive.roots, candidate, storage_targets={}))
        report = executor.run()
        assert (report.succeeded, report.failed, report.blocked) == (1, 0, 1)
        assert session.connection.execute("SELECT status,attempt_count FROM workflow_jobs WHERE job_id='asr'").fetchone()[:] == ("queued", 0)
        assert session.connection.execute("SELECT COUNT(*) FROM workflow_attempts WHERE job_id='asr'").fetchone()[0] == 0
        assert executor.run().failed == 0


def test_preclaim_restore_and_pin_cover_actual_handler_lifetime(archive):
    transfer(archive, plan(archive), mode="offload")
    _seed_candidates(archive)
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        repository = WorkflowRepository(session.connection)
        def handler(job):
            assert ArtifactCatalog(session.connection).pinned(archive.digest)
            assert (archive.root / "audio/input.m4a").read_bytes() == archive.data
            with pytest.raises(ArchiveBusyError), object_fence(archive.roots, archive.digest, exclusive=True):
                pass
            return {}
        executor = WorkflowExecutor(repository, worker_id="test", handlers={JobKind.ASR: handler}, kinds=(JobKind.ASR,),
            candidate_access=lambda candidate: candidate_artifact_access(session.connection, repository, archive.roots,
                candidate, storage_targets={"cold": archive.target}))
        report = executor.run()
        assert (report.succeeded, report.failed, report.blocked) == (1, 0, 0)
        assert not ArtifactCatalog(session.connection).pinned(archive.digest)


def test_shared_reservations_block_competing_restore_and_download_peak(archive, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=1000))
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        with (reserve_local_space(session.connection, archive.roots, 700, owner="restore"),
              pytest.raises(ValueError, match="unreserved"),
              reserve_local_space(session.connection, archive.roots, 400, owner="download")):
            pass
        with reserve_local_space(session.connection, archive.roots, 1000, owner="after"):
            pass
    configure(archive)
    with (ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session,
          pytest.raises(ValueError, match="backpressure"),
          download_reservation(session.connection, archive.roots, 1, owner="new")):
        pass


def test_online_transfer_refuses_live_reader_even_with_precomputed_plan(archive):
    frozen = plan(archive)
    with (ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session,
          consumer_pin(session.connection, archive.roots, archive.digest, owner="reader"),
          pytest.raises(ArchiveBusyError)):
        transfer_artifacts(archive.roots, frozen, target_root=archive.target, mode="offload", external_holds={}, _online=True)
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data




def _migration_owner(root, frozen, target, ready, finish, outcomes):
    from bili_asr.services import artifact_transfer as service
    original = service.create_artifact_package
    def wait(*args, **kwargs):
        ready.set()
        if not finish.wait(30):
            raise RuntimeError("test rendezvous timeout")
        return original(*args, **kwargs)
    service.create_artifact_package = wait
    try:
        outcome = service.transfer_artifacts(ArtifactRoots.of(root), frozen, target_root=target,
                    mode="offload", external_holds={}, _online=True)
        outcomes.put(outcome["operation"]["state"])
    except BaseException as error:  # noqa: BLE001 - report child failure to parent assertion.
        outcomes.put(type(error).__name__ + ":" + str(error))


def test_two_migrators_cannot_commit_or_release_the_same_generation(archive):
    context = multiprocessing.get_context("spawn")
    ready, finish, outcomes = context.Event(), context.Event(), context.Queue()
    frozen = plan(archive)
    process = context.Process(target=_migration_owner, args=(archive.root, frozen, archive.target, ready, finish, outcomes))
    process.start()
    try:
        assert ready.wait(15)
        with pytest.raises(ArchiveBusyError):
            transfer_artifacts(archive.roots, frozen, target_root=archive.target, mode="offload", external_holds={}, _online=True)
        assert (archive.root / "audio/input.m4a").read_bytes() == archive.data
    finally:
        finish.set()
        process.join(20)
        if process.is_alive():
            process.kill()
            process.join(5)
    assert outcomes.get(timeout=5) == "complete"
    assert not (archive.root / "audio/input.m4a").exists()


def test_new_queued_consumer_at_final_hash_prevents_release(archive, monkeypatch):
    from bili_asr.services import artifact_release
    from tests.test_artifact_inventory_service import job
    original = artifact_release._verify
    calls = 0
    def inject(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 2:
            with sqlite3.connect(archive.root / "archive.db") as connection:
                job(connection, "new-asr", status="queued")
        return result
    monkeypatch.setattr(artifact_release, "_verify", inject)
    with pytest.raises(ValueError, match="retention guard"):
        transfer_artifacts(archive.roots, plan(archive), target_root=archive.target, mode="offload", external_holds={}, _online=True)
    assert archive.data in [path.read_bytes() for path in (archive.root / "audio").iterdir() if path.is_file()]


class ChildFenceRunner:
    def __init__(self, config):
        self.config = config
    def set_hotword_evidence(self, **kwargs):
        pass
    def transcribe(self, path, **kwargs):
        import time
        from pathlib import Path
        Path(path + ".reading").write_text("ready")
        time.sleep(30)
        return [{"start": 0, "end": 1, "text": "done"}]
    def rebuild_hotwords_from_first_pass(self, text):
        return []
    def release(self):
        pass


def test_actual_persistent_child_holds_input_fence_until_terminated(archive):
    import threading
    import time
    from pathlib import Path

    from bili_asr.asr.config import ASRConfig
    from bili_asr.asr.session import AsrInferenceSession, InferenceRequest
    session = AsrInferenceSession(runner_factory=ChildFenceRunner)
    result = []
    def run():
        try:
            session.transcribe(ASRConfig(model_name="offline", device="cpu"), str(archive.root / "audio/input.m4a"),
                request=InferenceRequest("one", "worker", 1, "a" * 64, {}), paired_subtitle_text=None,
                timeout_seconds=4, artifact_context={"archive_root": str(archive.root), "artifact_root": None, "object_id": archive.digest})
        except Exception as error:  # noqa: BLE001 - assert the exact observed timeout type below.
            result.append(type(error).__name__)
    worker = threading.Thread(target=run)
    worker.start()
    marker = Path(str(archive.root / "audio/input.m4a") + ".reading")
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    try:
        assert marker.exists()
        with pytest.raises(ArchiveBusyError), object_fence(archive.roots, archive.digest, exclusive=True):
            pass
        worker.join(12)
        assert not worker.is_alive()
        assert result == ["ASRInferenceTimeoutError"]
        with object_fence(archive.roots, archive.digest, exclusive=True):
            pass
    finally:
        session.close()
        worker.join(5)






def test_migration_fences_publication_slot_until_old_group_commit(archive):
    from bili_asr.services.artifact_groups import (
        capture_artifact_groups,
        restore_artifact_group,
    )
    from bili_asr.workflow_runtime import ArchiveWorkflowHandlers
    from tests.test_artifact_groups import publish, text_plan
    paths = publish(archive)
    group = capture_artifact_groups(archive.roots)["groups"][0]
    frozen = text_plan(archive)
    context = multiprocessing.get_context("spawn")
    ready, finish, outcomes = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=_migration_owner, args=(archive.root, frozen, archive.target, ready, finish, outcomes))
    process.start()
    try:
        assert ready.wait(15)
        with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
            handlers = object.__new__(ArchiveWorkflowHandlers)
            handlers.connection, handlers.artifact_roots = session.connection, archive.roots
            handlers._publish = lambda job: pytest.fail("publication overwrote a fenced group")
            with pytest.raises(ArchiveBusyError):
                handlers.publish(SimpleNamespace(video_part_id=1))
            assert b"first" in (archive.root / paths["raw_path"]).read_bytes()
    finally:
        finish.set()
        process.join(20)
        if process.is_alive():
            process.kill()
            process.join(5)
    assert outcomes.get(timeout=5) == "complete"
    restore_artifact_group(archive.roots, group, storage_targets={"cold": archive.target})
    assert b"first" in (archive.root / paths["raw_path"]).read_bytes()


def test_manual_pins_are_never_retired_by_consumer_reconciliation(archive):
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        with session.connection:
            ArtifactCatalog(session.connection).pin_object(archive.digest, "manual investigation", pin_id="runtime:manual")
        assert reconcile_consumer_pins(session.connection, archive.roots) == 0
        assert ArtifactCatalog(session.connection).pinned(archive.digest)


def test_child_rejects_valid_path_with_wrong_fenced_identity(archive):
    from bili_asr.services.artifact_child import child_artifact_access
    with pytest.raises(ValueError, match="fenced byte identity"), child_artifact_access(
        {"archive_root": str(archive.root), "artifact_root": None, "object_id": "a" * 64},
        archive.root / "audio/input.m4a",
    ):
        pytest.fail("decoder must not see bytes under an unrelated fence")








def _restore_owner(root, target, identity, ready, finish, outcomes):
    from bili_asr.services import artifact_consumer as service
    from bili_asr.services.workflow_audio_access import retained_audio
    original = service.restore_artifact
    def wait(*args, **kwargs):
        ready.set()
        if not finish.wait(30):
            raise RuntimeError("test rendezvous timeout")
        return original(*args, **kwargs)
    service.restore_artifact = wait
    roots = ArtifactRoots.of(root)
    with (ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session,
          consumer_pin(session.connection, roots, identity, owner="restore-first")):
        restored = service.ensure_local(session.connection, roots, retained_audio(session.connection, 1),
                                        storage_targets={"cold": target})
        outcomes.put(restored.read_bytes())


def test_concurrent_restores_have_one_install_and_read_pin_excludes_migration(archive):
    from bili_asr.services.artifact_consumer import ensure_local
    from bili_asr.services.workflow_audio_access import retained_audio
    transfer(archive, plan(archive), mode="offload")
    context = multiprocessing.get_context("spawn")
    ready, finish, outcomes = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=_restore_owner, args=(archive.root, archive.target, archive.digest, ready, finish, outcomes))
    process.start()
    try:
        assert ready.wait(20)
        with (ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session,
              consumer_pin(session.connection, archive.roots, archive.digest, owner="restore-second")):
            with pytest.raises(ArchiveBusyError):
                ensure_local(session.connection, archive.roots, retained_audio(session.connection, 1),
                             storage_targets={"cold": archive.target})
            with pytest.raises(ArchiveBusyError), object_fence(archive.roots, archive.digest, exclusive=True):
                pass
    finally:
        finish.set()
        process.join(20)
        if process.is_alive():
            process.kill()
            process.join(5)
    assert outcomes.get(timeout=5) == archive.data
    with sqlite3.connect(archive.root / "archive.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_transfers WHERE kind='restore'").fetchone()[0] == 1
