"""Authorized lifecycle, current guards, cold ASR inputs and crash recovery."""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import random
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.services import artifact_transfer, audio_retention
from bili_asr.services.artifact_policy import configure_policy, run_policy_once
from bili_asr.services.audio_retention_policy import default_audio_retention_policy
from bili_asr.storage.artifact_catalog import ArtifactCatalog
from bili_asr.storage_targets import bind_directory_target
from tests.support.artifact_online import archive as online_archive
from tests.test_artifact_transfer import domain_facts


@pytest.fixture
def archive(tmp_path):
    return online_archive.__wrapped__(tmp_path)


def configure(archive, **changes):
    config = default_audio_retention_policy()
    config.update(mode="offload", target_id="cold", minimum_age_seconds=0, local_retention_seconds=0,
        trigger_free_bytes=0, stop_free_bytes=1, minimum_free_bytes=0, bytes_per_second=0)
    config.update(changes)
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        configure_policy(session.connection, "audio", config)
    return config


def run(archive, **options):
    return run_policy_once(archive.roots, policy_id="audio", target_root=archive.target, external_holds={}, **options)


def ready(archive):
    with sqlite3.connect(archive.root / "archive.db") as connection:
        connection.execute("UPDATE artifact_policy_runs SET retry_after=0")


@pytest.mark.parametrize("allow_delete", [False, True])
@pytest.mark.parametrize("priority", ["largest", "oldest"])
def test_bounded_backup_progresses_past_copied_audio_retained_for_consumers(archive, allow_delete, priority):
    from tests.test_artifact_inventory_service import job

    expected = {archive.digest}
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session, session.connection:
        job(session.connection, "queued-ai", kind="proofread", status="queued")
        for index, content in enumerate((b"second audio", b"third audio"), 2):
            identity = hashlib.sha256(content).hexdigest()
            expected.add(identity)
            key = f"audio/extra-{index}.m4a"
            (archive.root / key).write_bytes(content)
            session.connection.execute("INSERT INTO audio_objects VALUES (?,?,?,'m4a',1000,?,1)",
                                       (index, identity, len(content), key))
            session.connection.execute("INSERT INTO part_audio_objects VALUES (1,?,1,'test')", (index,))
            catalog = ArtifactCatalog(session.connection)
            catalog.register_object(identity, len(content))
            catalog.bind_audio(index, identity)
    configure(archive, allow_delete=allow_delete, batch_objects=1, priority=priority)
    copied = set()
    for _ in range(3):
        ready(archive)
        report = run(archive)
        assert report["state"] == ("blocked" if allow_delete else "complete")
        assert report["released_bytes"] == 0
        copied.update(receipt["object_id"] for receipt in report["object_receipts"])
    assert copied == expected
    assert len(list((archive.root / "audio").glob("*.m4a"))) == 3
    if allow_delete:
        with sqlite3.connect(archive.root / "archive.db") as connection:
            connection.execute("UPDATE workflow_jobs SET status='succeeded' WHERE job_id='queued-ai'")
        released = 0
        for _ in range(3):
            ready(archive)
            released += run(archive)["released_bytes"]
        assert released == len(archive.data) + len(b"second audio") + len(b"third audio")
        assert not list((archive.root / "audio").glob("*.m4a"))


def test_defaults_and_explicit_offload_without_delete_permission_only_back_up(archive):
    assert default_audio_retention_policy()["allow_delete"] is False
    assert run(archive)["state"] == "off"
    configure(archive)
    before = domain_facts(archive)
    report = run(archive)
    assert report["state"] == "complete" and report["delete_authorized"] is False
    assert report["released_bytes"] == 0
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data
    assert report["compression"][0]["original_payload_bytes"] == len(archive.data)
    assert report["compression"][0]["container_to_original_ratio"] > 1
    assert domain_facts(archive) == before


@pytest.mark.parametrize("changes", [{"allow_delete": "true"}, {"minimum_verified_copies": 0},
    {"minimum_verified_copies": 2}, {"replica_target_ids": ["cold"]}, {"max_concurrency": 2},
    {"minimum_age_seconds": 1}, {"selection": {"kinds": ["document"]}}, {"unknown": True}])
def test_policy_rejects_unsafe_or_ambiguous_authorization_before_persistence(archive, changes):
    with pytest.raises(ValueError):
        configure(archive, **changes)
    with sqlite3.connect(archive.root / "archive.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_policies").fetchone()[0] == 0


