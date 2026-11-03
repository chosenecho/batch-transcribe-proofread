"""Worker filtering, graceful drain and bounded owned-slot supervision."""

from __future__ import annotations

import os
import signal
import time
import threading
from pathlib import Path

import pytest

from bili_asr.cli import main
from bili_asr.storage import JobKind, WorkflowRepository, open_database
from bili_asr.workflow import WorkflowExecutor, attempt_checkpoint
from bili_asr.workflow_models import AsrPolicy, AsrProfile
from bili_asr.workflow_supervisor import WorkerSupervisor, _slot_lock
from test_workflow_control_plane import _seed_part


@pytest.fixture
def database(tmp_path):
    connection = open_database(tmp_path)
    _seed_part(connection)
    try:
        yield connection
    finally:
        connection.close()


def test_index_filter_is_rejected_before_claim(database, tmp_path, capsys):
    assert main(["workflow", "run", "--archive-root", str(tmp_path), "--kind", "index"]) == 1
    assert "registered handlers" in capsys.readouterr().err
    assert not database.execute("SELECT 1 FROM workflow_attempts").fetchone()


def test_roles_conflict_with_legacy_editorial_option(database, tmp_path, capsys):
    assert main(["workflow", "run", "--archive-root", str(tmp_path), "--role", "asr", "--only-editorial"]) == 1
    assert "choose only one" in capsys.readouterr().err


def test_preexisting_drain_file_claims_nothing(database, tmp_path):
    repository = WorkflowRepository(database)
    profile = repository.register_profile(AsrProfile("worker", "offline", device="cpu"))
    repository.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=profile)
    drain = tmp_path / "drain"
    drain.touch()
    assert main(["workflow", "run", "--archive-root", str(tmp_path), "--role", "cpu",
                 "--drain-file", str(drain)]) == 0
    assert not database.execute("SELECT 1 FROM workflow_attempts").fetchone()
    assert repository.count_by_status() == {"queued": 3}


def test_drain_after_current_task_finishes_leaves_next_job_queued(database):
    repository = WorkflowRepository(database)
    profile = repository.register_profile(AsrProfile("worker", "offline", device="cpu"))
    repository.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=profile)
    requested = False
    def handler(_job):
        nonlocal requested
        requested = True
        return {"outcome": "observed"}
    summary = WorkflowExecutor(repository, worker_id="cpu", handlers={JobKind.SUBTITLE: handler},
        kinds=(JobKind.SUBTITLE,), drain_requested=lambda: requested).run()
    assert summary.succeeded == 1 and summary.failed == 0
    assert repository.count_by_status() == {"queued": 2, "succeeded": 1}


def test_drain_deadline_raises_in_attempt_without_committing_a_result(database):
    repository = WorkflowRepository(database)
    profile = repository.register_profile(AsrProfile("worker", "offline", device="cpu"))
    repository.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=profile)
    requested = False
    def handler(job):
        nonlocal requested
        requested = True
        attempt_checkpoint(job)
        time.sleep(0.05)
        attempt_checkpoint(job)
        pytest.fail("deadline should prevent this result")
    summary = WorkflowExecutor(repository, worker_id="cpu", handlers={JobKind.SUBTITLE: handler},
        kinds=(JobKind.SUBTITLE,), drain_requested=lambda: requested, drain_timeout_seconds=0.01).run()
    assert summary.failed == 1
    row = database.execute("SELECT last_error_code,status FROM workflow_jobs WHERE kind='subtitle'").fetchone()
    assert tuple(row) == ("worker_drain_timeout", "failed")


def _wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("owned worker did not reach its expected process state")


