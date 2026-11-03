"""Lease recovery and real signal shutdown preserve exact attempt outcomes."""
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time

import pytest

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.services.workflow_application import WorkflowApplication
from bili_asr.storage import WorkflowRepository, open_database
from bili_asr.workflow import LeaseRenewalFailed, WorkerDrainTimeout, WorkflowExecutor, _LeaseHeartbeat, attempt_checkpoint
from bili_asr.workflow_models import AsrPolicy, AsrProfile, JobKind
from bili_asr.workflow_shutdown import WorkerShutdown
from bili_asr.workflow_supervisor import WorkerSupervisor
from tests.test_workflow_control_plane import _seed_part


def _seed(root):
    connection = open_database(root)
    _seed_part(connection)
    repository = WorkflowRepository(connection)
    profile = repository.register_profile(AsrProfile("lifecycle", "offline", device="cpu"))
    repository.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=profile)
    return connection, repository


def _wait(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("worker lifecycle condition was not observed")


def test_heartbeat_retries_open_failure_then_renews_and_closes(tmp_path, monkeypatch):
    connection, repository = _seed(tmp_path)
    job = repository.claim("worker", kinds=(JobKind.SUBTITLE,))
    opened, renewed, closed = [], threading.Event(), threading.Event()
    class Lease:
        def renew_lease(self, *_args, **_kwargs):
            renewed.set()
        def close(self):
            closed.set()
    def open_lease():
        opened.append(1)
        if len(opened) == 1:
            raise sqlite3.OperationalError("transient open failure")
        return Lease()
    monkeypatch.setattr(repository, "open_lease_repository", open_lease)
    heartbeat = _LeaseHeartbeat(repository, job, lease_seconds=2, interval_seconds=0.01)
    try:
        heartbeat.start()
        assert renewed.wait(2)
        heartbeat.check_health()
        assert len(opened) == 2
    finally:
        heartbeat.stop()
        connection.close()
    assert closed.is_set()


@pytest.mark.parametrize("stage", ["open", "renew", "contract"])
def test_persistent_heartbeat_failure_stops_cooperative_attempt_before_expiry(tmp_path, monkeypatch, stage):
    connection, repository = _seed(tmp_path)
    class Lease:
        def renew_lease(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("renew failed")
        def close(self):
            pass
    def open_lease():
        if stage == "open":
            raise sqlite3.OperationalError("open failed")
        if stage == "contract":
            raise ValueError("unsupported contract")
        return Lease()
    monkeypatch.setattr(repository, "open_lease_repository", open_lease)
    stopped = []
    def handler(job):
        until = time.monotonic() + 4
        while time.monotonic() < until:
            try:
                attempt_checkpoint(job)
            except LeaseRenewalFailed:
                stopped.append(True)
                raise
            time.sleep(0.01)
        pytest.fail("unhealthy heartbeat allowed the task to continue")
    try:
        summary = WorkflowExecutor(repository, worker_id="worker", lease_seconds=4,
            heartbeat_interval_seconds=0.01, kinds=(JobKind.SUBTITLE,),
            handlers={JobKind.SUBTITLE: handler}).run(limit=1)
        assert summary.failed == 1
        assert stopped == [True] or stage == "contract"  # Fatal startup can fence dispatch itself.
        row = connection.execute("SELECT status,last_error_code,lease_owner,lease_expires_at FROM workflow_jobs WHERE kind='subtitle'").fetchone()
        assert tuple(row) == ("failed", "lease_renewal_failed", None, None)
        attempt = connection.execute("SELECT outcome,error_code FROM workflow_attempts").fetchone()
        assert tuple(attempt) == ("failed", "lease_renewal_failed")
    finally:
        connection.close()


def test_unhealthy_heartbeat_rejects_noncooperative_late_result(tmp_path, monkeypatch):
    connection, repository = _seed(tmp_path)
    def fatal():
        raise ValueError("invalid schema")
    monkeypatch.setattr(repository, "open_lease_repository", fatal)
    def handler(_job):
        time.sleep(0.1)
        return {"must_not_commit": True}
    try:
        summary = WorkflowExecutor(repository, worker_id="worker", kinds=(JobKind.SUBTITLE,),
            handlers={JobKind.SUBTITLE: handler}).run(limit=1)
        assert summary.failed == 1 and summary.succeeded == 0
        row = connection.execute("SELECT outcome,error_code,result_json FROM workflow_attempts").fetchone()
        assert tuple(row) == ("failed", "lease_renewal_failed", "{}")
    finally:
        connection.close()


def _application_worker(options, role, worker_id):
    """Real application/executor/DB with an offline blocking provider handler."""
    from bili_asr.workflow_runtime import ArchiveWorkflowHandlers
    if os.name == "posix":
        os.setsid()
    root = Path(options["archive_root"])
    def handler(job):
        (root / "running").write_text(job.job_id)
        if role == "short":
            while not (root / "release").exists():
                time.sleep(0.01)
        else:
            time.sleep(30)
        return {"completed": True}
    ArchiveWorkflowHandlers.subtitle = lambda self, job: handler(job)
    with ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session:
        summary = WorkflowApplication(session).run(worker_id=worker_id, sessdata=None,
            kinds=(JobKind.SUBTITLE,), drain_file=options.get("drain_file"),
            drain_timeout_seconds=options["drain_timeout"], gpu_session="legacy")
        (root / "summary").write_text(json.dumps(summary.__dict__))


@pytest.mark.skipif(os.name != "posix", reason="real POSIX signal interruption")
@pytest.mark.parametrize("trigger", ["signal", "file", "repeat"])
def test_application_stops_blocking_handler_and_persists_drain_failure(tmp_path, trigger):
    connection, _ = _seed(tmp_path)
    drain = tmp_path / "drain"
    options = {"archive_root": str(tmp_path), "drain_file": str(drain),
               "drain_timeout": 10 if trigger == "repeat" else 0.15}
    process = multiprocessing.get_context("spawn").Process(target=_application_worker,
        args=(options, "long", "worker"))
    process.start()
    try:
        _wait(lambda: (tmp_path / "running").exists())
        if trigger == "file":
            drain.touch()
        else:
            os.kill(process.pid, signal.SIGTERM)
            if trigger == "repeat":
                time.sleep(0.05)
                os.kill(process.pid, signal.SIGINT)
        process.join(timeout=5)
        assert not process.is_alive() and process.exitcode == 0
        summary = json.loads((tmp_path / "summary").read_text())
        assert summary["failed"] == 1 and summary["succeeded"] == 0
        row = connection.execute("SELECT status,last_error_code,lease_owner,lease_expires_at FROM workflow_jobs WHERE kind='subtitle'").fetchone()
        assert tuple(row) == ("failed", "worker_drain_timeout", None, None)
        assert connection.execute("SELECT COUNT(*) FROM workflow_attempts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM workflow_jobs WHERE status='queued'").fetchone()[0] == 2
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=2)
        process.close()
        connection.close()


@pytest.mark.skipif(os.name != "posix", reason="real POSIX drain ordering")
def test_supervisor_allows_terminal_write_after_child_drain_deadline(tmp_path):
    connection, _ = _seed(tmp_path)
    supervisor = WorkerSupervisor(archive_root=tmp_path, slots={"cpu": 1},
        controller=_application_worker, drain_timeout=0.15, gpu_session="oneshot")
    try:
        supervisor.tick()
        _wait(lambda: (tmp_path / "running").exists())
        pid = supervisor.slots[0].process.pid
        supervisor.close()
        assert json.loads((tmp_path / "summary").read_text())["failed"] == 1
        row = connection.execute("SELECT status,last_error_code,lease_owner,lease_expires_at FROM workflow_jobs WHERE kind='subtitle'").fetchone()
        assert tuple(row) == ("failed", "worker_drain_timeout", None, None)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        supervisor.close()
        connection.close()


@pytest.mark.skipif(os.name != "posix", reason="real POSIX graceful completion")
def test_short_inflight_task_commits_after_first_signal_without_claiming_next(tmp_path):
    connection, _ = _seed(tmp_path)
    process = multiprocessing.get_context("spawn").Process(target=_application_worker,
        args=({"archive_root": str(tmp_path), "drain_timeout": 5}, "short", "worker"))
    process.start()
    try:
        _wait(lambda: (tmp_path / "running").exists())
        os.kill(process.pid, signal.SIGTERM)
        (tmp_path / "release").touch()
        process.join(timeout=5)
        assert not process.is_alive() and process.exitcode == 0
        summary = json.loads((tmp_path / "summary").read_text())
        assert summary["succeeded"] == 1 and summary["failed"] == 0
        assert connection.execute("SELECT COUNT(*) FROM workflow_attempts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM workflow_jobs WHERE status='queued'").fetchone()[0] == 2
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=2)
        process.close()
        connection.close()