def test_explicit_authorization_runs_verify_release_and_restores_exact_asr_input(archive):
    configure(archive, allow_delete=True)
    before = domain_facts(archive)
    report = run(archive)
    assert report["state"] == "complete" and report["released_bytes"] == len(archive.data)
    assert report["object_receipts"] == [{"object_id": archive.digest, "original_byte_size": len(archive.data), "verified_target_ids": ["cold"]}]
    assert report["release_receipts"][0]["object_id"] == archive.digest
    assert report["release_receipts"][0]["state"] == "released"
    assert not (archive.root / "audio/input.m4a").exists()
    assert len(list((archive.target / "packages").glob("*.zip"))) == 1  # Reuse the sealed backup.
    assert domain_facts(archive) == before
    from bili_asr.services.artifact_consumer import candidate_artifact_access
    from bili_asr.storage.workflow import WorkflowRepository
    from bili_asr.workflow import WorkflowExecutor
    from bili_asr.workflow_models import JobKind
    from tests.test_artifact_online import _seed_candidates
    _seed_candidates(archive)
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        repository = WorkflowRepository(session.connection)
        profile = list(session.connection.execute("SELECT * FROM workflow_asr_profiles"))
        def handler(job):
            assert ArtifactCatalog(session.connection).pinned(archive.digest)
            assert hashlib.sha256((archive.root / "audio/input.m4a").read_bytes()).hexdigest() == archive.digest
            assert job.profile_id == 1
            return {"restored_input_sha256": archive.digest}
        executor = WorkflowExecutor(repository, worker_id="retention-test", handlers={JobKind.ASR: handler}, kinds=(JobKind.ASR,),
            candidate_access=lambda job: candidate_artifact_access(session.connection, repository, archive.roots,
                job, storage_targets={"cold": archive.target}))
        outcome = executor.run()
        assert (outcome.succeeded, outcome.failed, outcome.blocked) == (1, 0, 0)
        assert list(session.connection.execute("SELECT * FROM workflow_asr_profiles")) == profile
        assert session.connection.execute("SELECT COUNT(*) FROM workflow_attempts WHERE job_id='asr'").fetchone()[0] == 1


@pytest.mark.parametrize("kind,status", [("audio", "queued"), ("asr", "running"), ("proofread", "queued"),
    ("publish", "failed"), ("render_document", "cancelled")])
def test_all_unfinished_consumers_preserve_audio_and_attempt_history(archive, kind, status):
    from tests.test_artifact_inventory_service import job
    with sqlite3.connect(archive.root / "archive.db") as connection:
        job(connection, "pending", kind=kind, status=status)
    configure(archive, allow_delete=True)
    before = domain_facts(archive)
    report = run(archive)
    assert report["state"] == "blocked"
    assert report["object_receipts"][0]["object_id"] == archive.digest
    assert report["copy_backlog"]["objects"] == 0
    assert report["release_blockers"] and report["released_bytes"] == 0
    assert len(list((archive.target / "packages").glob("*.zip"))) == 1
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data
    assert domain_facts(archive) == before


def test_queued_ai_and_failed_old_asr_copy_without_deleting_unique_source(archive):
    from tests.test_artifact_inventory_service import job
    with sqlite3.connect(archive.root / "archive.db") as connection:
        job(connection, "old-asr", kind="asr", status="failed")
        job(connection, "replacement", kind="asr", status="succeeded")
        job(connection, "ai", kind="proofread", status="queued")
    configure(archive, allow_delete=True)
    before = domain_facts(archive)
    report = run(archive)
    assert report["state"] == "blocked" and report["released_bytes"] == 0
    assert report["object_receipts"][0]["original_byte_size"] == len(archive.data)
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data
    assert domain_facts(archive) == before
    ready(archive)
    resumed = run(archive)
    assert resumed["object_receipts"] == report["object_receipts"]
    assert len(list((archive.target / "packages").glob("*.zip"))) == 1


def test_copy_shares_active_consumer_read_fence_but_release_remains_guarded(archive):
    from bili_asr.services.artifact_coordination import consumer_pin
    configure(archive, allow_delete=True)
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        with consumer_pin(session.connection, archive.roots, archive.digest, owner="live-asr"):
            report = run(archive)
            assert report["state"] == "blocked"
            assert report["object_receipts"][0]["object_id"] == archive.digest
            assert ArtifactCatalog(session.connection).pinned(archive.digest)
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data


