"""Real spawn-process faults without a GPU or model downloads."""

from __future__ import annotations

import multiprocessing
import errno
import os
import queue
import signal
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from bili_asr.asr.config import ASRConfig
from bili_asr.asr.runner import ASRInferenceTimeoutError
from bili_asr.asr.session import AsrInferenceSession, InferenceRequest, InferenceSessionError


class ProcessRunner:
    def __init__(self, config):
        if config.model_name == "startup-error":
            raise ValueError("sensitive /private/model/path token-value")
        self.config = config
        self.calls = 0
        self.paired = None

    def set_hotword_evidence(self, *, evidence_text, paired_subtitle_text):
        self.paired = paired_subtitle_text

    def transcribe(self, path, *, bust_cache=False):
        self.calls += 1
        from bili_asr.asr.runner import _progress
        if path in {"progressing", "progress-then-hang", "heartbeats"}:
            _progress("model_ready")
            for _ in range(8):
                time.sleep(.1)
                _progress("decode" if path == "heartbeats" else "chunk_complete")
                if path == "progress-then-hang":
                    time.sleep(20)
        if path == "os-error":
            _progress("decode")
            raise OSError(errno.EMFILE, "sensitive .env SESSDATA password")
        if path == "sqlite-error":
            import sqlite3
            _progress("align")
            sqlite3.connect("file:/absent-sensitive-secret-parent/archive.db?mode=rw", uri=True)
        if path == "hang":
            time.sleep(20)
        if path == "error":
            raise ValueError("sensitive /model/path signed-url?secret=value")
        if path == "crash":
            os._exit(9)
        if path.startswith("orphan:"):
            sleeper = subprocess.Popen(["sleep", "30"])
            Path(path.removeprefix("orphan:")).write_text(str(sleeper.pid))
            os._exit(9)
        if path.startswith("holdgilchild:"):
            import ctypes
            sleeper = subprocess.Popen(["sleep", "30"])
            Path(path.removeprefix("holdgilchild:")).write_text(str(sleeper.pid))
            ctypes.PyDLL(None).sleep(20)
        if path == "large":
            return [{"start": 0, "end": 1, "text": "x" * 1024}] * 4096
        return [{"start": 0, "end": 1, "text": path + ":" + str(self.paired)}]

    def rebuild_hotwords_from_first_pass(self, _text):
        return []

    def provenance(self):
        return {"language": "Chinese", "pid": str(os.getpid())}

    def transcribed_coverage(self):
        return {"calls": self.calls}

    def diagnostics(self):
        return {"calls": self.calls, "quality": {"status": "not-evaluable"}}

    def release(self):
        pass


def request(attempt=1, *, binding=None):
    return InferenceRequest("job-" + str(attempt), "worker", attempt, "f" * 64, binding or {})


def invoke(session, path="first", *, config=None, attempt=1, timeout=8, checkpoint=lambda: None, binding=None):
    diagnostics = {}
    result = session.transcribe(config or ASRConfig(model_name="offline", device="cpu"), path, request=request(attempt, binding=binding),
        paired_subtitle_text=str(attempt), timeout_seconds=timeout, checkpoint=checkpoint,
        diagnostics_sink=diagnostics)
    return result, diagnostics


def test_real_spawn_reuses_models_with_independent_output_and_request_identity():
    session = AsrInferenceSession(runner_factory=ProcessRunner)
    try:
        first, first_evidence = invoke(session)
        second, second_evidence = invoke(session, "second", attempt=2)
        assert first[0][0]["text"] == "first:1"
        assert second[0][0]["text"] == "second:2"
        assert first[1]["pid"] == second[1]["pid"]
        assert first_evidence["calls"] == 1 and second_evidence["calls"] == 2
        assert first_evidence["session"]["reused"] is False
        assert second_evidence["session"]["reused"] is True
        assert first_evidence["session"]["request_id"] != second_evidence["session"]["request_id"]
        assert second_evidence["session"]["identity"]["attempt_count"] == 2
        assert second_evidence["session"]["parent_clock"]["process_id"] == os.getpid()
        assert second_evidence["session"]["configuration_key"] == first_evidence["session"]["configuration_key"]
        assert second_evidence["session"]["parent_clock"]["domain_id"] != first_evidence["session"]["parent_clock"]["domain_id"]
    finally:
        session.close()
    assert session._process is None


