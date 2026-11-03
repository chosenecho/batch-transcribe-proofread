"""Both reservation ledgers share admission, lifetime and optional-schema rules."""
from __future__ import annotations

import multiprocessing
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace

import pytest

from bili_asr.archive_maintenance import ArchiveBusyError
from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_fences import resource_fence
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.services import asr_input_space, local_space
from bili_asr.services.artifact_coordination import reserve_local_space
from bili_asr.services.asr_input_space import reserve_input_space
from bili_asr.services.workflow_audio_input import verified_audio_input
from bili_asr.storage.artifact_online import install_online_in_staged_copy
from bili_asr.workflow_errors import JobExecutionError
from tests.test_artifact_transfer import archive as audio_archive

MIB = 1024 * 1024


def _free_space(monkeypatch):
    monkeypatch.setattr("shutil.disk_usage", lambda _: SimpleNamespace(free=200 * MIB))


@pytest.mark.parametrize("first", ["database", "private"])
@pytest.mark.parametrize("split_roots", [False, True])
def test_each_ledger_counts_the_other_before_admission(tmp_path, monkeypatch, first, split_roots):
    archive = audio_archive.__wrapped__(tmp_path)
    _free_space(monkeypatch)
    destination = tmp_path / "artifacts"
    destination.mkdir()
    roots = ArtifactRoots.of(archive.root, destination) if split_roots else archive.roots
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        connection = session.connection
        install_online_in_staged_copy(connection)
        existing = (reserve_local_space(connection, archive.roots, 120 * MIB, owner="download-decode")
                    if first == "database" else
                    reserve_input_space(archive.roots, archive.root, 120 * MIB, lambda: None))
        with existing:
            rejected = (reserve_input_space(roots, roots.write_base, 40 * MIB, lambda: None, connection=connection)
                        if first == "database" else
                        reserve_local_space(connection, roots, 40 * MIB, owner="restore", minimum_free_bytes=64 * MIB))
            expected = JobExecutionError if first == "database" else ValueError
            with pytest.raises(expected), rejected:
                pytest.fail("the two ledgers must not admit 160 MiB with only 200 MiB free and a 64 MiB floor")
            if first == "private":
                assert len(list((archive.root / ".asr-input-reservations").glob("*.json"))) == 1
            else:
                assert connection.execute(
                    "SELECT COUNT(*) FROM artifact_reservations WHERE released_at IS NULL").fetchone()[0] == 1
        with reserve_input_space(roots, roots.write_base, 40 * MIB, lambda: None, connection=connection):
            pass


def test_nested_private_reservation_keeps_its_live_same_thread_owner(tmp_path, monkeypatch):
    _free_space(monkeypatch)
    roots = ArtifactRoots.of(tmp_path)
    with reserve_input_space(roots, tmp_path, 120 * MIB, lambda: None):
        original, = (tmp_path / ".asr-input-reservations").glob("*.json")
        with (pytest.raises(JobExecutionError, match="audio_input_space_unavailable"),
              reserve_input_space(roots, tmp_path, 40 * MIB, lambda: None)):
            pytest.fail("a reentrant exclusive fence must not retire a live outer reservation")
        assert original.is_file()
    with reserve_input_space(roots, tmp_path, 40 * MIB, lambda: None):
        pass


@pytest.mark.parametrize("catalog", [False, True])
def test_missing_online_table_stays_a_private_reservation_without_schema_install(tmp_path, monkeypatch, catalog):
    _free_space(monkeypatch)
    if catalog:
        archive = audio_archive.__wrapped__(tmp_path)
        roots = archive.roots
        connection = sqlite3.connect(archive.root / "archive.db")
    else:
        roots = ArtifactRoots.of(tmp_path)
        connection = sqlite3.connect(":memory:")
    with connection:
        before = connection.execute("SELECT type,name,sql FROM sqlite_schema ORDER BY type,name").fetchall()
        with reserve_input_space(roots, roots.write_base, 40 * MIB, lambda: None, connection=connection):
            assert len(list((roots.write_base / ".asr-input-reservations").glob("*.json"))) == 1
        assert connection.execute("SELECT type,name,sql FROM sqlite_schema ORDER BY type,name").fetchall() == before
    connection.close()