def test_copy_plan_cannot_authorize_offload_or_release_reconciliation(archive):
    from bili_asr.services.artifact_inventory_service import inventory_artifacts, plan_artifact_copy
    inventory = inventory_artifacts(archive.root / "archive.db", archive.roots, deep=True, external_holds=None)
    plan = plan_artifact_copy(inventory, target_id="cold")
    assert plan["items"] and plan["external_holds_verified"] is False
    for action in (
        lambda: artifact_transfer.transfer_artifacts(archive.roots, plan, target_root=archive.target, mode="offload"),
        lambda: artifact_transfer.reconcile_artifact_transfer(archive.roots, plan, target_root=archive.target),
    ):
        with pytest.raises(ValueError, match="copy plan cannot authorize"):
            action()
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data


def test_legacy_frozen_copy_gate_is_replanned_without_clearing_consumer_history(archive):
    from bili_asr.services.artifact_inventory_service import inventory_artifacts, plan_artifact_offload
    from tests.test_artifact_inventory_service import job
    with sqlite3.connect(archive.root / "archive.db") as connection:
        job(connection, "old-asr", kind="asr", status="failed")
    configure(archive, allow_delete=True)
    assert run(archive)["state"] == "blocked"
    inventory = inventory_artifacts(archive.root / "archive.db", archive.roots, deep=True, external_holds={})
    old = plan_artifact_offload(inventory, target_id="cold")
    assert not old["items"]
    frozen = {"format": "audio-retention-plan/v1", "backup": old, "release": old, "pressure_active": False}
    with sqlite3.connect(archive.root / "archive.db") as connection:
        connection.execute("UPDATE artifact_policy_runs SET state='failed',retry_after=0,plan_json=?", (json.dumps(frozen),))
    before = domain_facts(archive)
    report = run(archive)
    assert report["state"] == "blocked" and report["object_receipts"]
    assert report["legacy_plan_replanned"] == old["plan_sha256"]
    assert report["released_bytes"] == 0 and domain_facts(archive) == before


def test_transitive_ai_repair_and_external_hold_without_part_column_protect_audio(archive):
    from tests.test_artifact_inventory_service import job
    with sqlite3.connect(archive.root / "archive.db") as connection:
        job(connection, "asr", kind="asr", status="succeeded")
        job(connection, "draft", kind="proofread", status="succeeded")
        job(connection, "repair", kind="proofread", status="queued")
        connection.execute("UPDATE workflow_jobs SET video_part_id=NULL WHERE job_id IN ('draft','repair')")
        connection.execute("INSERT INTO workflow_job_dependencies VALUES ('draft','asr')")
        connection.execute("INSERT INTO workflow_job_dependencies VALUES ('repair','draft')")
    configure(archive, allow_delete=True)
    assert run(archive)["state"] == "blocked"
    with sqlite3.connect(archive.root / "archive.db") as connection:
        connection.execute("UPDATE workflow_jobs SET status='succeeded' WHERE job_id='repair'")
    ready(archive)
    report = run_policy_once(archive.roots, policy_id="audio", target_root=archive.target, external_holds={"repair": ["manual investigation"]})
    assert report["state"] == "blocked"
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data


def test_new_consumer_at_final_replica_hash_stops_delete_and_restores_isolation(archive, monkeypatch):
    from tests.test_artifact_inventory_service import job
    configure(archive, allow_delete=True)
    original = audio_retention.AudioReplicaRequirement.verify
    calls = [0]
    def verify(self, connection, identity):
        guard = original(self, connection, identity)
        calls[0] += 1
        if calls[0] > 1:
            with sqlite3.connect(archive.root / "archive.db") as other:
                job(other, "new-repair", kind="proofread", status="queued")
        return guard
    monkeypatch.setattr(audio_retention.AudioReplicaRequirement, "verify", verify)
    report = run(archive)
    assert report["state"] == "failed" and report["released_bytes"] == 0
    assert report["restored_isolated_copies"] == 1
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data


@pytest.mark.parametrize("guard", ["manual_pin", "external_hold", "unknown_holds"])
def test_pins_and_holds_cannot_be_authorized_away(archive, guard):
    configure(archive, allow_delete=True)
    if guard == "manual_pin":
        with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session, session.connection:
            ArtifactCatalog(session.connection).pin_object(archive.digest, "investigation")
        report = run(archive)
    else:
        report = run_policy_once(archive.roots, policy_id="audio", target_root=archive.target,
            external_holds=None if guard == "unknown_holds" else {"part:1": ["retry_hold"]})
    assert report["state"] == "blocked"
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data