def test_shutdown_restores_handlers_and_existing_alarm(tmp_path):
    signals = (signal.SIGINT, signal.SIGTERM) + ((signal.SIGALRM,) if os.name == "posix" else ())
    original = {signum: signal.getsignal(signum) for signum in signals}
    stop = threading.Event()
    shutdown = WorkerShutdown(stop, drain_file=None, timeout=1)
    with shutdown.install():
        assert not shutdown.requested()
        stop.set()
        assert shutdown.requested()
    assert {signum: signal.getsignal(signum) for signum in signals} == original


@pytest.mark.skipif(os.name != "posix", reason="POSIX application-owned alarm")
def test_shutdown_does_not_steal_application_alarm():
    handler = lambda *_args: None
    old = signal.signal(signal.SIGALRM, handler)
    try:
        with WorkerShutdown(threading.Event(), drain_file=None, timeout=1).install():
            assert signal.getsignal(signal.SIGALRM) is handler
            assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    finally:
        signal.signal(signal.SIGALRM, old)


@pytest.mark.skipif(os.name != "posix", reason="watchdog must not interrupt cooperative cleanup")
def test_cooperative_timeout_disarms_watchdog_during_slow_cleanup():
    stop = threading.Event()
    shutdown = WorkerShutdown(stop, drain_file=None, timeout=0.01)
    cleaned = []
    with shutdown.install():
        with pytest.raises(WorkerDrainTimeout):
            with shutdown.attempt():
                stop.set()
                shutdown.requested()
                # Trigger synchronously before the watchdog's first 50ms tick.
                shutdown.started -= 1
                try:
                    shutdown.checkpoint()
                finally:
                    time.sleep(0.15)
                    cleaned.append(True)
    assert cleaned == [True]
    assert not shutdown._interrupt_sent


def test_heartbeat_connection_bounds_lock_wait_and_preserves_lower_setting(tmp_path):
    connection, repository = _seed(tmp_path)
    try:
        for configured, expected in ((30000, 1000), (75, 75)):
            connection.execute(f"PRAGMA busy_timeout={configured}")
            lease = WorkflowRepository(connection).open_lease_repository()
            try:
                assert lease.connection.execute("PRAGMA busy_timeout").fetchone()[0] == expected
            finally:
                lease.close()
    finally:
        connection.close()


def test_nonmain_shutdown_uses_cooperative_deadline_without_mutating_signals():
    original = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    observed, errors = [], []
    def worker():
        try:
            stop = threading.Event()
            shutdown = WorkerShutdown(stop, drain_file=None, timeout=0.01)
            with shutdown.install():
                with pytest.raises(WorkerDrainTimeout):
                    with shutdown.attempt():
                        stop.set()
                        shutdown.requested()
                        shutdown.started -= 1
                        shutdown.checkpoint()
                observed.append(True)
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=3)
    assert not thread.is_alive() and not errors and observed == [True]
    assert {signum: signal.getsignal(signum) for signum in original} == original