def test_existing_private_receipt_is_counted_after_explicit_online_install(tmp_path, monkeypatch):
    archive = audio_archive.__wrapped__(tmp_path)
    _free_space(monkeypatch)
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        with reserve_input_space(archive.roots, archive.root, 120 * MIB, lambda: None, connection=session.connection):
            install_online_in_staged_copy(session.connection)
            with (pytest.raises(ValueError, match="unreserved"), reserve_local_space(
                session.connection, archive.roots, 40 * MIB, owner="installed-later", minimum_free_bytes=64 * MIB
            )):
                pytest.fail("installing the optional table must not hide an earlier private receipt")


def _crashed_owner(root, kind, ready, finish):
    # Real spawned owners must leave persistent evidence behind when killed.
    import shutil
    shutil.disk_usage = lambda _: SimpleNamespace(free=200 * MIB)
    roots = ArtifactRoots.of(root)
    if kind == "private":
        with reserve_input_space(roots, roots.write_base, 120 * MIB, lambda: None):
            (root / "audio/.workflow-audio-input-crashed.m4a").write_bytes(b"preserve crashed input")
            ready.set()
            finish.wait(30)
    else:
        with sqlite3.connect(root / "archive.db") as connection:
            with reserve_local_space(connection, roots, 120 * MIB, owner="killed-owner"):
                ready.set()
                finish.wait(30)


@pytest.mark.parametrize("kind", ["database", "private"])
def test_killed_kernel_owner_retires_only_its_reservation_evidence(tmp_path, monkeypatch, kind):
    archive = audio_archive.__wrapped__(tmp_path)
    _free_space(monkeypatch)
    context = multiprocessing.get_context("spawn")
    ready, finish = context.Event(), context.Event()
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        install_online_in_staged_copy(session.connection)
        process = context.Process(target=_crashed_owner, args=(archive.root, kind, ready, finish))
        process.start()
        try:
            assert ready.wait(15)

            def reserve_other():
                return (reserve_input_space(archive.roots, archive.root, 40 * MIB, lambda: None,
                                            connection=session.connection) if kind == "database" else
                        reserve_local_space(session.connection, archive.roots, 40 * MIB, owner="after-private",
                                            minimum_free_bytes=64 * MIB))

            with pytest.raises(JobExecutionError if kind == "database" else ValueError), reserve_other():
                pytest.fail("a live kernel owner must still charge its bytes")
            process.kill()
            process.join(10)
            assert not process.is_alive()
            with reserve_other():
                if kind == "database":
                    row = session.connection.execute(
                        "SELECT byte_count,owner,released_at FROM artifact_reservations WHERE owner='killed-owner'"
                    ).fetchone()
                    assert row[0] == 120 * MIB and row[1] == "killed-owner" and row[2] is not None
                else:
                    assert not list((archive.root / ".asr-input-reservations").glob("*.json"))
                    assert (archive.root / "audio/.workflow-audio-input-crashed.m4a").read_bytes() == b"preserve crashed input"
        finally:
            if process.is_alive():
                finish.set()
            process.join(10)
            if process.is_alive():
                process.kill()
                process.join(5)