@pytest.mark.parametrize("change", ["model", "binding", "revision", "batch", "compile", "attention"])
def test_full_configuration_and_runtime_identity_rebuild_session(change):
    session = AsrInferenceSession(runner_factory=ProcessRunner)
    try:
        first, _ = invoke(session)
        config = ASRConfig(model_name="offline", device="cpu")
        binding = None
        if change == "model":
            config = replace(config, model_name="other-model")
        elif change == "revision":
            config = replace(config, aligner_revision="other-revision")
        elif change == "batch":
            config = replace(config, asr_batch_size=2)
        elif change == "compile":
            config = replace(config, asr_cache_implementation="static", asr_compile=True)
        elif change == "attention":
            config = replace(config, aligner_attention="sdpa")
        else:
            binding = {"manifest_sha256": "a" * 64}
        second, evidence = invoke(session, attempt=2, config=config, binding=binding)
        assert first[1]["pid"] != second[1]["pid"]
        assert evidence["session"]["rebuild_reason"] == "configuration_changed"
        assert evidence["calls"] == 1
    finally:
        session.close()


@pytest.mark.parametrize("path,error", [("hang", ASRInferenceTimeoutError),
                                       ("error", InferenceSessionError), ("crash", InferenceSessionError)])
def test_timeout_error_crash_reap_and_next_task_rebuild(path, error):
    session = AsrInferenceSession(runner_factory=ProcessRunner)
    try:
        first, _ = invoke(session)
        with pytest.raises(error) as caught:
            invoke(session, path, timeout=0.5 if path == "hang" else 8)
        assert "sensitive" not in str(caught.value)
        assert session._process is None
        recovered, evidence = invoke(session, "recovered", attempt=2)
        assert recovered[1]["pid"] != first[1]["pid"]
        assert evidence["calls"] == 1
    finally:
        session.close()


def test_cancellation_during_inflight_request_discards_result_and_reaps_process():
    session = AsrInferenceSession(runner_factory=ProcessRunner)
    try:
        invoke(session)
        pid = session._process.pid
        deadline = time.monotonic() + 0.2
        def cancelled():
            if time.monotonic() >= deadline:
                raise RuntimeError("attempt revoked")
        with pytest.raises(RuntimeError, match="attempt revoked"):
            invoke(session, "hang", checkpoint=cancelled)
        assert session._process is None
        if os.name == "posix":
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        result, evidence = invoke(session, "new-attempt", attempt=2)
        assert result[0][0]["text"] == "new-attempt:2"
        assert evidence["calls"] == 1
    finally:
        session.close()


def test_real_progress_keeps_long_work_alive_but_stalled_work_and_heartbeats_are_reaped():
    config = ASRConfig(model_name="offline", device="cpu", inference_timeout_policy="audio-scaled",
        inference_timeout_seconds=6, inference_startup_seconds=5, inference_hard_cap_seconds=10,
        inference_no_progress_seconds=.35)
    session = AsrInferenceSession(runner_factory=ProcessRunner)
    try:
        result, _ = invoke(session, "progressing", config=config, timeout=6)
        assert result[0][0]["text"].startswith("progressing")
        with pytest.raises(ASRInferenceTimeoutError) as caught:
            invoke(session, "progressing", config=config, timeout=.45)
        assert caught.value.safe_details["reason"] == "total_deadline"
        assert session._process is None
        for path in ("progress-then-hang", "heartbeats"):
            with pytest.raises(ASRInferenceTimeoutError) as caught:
                invoke(session, path, config=config, timeout=6)
            assert caught.value.safe_details["reason"] == "no_progress"
            assert session._process is None
    finally:
        session.close()