def test_local_age_keeps_recently_completed_audio_until_retention_expiry(archive, monkeypatch):
    clock = [(archive.root / "audio/input.m4a").stat().st_mtime + 10]
    monkeypatch.setattr(audio_retention.time, "time", lambda: clock[0])
    configure(archive, allow_delete=True, local_retention_seconds=100)
    copied = run(archive)
    assert copied["released_bytes"] == 0
    clock[0] += 101
    ready(archive)
    released = run(archive)
    assert released["released_bytes"] == len(archive.data)


def test_minimum_age_starts_after_latest_consumer_finishes(archive, monkeypatch):
    from tests.test_artifact_inventory_service import job
    clock = [(archive.root / "audio/input.m4a").stat().st_mtime + 1000]
    with sqlite3.connect(archive.root / "archive.db") as connection:
        job(connection, "complete", kind="proofread", status="succeeded")
        connection.execute("UPDATE workflow_jobs SET updated_at=?", (int(clock[0]),))
    monkeypatch.setattr(audio_retention.time, "time", lambda: clock[0])
    configure(archive, allow_delete=True, minimum_age_seconds=50, local_retention_seconds=50)
    assert run(archive)["state"] == "blocked"
    clock[0] += 51
    ready(archive)
    assert run(archive)["released_bytes"] == len(archive.data)