def _owned_test_worker(options, role, worker_id):
    if os.name == "posix":
        os.setsid()
    ready = Path(options["archive_root"]) / worker_id
    ready.write_text(role)
    stopped = False
    def stop(_signal, _frame):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    while not stopped:
        time.sleep(0.02)
    ready.with_suffix(".drained").write_text("drained")


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal delivery and process-group cleanup")
def test_supervisor_owns_exact_slots_and_reaps_before_replacement(tmp_path):
    supervisor = WorkerSupervisor(archive_root=tmp_path, slots={"asr": 2, "cpu": 1},
                                  controller=_owned_test_worker, drain_timeout=2)
    try:
        supervisor.tick(now=10)
        _wait_for(lambda: len(list(tmp_path.glob("supervisor-*"))) == 3)
        pids = [slot.process.pid for slot in supervisor.slots]
        supervisor.tick(now=11)
        assert [slot.process.pid for slot in supervisor.slots] == pids
        victim = supervisor.slots[0].process
        victim.kill()
        victim.join(timeout=2)
        supervisor.tick(now=12)
        assert supervisor.slots[0].process is None
        supervisor.tick(now=12.49)
        assert supervisor.slots[0].process is None
        supervisor.tick(now=12.5)
        new_pid = supervisor.slots[0].process.pid
        assert new_pid != pids[0]
        assert [slot.process.pid for slot in supervisor.slots[1:]] == pids[1:]
        _wait_for(lambda: supervisor.slots[0].process.is_alive())
    finally:
        supervisor.close()
    assert all(slot.process is None for slot in supervisor.slots)
    for pid in pids[1:]:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert len(list(tmp_path.glob("*.drained"))) >= 2


def test_second_supervisor_lock_refuses_without_spawning_slots(tmp_path):
    with _slot_lock(tmp_path), pytest.raises(ValueError, match="already owns"):
        with _slot_lock(tmp_path):
            pytest.fail("second supervisor unexpectedly acquired ownership")


def test_supervisor_lock_refuses_symlink_and_preserves_external_bytes(tmp_path):
    external = tmp_path / "external"
    external.write_bytes(b"do not change")
    lock = tmp_path.parent / f".{tmp_path.name}.workflow-supervisor.lock"
    try:
        lock.symlink_to(external)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    with pytest.raises(ValueError, match="symlink"):
        with _slot_lock(tmp_path):
            pytest.fail("followed an external capability")
    assert external.read_bytes() == b"do not change"


@pytest.mark.skipif(os.name != "posix", reason="local WSL controller signal and drain acceptance")
def test_real_controller_polls_only_asr_and_drains_without_claiming_acquisition(database, tmp_path):
    repository = WorkflowRepository(database)
    profile = repository.register_profile(AsrProfile("worker", "offline", device="cpu"))
    repository.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=profile)
    drain = tmp_path / "drain-real-controller"
    supervisor = WorkerSupervisor(archive_root=tmp_path, slots={"asr": 1}, poll_interval=0.05,
                                  drain_file=str(drain), drain_timeout=2)
    errors = []
    def run():
        try:
            supervisor.run()
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        _wait_for(lambda: supervisor.slots[0].process is not None and supervisor.slots[0].process.is_alive())
        time.sleep(0.7)
        assert not database.execute("SELECT 1 FROM workflow_attempts").fetchone()
        assert supervisor.slots[0].process.is_alive()
        drain.touch()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not errors
        assert supervisor.slots[0].process is None
        assert repository.count_by_status() == {"queued": 3}
    finally:
        drain.touch()
        thread.join(timeout=5)


def test_supervisor_restart_budget_cannot_create_a_restart_storm(tmp_path):
    supervisor = WorkerSupervisor(archive_root=tmp_path, slots={"asr": 1},
                                  controller=_owned_test_worker, max_restarts=0, drain_timeout=0.1)
    try:
        supervisor.tick(now=1)
        supervisor.slots[0].process.kill()
        supervisor.slots[0].process.join(timeout=2)
        with pytest.raises(ValueError, match="restart budget exhausted"):
            supervisor.tick(now=2)
        assert supervisor.slots[0].process is None
    finally:
        supervisor.close()


@pytest.mark.parametrize("slots", [{"asr": -1}, {"asr": True}, {"asr": 0}, {"index": 1}])
def test_supervisor_rejects_invalid_slot_configuration(tmp_path, slots):
    with pytest.raises(ValueError):
        WorkerSupervisor(archive_root=tmp_path, slots=slots)