@pytest.mark.parametrize("reason,expires", [("startup_deadline", 5), ("no_progress", 3)])
@pytest.mark.parametrize("frame", ["progress", "result"])
@pytest.mark.parametrize("delay_at", ["queue", "checkpoint"])
@pytest.mark.parametrize("offset", [-.001, 0, .001], ids=["before", "at", "after"])
def test_receive_checks_watchdog_boundary(monkeypatch, reason, expires, frame, delay_at, offset):
    from bili_asr.asr import session as session_module
    from bili_asr.asr.deadlines import ProgressWatchdog

    now = [0.0]
    monkeypatch.setattr(session_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    watchdog = ProgressWatchdog(started=0, total_seconds=100, startup_seconds=5, no_progress_seconds=3)
    sequence = 1
    if reason == "no_progress":
        assert watchdog.observe({"phase": "model_ready", "sequence": sequence}, 0)
        sequence += 1
    envelope = {"request_id": "current-request"}
    responses = iter([
        envelope | ({"progress": {"phase": "decoded", "sequence": sequence}}
                    if frame == "progress" else {"ok": True}),
        envelope | {"ok": True},
    ])
    received = False

    def get(**_kwargs):
        nonlocal received
        received = True
        if delay_at == "queue":
            now[0] = expires + offset
        return next(responses)

    def checkpoint():
        if received and delay_at == "checkpoint":
            now[0] = expires + offset

    session = AsrInferenceSession(runner_factory=ProcessRunner)
    session._responses = SimpleNamespace(get=get)
    if offset < 0:
        assert session._receive(envelope, 100, checkpoint, watchdog) == envelope | {"ok": True}
        assert watchdog.progress_count == sequence - 1 + (frame == "progress")
        return
    with pytest.raises(ASRInferenceTimeoutError) as caught:
        session._receive(envelope, 100, checkpoint, watchdog)
    assert caught.value.safe_details["reason"] == reason
    assert watchdog.progress_count == sequence - 1


@pytest.mark.parametrize("reason,expires", [("startup_deadline", 5), ("no_progress", 3)])
def test_late_result_reaps_real_session_and_next_request_rebuilds(monkeypatch, reason, expires):
    from bili_asr.asr import session as session_module

    config = ASRConfig(model_name="offline", device="cpu", inference_timeout_policy="audio-scaled",
        inference_timeout_seconds=10, inference_startup_seconds=5, inference_hard_cap_seconds=20,
        inference_no_progress_seconds=3)
    session = AsrInferenceSession(runner_factory=ProcessRunner)
    try:
        first, _ = invoke(session, config=config, timeout=10)
        original = session._responses.get
        completed = None
        now = [0.0]

        def delayed_result(*args, **kwargs):
            nonlocal completed
            if completed is None:
                completed = original(*args, **kwargs)
                if reason == "no_progress":
                    return {key: completed[key] for key in ("protocol", "generation", "request_id", "identity")} | {
                        "progress": {"phase": "model_ready", "sequence": 1}}
            now[0] = expires
            return completed

        with monkeypatch.context() as patch:
            patch.setattr(session_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
            patch.setattr(session._responses, "get", delayed_result)
            with pytest.raises(ASRInferenceTimeoutError) as caught:
                invoke(session, "must-not-be-returned", config=config, attempt=2, timeout=10)
            assert caught.value.safe_details["reason"] == reason
            assert session._process is None
        recovered, evidence = invoke(session, "new-generation", config=config, attempt=3, timeout=10)
        assert recovered[0][0]["text"] == "new-generation:3"
        assert recovered[1]["pid"] != first[1]["pid"]
        assert evidence["calls"] == 1
    finally:
        session.close()


@pytest.fixture
def worker_protocol(monkeypatch):
    """Exercise the worker protocol without starting its process guardians here."""
    from bili_asr.asr import session as session_module
    from bili_asr.asr.progress import set_progress_hook

    monkeypatch.setattr(session_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(session_module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(session_module, "threading", SimpleNamespace(
        Thread=lambda **_: SimpleNamespace(start=lambda: None)))

    def exchange(runner, *paths):
        commands, responses = queue.Queue(), queue.Queue()
        envelopes = []
        for attempt, path in enumerate(paths, 1):
            envelope = {"protocol": 1, "generation": 7, "request_id": f"request-{attempt}",
                        "identity": request(attempt).identity()}
            envelopes.append(envelope)
            commands.put({**envelope, "audio_path": path, "paired_subtitle_text": str(attempt)})
        commands.put(None)
        session_module._session_worker(ASRConfig(model_name="offline", device="cpu"),
            commands, responses, 7, lambda _config: runner, False, 1024)
        result = []
        while not responses.empty():
            result.append(responses.get_nowait())
        return envelopes, result

    try:
        yield exchange
    finally:
        set_progress_hook(None)


def test_worker_emits_only_real_progress_with_request_identity_and_resets_sequence(worker_protocol):
    from bili_asr.asr.progress import _progress

    class StagedRunner(ProcessRunner):
        released = 0

        def transcribe(self, path, *, bust_cache=False):
            for phase in ("load", "model_ready", "audio_ready", "decode", "decoded",
                          "align", "aligned", "chunk_complete", "heartbeat"):
                _progress(phase)
            return super().transcribe(path, bust_cache=bust_cache)

        def release(self):
            self.released += 1

    runner = StagedRunner(ASRConfig(model_name="offline", device="cpu"))
    envelopes, responses = worker_protocol(runner, "first", "second")
    phases = ["model_ready", "audio_ready", "decoded", "aligned", "chunk_complete"]
    for attempt, envelope in enumerate(envelopes, 1):
        frames = [frame for frame in responses if frame["request_id"] == envelope["request_id"]]
        assert frames[:-1] == [{**envelope, "progress": {"phase": phase, "sequence": sequence}}
                              for sequence, phase in enumerate(phases, 1)]
        assert frames[-1]["ok"] is True
        assert all(frames[-1][key] == value for key, value in envelope.items())
        assert frames[-1]["segments"][0]["text"] == f"{'first' if attempt == 1 else 'second'}:{attempt}"
        assert frames[-1]["coverage"] == {"calls": attempt}
    assert runner.released == 1


@pytest.mark.parametrize("phase,expected", [
    ("load", "model_load"), ("model_ready", "audio_prepare"), ("audio_ready", "decode"),
    ("decode", "decode"), ("decoded", "align"), ("align", "align"),
    ("aligned", "align"), ("chunk_complete", "decode"),
    ("sensitive /private/path SESSDATA", "inference"),
])
def test_worker_failure_keeps_safe_stage_and_system_facts_then_discards_runner(worker_protocol, phase, expected):
    from bili_asr.asr.progress import _progress

    class FailedRunner(ProcessRunner):
        released = 0

        def transcribe(self, _path, *, bust_cache=False):
            self.calls += 1
            _progress(phase)
            raise OSError(errno.EMFILE, "sensitive /private/model token=value")

        def release(self):
            self.released += 1

    runner = FailedRunner(ASRConfig(model_name="offline", device="cpu"))
    envelopes, responses = worker_protocol(runner, "failure", "must-not-run")
    failure = responses[-1]
    assert all(failure[key] == value for key, value in envelopes[0].items())
    assert failure["ok"] is False
    assert failure["diagnostic"] == {"phase": expected, "exception_type": "OSError",
        "errno": errno.EMFILE, "errno_name": "EMFILE"}
    assert "sensitive" not in repr(responses)
    assert all(frame["request_id"] == envelopes[0]["request_id"] for frame in responses)
    assert runner.calls == 1 and runner.released == 1


@pytest.mark.parametrize("events", [
    pytest.param([{"phase": "chunk_complete", "sequence": 1},
                  {"phase": "chunk_complete", "sequence": 1}], id="repeated-sequence"),
    pytest.param([{"phase": "chunk_complete", "sequence": 2}], id="skipped-sequence"),
    pytest.param([{"phase": "heartbeat", "sequence": 1}], id="heartbeat"),
    pytest.param([{"phase": "decoded", "sequence": True}], id="boolean-sequence"),
    pytest.param([{"phase": [], "sequence": 1}], id="malformed-phase"),
])
def test_invalid_progress_frame_reaps_session_and_cannot_return_a_completed_result(monkeypatch, events):
    session = AsrInferenceSession(runner_factory=ProcessRunner)
    try:
        first, _ = invoke(session)
        original = session._responses.get
        pending = iter(events)
        completed = None

        def invalid_progress(*args, **kwargs):
            nonlocal completed
            if completed is None:
                completed = original(*args, **kwargs)
            return {key: completed[key] for key in ("protocol", "generation", "request_id", "identity")} | {
                "progress": next(pending)}

        monkeypatch.setattr(session._responses, "get", invalid_progress)
        with pytest.raises(InferenceSessionError, match="invalid ASR progress response") as caught:
            invoke(session, "must-not-be-returned", attempt=2)
        assert caught.value.safe_details == {"phase": "ipc"}
        assert session._process is None
        recovered, evidence = invoke(session, "new-generation", attempt=3)
        assert recovered[0][0]["text"] == "new-generation:3"
        assert recovered[1]["pid"] != first[1]["pid"]
        assert evidence["calls"] == 1
    finally:
        session.close()


@pytest.mark.parametrize("path,expected", [("os-error", {"errno": errno.EMFILE, "errno_name": "EMFILE", "phase": "decode"}),
    ("sqlite-error", {"sqlite_errorcode": 14, "sqlite_errorname": "SQLITE_CANTOPEN", "phase": "align"}),
    ("crash", {"exit_code": 9, "reason": "child_exited", "phase": "ipc"})])
def test_spawn_failures_keep_safe_system_facts_without_exception_text(path, expected):
    session = AsrInferenceSession(runner_factory=ProcessRunner)
    try:
        with pytest.raises(InferenceSessionError) as caught:
            invoke(session, path)
        assert expected.items() <= caught.value.safe_details.items()
        assert "sensitive" not in repr(caught.value.safe_details) + str(caught.value)
        assert session._process is None
    finally:
        session.close()


def test_legacy_one_shot_uses_the_same_filtered_process_protocol(monkeypatch):
    from bili_asr.asr import session as session_module
    from bili_asr.asr.session import transcribe_with_timeout
    created = []
    def factory(*, persistent):
        session = AsrInferenceSession(persistent=persistent, runner_factory=ProcessRunner)
        created.append(session)
        return session
    monkeypatch.setattr(session_module, "AsrInferenceSession", factory)
    with pytest.raises(InferenceSessionError) as caught:
        transcribe_with_timeout(ASRConfig(model_name="offline", device="cpu"), "os-error",
            paired_subtitle_text=None, timeout_seconds=8)
    assert caught.value.safe_details["errno"] == errno.EMFILE
    assert caught.value.safe_details["phase"] == "decode"
    assert "sensitive" not in str(caught.value) + repr(caught.value.safe_details)
    assert not created[0].persistent and created[0]._process is None


def test_runner_construction_failure_is_sanitized_at_the_process_boundary(capfd):
    session = AsrInferenceSession(runner_factory=ProcessRunner)
    try:
        with pytest.raises(InferenceSessionError):
            invoke(session, config=ASRConfig(model_name="startup-error", device="cpu"))
        assert "sensitive" not in capfd.readouterr().err
        assert session._process is None
        recovered, _ = invoke(session)
        assert recovered[0][0]["text"] == "first:1"
    finally:
        session.close()


def test_large_ipc_response_is_drained_before_join_and_oneshot_restarts():
    session = AsrInferenceSession(runner_factory=ProcessRunner, persistent=False)
    try:
        first, _ = invoke(session, "large")
        assert len(first[0]) == 4096
        assert session._process is None
        second, _ = invoke(session)
        assert second[1]["pid"] != first[1]["pid"]
    finally:
        session.close()


def test_mismatched_late_response_cannot_be_consumed_by_a_new_request():
    session = AsrInferenceSession(runner_factory=ProcessRunner)
    try:
        _, evidence = invoke(session)
        stale = {"protocol": 1, "generation": evidence["session"]["generation"],
            "request_id": evidence["session"]["request_id"], "identity": request().identity(),
            "ok": True, "segments": [{"start": 0, "end": 1, "text": "stale"}], "provenance": {}}
        session._responses.put(stale)
        with pytest.raises(InferenceSessionError, match="identity mismatch"):
            invoke(session, "hang", attempt=2)
        assert session._process is None
        recovered, _ = invoke(session, "fresh", attempt=3)
        assert recovered[0][0]["text"] == "fresh:3"
    finally:
        session.close()


@pytest.mark.parametrize("field", ["protocol", "generation", "request_id", "job_id", "owner",
                                   "attempt_count", "profile_digest", "runtime_binding"])
def test_each_response_identity_field_is_fenced_before_returning_real_child_result(monkeypatch, field):
    session = AsrInferenceSession(runner_factory=ProcessRunner)
    try:
        invoke(session)
        original = session._responses.get
        def tampered_response(*args, **kwargs):
            response = original(*args, **kwargs)
            if field in ("protocol", "generation", "request_id"):
                response[field] = "wrong"
            else:
                response["identity"] = {**response["identity"], field: "wrong"}
            return response
        monkeypatch.setattr(session._responses, "get", tampered_response)
        with pytest.raises(InferenceSessionError, match="identity mismatch"):
            invoke(session, "must-not-be-returned", attempt=2)
        assert session._process is None
        result, evidence = invoke(session, "valid-new-generation", attempt=3)
        assert result[0][0]["text"] == "valid-new-generation:3"
        assert evidence["calls"] == 1
    finally:
        session.close()


def _process_is_live(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        # procfs can return ESRCH after open succeeds but before read finishes.
        return False
    # A zombie no longer executes or holds GPU state; init owns reaping.
    return state != "Z"


@pytest.mark.parametrize("error", [FileNotFoundError(errno.ENOENT, "gone"),
                                  ProcessLookupError(errno.ESRCH, "gone")])
def test_process_probe_accepts_disappearance_during_read(monkeypatch, error):
    def disappeared(_path):
        raise error
    monkeypatch.setattr(Path, "read_text", disappeared)
    assert not _process_is_live(123)


@pytest.mark.parametrize("state,live", [("S", True), ("R", True), ("Z", False)])
def test_process_probe_distinguishes_live_and_zombie_states(monkeypatch, state, live):
    monkeypatch.setattr(Path, "read_text", lambda _: f"123 (worker with ) spaces) {state} 1 2 3")
    assert _process_is_live(123) is live


def test_process_probe_does_not_hide_permission_failures(monkeypatch):
    def denied(_path):
        raise PermissionError(errno.EACCES, "denied")
    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(PermissionError):
        _process_is_live(123)


@pytest.mark.skipif(os.name != "posix", reason="POSIX owned process group cleanup")
def test_crashed_session_leader_reaps_its_remaining_decoder_process(tmp_path):
    session = AsrInferenceSession(runner_factory=ProcessRunner)
    child_file = tmp_path / "decoder.pid"
    try:
        with pytest.raises(InferenceSessionError):
            invoke(session, "orphan:" + str(child_file))
        decoder_pid = int(child_file.read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not _process_is_live(decoder_pid):
                break
            time.sleep(0.02)
        else:
            pytest.fail("decoder survived its crashed inference group leader")
    finally:
        session.close()


def _parent_with_session(pipe, decoder_file=None):
    session = AsrInferenceSession(runner_factory=ProcessRunner)
    result, _ = invoke(session)
    pipe.send(int(result[1]["pid"]))
    invoke(session, "hang" if decoder_file is None else "holdgilchild:" + decoder_file, timeout=40)


@pytest.mark.skipif(os.name != "posix", reason="POSIX parent death and process-group proof")
def test_parent_sigkill_during_inference_does_not_leave_live_child():
    context = multiprocessing.get_context("spawn")
    read, write = context.Pipe(duplex=False)
    parent = context.Process(target=_parent_with_session, args=(write,))
    parent.start()
    write.close()
    try:
        assert read.poll(10)
        child_pid = read.recv()
        os.kill(parent.pid, signal.SIGKILL)
        parent.join(timeout=3)
        deadline = time.monotonic() + 5
        live = True
        while time.monotonic() < deadline:
            live = _process_is_live(child_pid)
            if not live:
                break
            time.sleep(0.05)
        assert not live
    finally:
        if parent.is_alive():
            parent.kill()
            parent.join(timeout=3)
        parent.close()
        read.close()


@pytest.mark.skipif(not __import__("sys").platform.startswith("linux"), reason="Linux kernel parent death signal")
def test_parent_sigkill_while_model_holds_gil_cleans_gpu_and_decoder(tmp_path):
    context = multiprocessing.get_context("spawn")
    read, write = context.Pipe(duplex=False)
    decoder_file = tmp_path / "decoder.pid"
    parent = context.Process(target=_parent_with_session, args=(write, str(decoder_file)))
    parent.start()
    write.close()
    try:
        assert read.poll(10)
        gpu_pid = read.recv()
        deadline = time.monotonic() + 5
        while not decoder_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        decoder_pid = int(decoder_file.read_text())
        os.kill(parent.pid, signal.SIGKILL)
        parent.join(timeout=2)
        for pid in (gpu_pid, decoder_pid):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if not _process_is_live(pid):
                    break
                time.sleep(0.02)
            else:
                pytest.fail(f"owned process survived parent death: {pid}")
    finally:
        if parent.is_alive():
            parent.kill()
            parent.join(timeout=2)
        parent.close()
        read.close()