def test_two_verified_directory_targets_are_required_and_evidence_is_saved(archive, tmp_path):
    second = tmp_path / "second"
    second.mkdir()
    bind_directory_target(second, "second", roots=archive.roots)
    configure(archive, allow_delete=True, replica_target_ids=["second"], minimum_verified_copies=2)
    failed = run(archive)
    assert failed["state"] == "failed" and "target_unbound" in failed["reason"]
    assert (archive.root / "audio/input.m4a").exists()
    ready(archive)
    released = run(archive, storage_targets={"second": second})
    assert released["released_bytes"] == len(archive.data)
    assert {row["target_id"] for row in released["compression"]} == {"cold", "second"}
    with sqlite3.connect(archive.root / "archive.db") as connection:
        result = json.loads(connection.execute("SELECT result_json FROM artifact_policy_runs ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        assert result["delete_authorized"] is True and result["policy_sha256"] == released["policy_sha256"]


def test_cold_restore_tries_a_verified_second_replica_when_first_payload_is_corrupt(archive, tmp_path):
    import zipfile

    from bili_asr.services.artifact_consumer import ensure_local
    from bili_asr.services.artifact_coordination import consumer_pin
    from bili_asr.services.workflow_audio_access import retained_audio
    second = tmp_path / "second"
    second.mkdir()
    bind_directory_target(second, "second", roots=archive.roots)
    configure(archive, allow_delete=True, replica_target_ids=["second"], minimum_verified_copies=2)
    assert run(archive, storage_targets={"second": second})["released_bytes"] == len(archive.data)
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        # Corrupt the first preferred payload while leaving a readable manifest.
        replica = next(row for row in ArtifactCatalog(session.connection).replicas_for_object(archive.digest)
                       if row["package_id"] is not None and row["presence"] == "present")
        bindings = {"cold": archive.target, "second": second}
        path = bindings[replica["target_id"]] / replica["relative_key"]
        replacement = tmp_path / "corrupt.zip"
        with zipfile.ZipFile(path) as incoming, zipfile.ZipFile(replacement, "w") as outgoing:
            for info in incoming.infolist():
                content = incoming.read(info)
                if info.filename.startswith("objects/"):
                    content = b"X" + content[1:]
                outgoing.writestr(info.filename, content, compress_type=info.compress_type)
        os.replace(replacement, path)
        with consumer_pin(session.connection, archive.roots, archive.digest, owner="recovery-test"):
            restored = ensure_local(session.connection, archive.roots, retained_audio(session.connection, 1), storage_targets=bindings)
            assert hashlib.sha256(restored.read_bytes()).hexdigest() == archive.digest


@pytest.mark.parametrize("fault", ["missing", "corrupt", "changed_after_hash", "changed_and_restored_mtime", "aba"])
def test_replica_failure_at_release_preserves_the_unique_local_bytes(archive, tmp_path, monkeypatch, fault):
    second = tmp_path / "second"
    second.mkdir()
    bind_directory_target(second, "second", roots=archive.roots)
    configure(archive, allow_delete=True, replica_target_ids=["second"], minimum_verified_copies=2)
    original = artifact_transfer.release_copy
    def disturb(*args, **kwargs):
        package = next((second / "packages").glob("*.zip"))
        if fault in {"changed_after_hash", "changed_and_restored_mtime", "aba"}:
            previous = kwargs["before_delete"]
            def before():
                previous()
                info = package.stat()
                original_bytes = package.read_bytes()
                package.write_bytes(b"X" + original_bytes[1:])
                if fault == "aba":
                    package.write_bytes(original_bytes)
                if fault in {"changed_and_restored_mtime", "aba"}:
                    os.utime(package, ns=(info.st_atime_ns, info.st_mtime_ns))
            kwargs["before_delete"] = before
        elif fault == "missing":
            second.rename(second.with_name("disconnected"))
        else:
            package.write_bytes(b"corrupt")
        return original(*args, **kwargs)
    monkeypatch.setattr(artifact_transfer, "release_copy", disturb)
    report = run(archive, storage_targets={"second": second})
    assert report["state"] == "failed" and report["released_bytes"] == 0
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data


def test_low_target_space_cannot_remove_local_audio(archive, monkeypatch):
    real = artifact_transfer.shutil.disk_usage
    monkeypatch.setattr(artifact_transfer.shutil, "disk_usage", lambda path:
        SimpleNamespace(free=0) if Path(path) == archive.target else real(path))
    configure(archive, allow_delete=True)
    assert run(archive)["state"] == "failed"
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data


def test_cancel_during_stream_copy_is_journaled_and_can_resume(archive, monkeypatch):
    configure(archive, allow_delete=True)
    copying = [False]
    original = artifact_transfer.create_artifact_package
    def copy(*args, **kwargs):
        copying[0] = True
        return original(*args, **kwargs)
    monkeypatch.setattr(artifact_transfer, "create_artifact_package", copy)
    def cancel():
        if copying[0]:
            raise ValueError("audio_retention_drained")
    report = run(archive, checkpoint=cancel)
    assert report["state"] == "failed" and "drained" in report["reason"]
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data
    assert not list((archive.target / "packages").glob(".artifact-package-stage-*"))
    ready(archive)
    assert run(archive)["released_bytes"] == len(archive.data)


def test_policy_disable_after_back_up_stops_release(archive, monkeypatch):
    configure(archive, allow_delete=True)
    original = artifact_transfer.create_artifact_package
    def disable(*args, **kwargs):
        result = original(*args, **kwargs)
        configure(archive, mode="off")
        return result
    monkeypatch.setattr(artifact_transfer, "create_artifact_package", disable)
    report = run(archive)
    assert report["state"] == "failed" and "policy_changed" in report["reason"]
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data


def _killed_retention(root, target, ready_event):
    original = artifact_transfer.release_copy
    def stop(*args, **kwargs):
        callback = kwargs["isolated"]
        def isolated():
            callback()
            ready_event.set()
            multiprocessing.Event().wait(50)
        kwargs["isolated"] = isolated
        return original(*args, **kwargs)
    artifact_transfer.release_copy = stop
    run_policy_once(ArtifactRoots.of(root), policy_id="audio", target_root=target, external_holds={})


def test_killed_maintenance_resumes_frozen_plan_and_verified_original_bytes(archive):
    configure(archive, allow_delete=True)
    before = domain_facts(archive)
    context = multiprocessing.get_context("spawn")
    event = context.Event()
    process = context.Process(target=_killed_retention, args=(archive.root, archive.target, event))
    process.start()
    try:
        assert event.wait(20)
    finally:
        process.kill()
        process.join(10)
    assert archive.data in [path.read_bytes() for path in (archive.root / "audio").iterdir() if path.is_file()]
    ready(archive)
    with sqlite3.connect(archive.root / "archive.db") as connection:
        frozen = connection.execute("SELECT plan_json FROM artifact_policy_runs").fetchone()[0]
    assert run(archive)["released_bytes"] == len(archive.data)
    with sqlite3.connect(archive.root / "archive.db") as connection:
        assert {row[0] for row in connection.execute("SELECT plan_json FROM artifact_policy_runs")} == {frozen}
    assert domain_facts(archive) == before


def test_multi_day_capacity_is_bounded_and_records_actual_costs(archive, monkeypatch):
    size = 65536
    clock = [2_000_000_000]
    monkeypatch.setattr(audio_retention.time, "time", lambda: clock[0])
    configure(archive, allow_delete=True, local_retention_seconds=100 * 86400, maximum_local_audio_bytes=2 * size)
    reports = []
    for day in range(1, 9):
        clock[0] += 86400
        content = random.Random(day).randbytes(size)
        digest = hashlib.sha256(content).hexdigest()
        key = f"audio/day-{day}.m4a"
        path = archive.root / key
        path.write_bytes(content)
        os.utime(path, (clock[0] - 1, clock[0] - 1))
        with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session, session.connection:
            session.connection.execute("INSERT INTO video_parts VALUES (?,'BVTEST',?,?,? ,1000,'discovered',?,?)", (day+1, day, day+1, key, clock[0], clock[0]))
            session.connection.execute("INSERT INTO audio_objects VALUES (?,?,?,'m4a',1000,?,?)", (day+1, digest, size, key, clock[0]))
            session.connection.execute("INSERT INTO part_audio_objects VALUES (?,?,?,'test')", (day+1, day+1, clock[0]))
            catalog = ArtifactCatalog(session.connection)
            catalog.register_object(digest, size)
            catalog.bind_audio(day+1, digest)
        ready(archive)
        before = domain_facts(archive)
        report = run(archive)
        assert report["state"] == "complete"
        assert sum(path.stat().st_size for path in (archive.root / "audio").glob("*.m4a")) <= 2 * size
        assert domain_facts(archive) == before
        reports.append(report)
    assert sum(report["released_bytes"] for report in reports) >= 6 * size
    assert all(report["io"]["bytes_read"] > 0 and report["compression"] for report in reports)
    assert max(report["target_temporary_space_upper_bound_bytes"] for report in reports) <= 4 * size + 64 * 1024**2
    evidence = [{key: report[key] for key in ("local_audio_bytes_before", "local_audio_bytes_after", "released_bytes",
        "target_temporary_space_upper_bound_bytes", "io", "compression")} for report in reports]
    (archive.root.parent / "eight-day-retention-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")


def test_compressed_audio_remains_readable_from_reference_and_complete_state_backups(archive, tmp_path):
    import zipfile

    from bili_asr.services.archive_snapshot import restore_snapshot, save_snapshot
    from bili_asr.services.reference_backup import (
        check_reference_backup,
        restore_reference_backup,
        save_reference_backup,
    )
    configure(archive, allow_delete=True)
    assert run(archive)["released_bytes"] == len(archive.data)
    before = domain_facts(archive)
    reference = tmp_path / "reference.zip"
    save_reference_backup(archive.roots, reference)
    assert check_reference_backup(reference, online=True, local_targets={"cold": archive.target})["external_verified"]
    with zipfile.ZipFile(reference) as bundle:
        assert all(info.compress_type == zipfile.ZIP_STORED for info in bundle.infolist())
    reference_root = tmp_path / "reference-restored"
    restore_reference_backup(reference, reference_root, source_workers_stopped=True)
    with sqlite3.connect(reference_root / "archive.db") as connection:
        config = json.loads(connection.execute("SELECT config_json FROM artifact_policies").fetchone()[0])
        assert config["version"] == 2 and config["allow_delete"] is True
        assert connection.execute("SELECT COUNT(*) FROM artifact_policy_runs").fetchone()[0] == 1
    complete = tmp_path / "complete.zip"
    save_snapshot(archive.root, complete, storage_targets={"cold": archive.target})
    complete_root = tmp_path / "complete-restored"
    restore_snapshot(complete, complete_root)
    assert hashlib.sha256((complete_root / "audio/input.m4a").read_bytes()).hexdigest() == archive.digest
    assert domain_facts(archive) == before


def test_public_cli_exports_audio_defaults_and_drains_mid_run(archive, capsys, tmp_path, monkeypatch):
    from tests.test_artifacts_cli import call
    assert call(archive.root, "policy-show", "--policy-id", "audio", "--default-version", "2") == 0
    assert json.loads(capsys.readouterr().out)["allow_delete"] is False
    configure(archive, allow_delete=True)
    drain = tmp_path / "drain"
    original = artifact_transfer.create_artifact_package
    def cancel(*args, **kwargs):
        drain.touch()
        return original(*args, **kwargs)
    monkeypatch.setattr(artifact_transfer, "create_artifact_package", cancel)
    assert call(archive.root, "policy-run", "--policy-id", "audio", "--target-root", archive.target,
        "--drain-file", drain, "--watch", "--no-external-holds") == 0
    assert json.loads(capsys.readouterr().out)["state"] == "drained"
    assert (archive.root / "audio/input.m4a").read_bytes() == archive.data
