"""A real worker composes current CLI commands outside the supervisor service."""
from __future__ import annotations

import socket
import subprocess
import sys
import time

import pytest

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.process_environment import worker_environment
from bili_asr.publication_worker import main as worker_main
from bili_asr.services import publication_supervisor as supervisor


@pytest.mark.parametrize("module", ["bili_asr.publication_worker", "bili_asr.services.publication_supervisor"])
def test_real_worker_keeps_current_cli_and_historical_module_entrypoint(module, tmp_path):
    archive = tmp_path / "archive"
    with ArchiveSession(archive, mode=ArchiveAccessMode.BOOTSTRAP):
        pass
    token = "a" * 32
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(15)
        with subprocess.Popen([
            sys.executable, "-m", module, str(listener.getsockname()[1]), token,
            "status", "--archive-root", str(archive),
        ], env=worker_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            with listener.accept()[0] as progress:
                greeting = b""
                while len(greeting) < 33:
                    data = progress.recv(33 - len(greeting))
                    assert data
                    greeting += data
                assert greeting == token.encode() + b"\n"
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr
            assert "archive.db:" in stdout or "videos:" in stdout
            assert "Traceback" not in stderr


def test_supervisor_executes_current_registered_cli_without_parent_writer_access(tmp_path, capfd):
    archive = tmp_path / "archive"
    with ArchiveSession(archive, mode=ArchiveAccessMode.BOOTSTRAP):
        pass
    assert supervisor.supervise_publication(["status", "--archive-root", str(archive)], timeout_seconds=15) == 0
    assert "Traceback" not in capfd.readouterr().err


def test_supervisor_stops_real_nonresponsive_worker_with_bounded_wait(monkeypatch, capfd):
    monkeypatch.setattr(supervisor, "_command", lambda *args: [sys.executable, "-c", "import time; time.sleep(60)"])
    start = time.monotonic()
    assert supervisor.supervise_publication([], timeout_seconds=0.15) == 1
    assert time.monotonic() - start < 4
    assert "deadline exceeded" in capfd.readouterr().err


@pytest.mark.parametrize("arguments", [[], ["42"], ["0", "a" * 32], ["1" * 5000, "a" * 32], ["42", "bad"]])
def test_worker_refuses_invalid_progress_arguments_before_connecting(arguments, monkeypatch, capsys):
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("connected before validating progress arguments"))
    assert worker_main(arguments) == 1
    assert "publication worker:" in capsys.readouterr().err


def test_worker_progress_binding_clears_after_application_failure():
    class Progress:
        def __init__(self):
            self.sent = []

        def sendall(self, value):
            self.sent.append(value)

    progress = Progress()
    with pytest.raises(RuntimeError, match="application failed"):
        with supervisor.publication_progress(progress):
            supervisor.publication_phase("candidate")
            supervisor.publication_phase("final")
            raise RuntimeError("application failed")
    supervisor.publication_phase("candidate")
    assert progress.sent == [b"C", b"F"]