def test_private_admission_waits_for_the_database_capacity_fence(tmp_path, monkeypatch):
    archive = audio_archive.__wrapped__(tmp_path)
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        install_online_in_staged_copy(session.connection)
    checking, admit_database, database_ready, finish, private_blocked = (Event() for _ in range(5))
    first_check = True

    def disk_usage(_):
        nonlocal first_check
        if first_check:
            first_check = False
            checking.set()
            assert admit_database.wait(10)
        return SimpleNamespace(free=200 * MIB)

    @contextmanager
    def observed_fence(roots, key, *, exclusive):
        try:
            with resource_fence(roots, key, exclusive=exclusive):
                yield
        except ArchiveBusyError:
            if key == "capacity":
                private_blocked.set()
            raise

    monkeypatch.setattr("shutil.disk_usage", disk_usage)
    monkeypatch.setattr(asr_input_space, "resource_fence", observed_fence)

    def own_database():
        with sqlite3.connect(archive.root / "archive.db") as connection:
            with reserve_local_space(connection, archive.roots, 120 * MIB, owner="publishing-reservation"):
                database_ready.set()
                assert finish.wait(10)

    def try_private():
        with sqlite3.connect(archive.root / "archive.db") as connection:
            with reserve_input_space(archive.roots, archive.root, 40 * MIB, lambda: None, connection=connection):
                pytest.fail("the completed database admission must be counted before private admission")

    with ThreadPoolExecutor(max_workers=2) as executor:
        database_owner = executor.submit(own_database)
        private = None
        try:
            assert checking.wait(10)
            private = executor.submit(try_private)
            assert private_blocked.wait(10)
            admit_database.set()
            assert database_ready.wait(10)
            with pytest.raises(JobExecutionError, match="audio_input_space_unavailable"):
                private.result(timeout=10)
        finally:
            admit_database.set()
            finish.set()
            database_owner.result(timeout=10)
            if private is not None and not private.done():
                private.result(timeout=10)


def test_fixed_workflow_input_passes_its_database_connection_to_capacity_admission(tmp_path, monkeypatch):
    archive = audio_archive.__wrapped__(tmp_path)
    _free_space(monkeypatch)
    result = {"storage_key": "audio/input.m4a", "sha256": archive.digest, "byte_size": len(archive.data)}
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        install_online_in_staged_copy(session.connection)
        with reserve_local_space(session.connection, archive.roots, 136 * MIB, owner="all-remaining-headroom"):
            with (pytest.raises(JobExecutionError, match="audio_input_space_unavailable") as error,
                  verified_audio_input(session.connection, archive.roots, 1, result, retained=None, checkpoint=lambda: None)):
                pytest.fail("the production caller must count the database ledger before copying its input")
            assert error.value.safe_details["reserved_byte_size"] == 136 * MIB
            assert not list((archive.root / "audio").glob(".workflow-audio-input-*.m4a"))
        with verified_audio_input(session.connection, archive.roots, 1, result, retained=None, checkpoint=lambda: None) as fixed:
            assert fixed.path.read_bytes() == archive.data


@pytest.mark.parametrize("ledger", ["database", "private"])
def test_reservation_scan_limit_fails_instead_of_billing_a_truncated_ledger(tmp_path, monkeypatch, ledger):
    archive = audio_archive.__wrapped__(tmp_path)
    _free_space(monkeypatch)
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        install_online_in_staged_copy(session.connection)
        first = (reserve_local_space(session.connection, archive.roots, MIB, owner="one") if ledger == "database" else
                 reserve_input_space(archive.roots, archive.root, MIB, lambda: None))
        second = (reserve_local_space(session.connection, archive.roots, MIB, owner="two") if ledger == "database" else
                  reserve_input_space(archive.roots, archive.root, MIB, lambda: None))
        with first, second:
            monkeypatch.setattr(local_space, "MAX_LOCAL_RESERVATIONS", 1)
            with (pytest.raises(JobExecutionError, match="audio_input_space_unavailable") as error,
                  reserve_input_space(archive.roots, archive.root, MIB, lambda: None, connection=session.connection)):
                pytest.fail("a bounded scan must reject additional uncounted owners")
            assert error.value.safe_details["reason"] == "reservation-scan-limit"
            with (pytest.raises(ValueError, match="reservation-scan-limit"),
                  reserve_local_space(session.connection, archive.roots, MIB, owner="truncated")):
                pytest.fail("database admission must apply the same bounded scan")
