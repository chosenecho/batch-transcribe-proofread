"""A bounded, killable model session. Durable ownership always stays in the parent."""

from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import stat
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any
from uuid import uuid4
from pathlib import Path

from .config import ASRConfig
from .execution import clock_anchor
from .runner import ASRInferenceTimeoutError, ASRRunner, two_pass_transcribe
from .progress import set_progress_hook
from .deadlines import ProgressWatchdog
from bili_asr.exception_diagnostics import DiagnosedFailure, capture_failure, filter_failure_details

_DECODER_GUARDS: list[subprocess.Popen] = []


def transcribe_with_timeout(config: Any, audio_path: str, *, paired_subtitle_text: str | None,
                            timeout_seconds: float, diagnostics_sink: dict[str, Any] | None = None,
                            artifact_context: dict | None = None):
    """One-shot inference uses the same filtered, killable session protocol."""
    session = AsrInferenceSession(persistent=False)
    try:
        return session.transcribe(config, audio_path,
            request=InferenceRequest("standalone-inference", "standalone", 1, "standalone", {}),
            paired_subtitle_text=paired_subtitle_text, timeout_seconds=timeout_seconds,
            diagnostics_sink=diagnostics_sink, artifact_context=artifact_context)
    finally:
        session.close()


def _warmup_digest(path: str | None) -> str | None:
    if path is None:
        return None
    source = Path(path)
    for item in (source, *source.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("warmup audio cannot use symlinked or reparse paths")
    if not source.is_absolute() or source.is_symlink() or not source.is_file() or source.stat().st_size > 32 * 1024**2:
        raise ValueError("warmup requires an absolute regular audio file up to 32 MiB")
    with source.open("rb") as handle:
        data = handle.read(32 * 1024**2 + 1)
    if len(data) > 32 * 1024**2:
        raise ValueError("warmup audio exceeds 32 MiB")
    return hashlib.sha256(data).hexdigest()


class InferenceSessionError(RuntimeError, DiagnosedFailure):
    """A session failed without disclosing model paths or third-party errors."""

    error_code = "inference_session_failed"

    def __init__(self, message, *, diagnostic=None):
        super().__init__(message)
        self.set_failure_details(diagnostic or {"phase": "ipc"})


@dataclass(frozen=True)
class InferenceRequest:
    job_id: str
    owner: str
    attempt_count: int
    profile_digest: str
    runtime_binding: Mapping[str, Any]

    def identity(self) -> dict[str, Any]:
        return {"job_id": self.job_id, "owner": self.owner,
                "attempt_count": self.attempt_count, "profile_digest": self.profile_digest,
                "runtime_binding": dict(self.runtime_binding)}


def _parent_guard() -> None:
    """Exit even when inference is blocked and the parent is killed without cleanup."""
    parent = multiprocessing.parent_process()
    original_parent = os.getppid()
    while True:
        time.sleep(0.1)
        if (parent is not None and not parent.is_alive()) or os.getppid() != original_parent:
            if os.name == "posix":
                # The session starts its own process group. This also owns its ffmpeg children.
                os.killpg(os.getpgrp(), signal.SIGKILL)
            os._exit(1)


def _session_worker(config, commands, responses, generation, runner_factory, prefetch, prefetch_bytes,
                    cache_root=None, cache_max_bytes=10 * 1024**3):
    if os.name == "posix":
        os.setsid()
    if sys.platform.startswith("linux"):
        # Native inference may hold the GIL. Linux's parent-death signal still
        # kills the GPU owner when a Python guardian thread cannot run.
        import ctypes
        creator = multiprocessing.parent_process()
        expected_parent = creator.pid if creator is not None else os.getppid()
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise RuntimeError("cannot establish inference parent-death ownership")
        if os.getppid() != expected_parent:
            os._exit(1)
    if os.name == "posix":
        # A separate interpreter can observe EOF after the GPU owner is killed,
        # including by the kernel parent-death signal. It also cleans up decoder
        # descendants when the leader no longer exists. No model modules load here.
        guard_code = (
            "import os,signal,sys;sys.stdin.buffer.read();"
            "os.killpg(int(sys.argv[1]),signal.SIGKILL)"
        )
        _DECODER_GUARDS.append(subprocess.Popen([sys.executable, "-c", guard_code, str(os.getpid())],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    threading.Thread(target=_parent_guard, daemon=True, name="asr-parent-guard").start()
    runner = None
    cache_evidence = None
    try:
        while True:
            command = commands.get()
            if command is None:
                return
            envelope = {key: command[key] for key in ("protocol", "generation", "request_id", "identity")}
            if command["protocol"] != 1 or command["generation"] != generation:
                return
            progress_state = {"sequence": 0, "phase": "model_load"}
            def progress(phase):
                phases = {"load": "model_load", "decode": "decode", "align": "align",
                          "model_ready": "audio_prepare", "audio_ready": "decode",
                          "decoded": "align", "aligned": "align", "chunk_complete": "decode"}
                progress_state["phase"] = phases.get(phase, "inference")
                if phase in {"model_ready", "audio_ready", "decoded", "aligned", "chunk_complete"}:
                    progress_state["sequence"] += 1
                    responses.put({**envelope, "progress": {"phase": phase,
                                   "sequence": progress_state["sequence"]}})
            set_progress_hook(progress)
            try:
                if runner is None:
                    if cache_root is not None:
                        from .runtime_cache import prepare_runtime_cache
                        cache_evidence = prepare_runtime_cache(config, cache_root, max_bytes=cache_max_bytes,
                                                               binding=command["identity"]["runtime_binding"])
                    runner = runner_factory(config)
                    if prefetch:
                        configure = getattr(runner, "configure_prefetch", None)
                        if callable(configure):
                            configure(enabled=True, max_bytes=prefetch_bytes)
                if command.get("operation") == "prepare":
                    sentinel = command.get("audio_path")
                    if _warmup_digest(sentinel) != command.get("warmup_sha256"):
                        raise ValueError("warmup audio changed before preparation")
                    evidence = runner.prepare(sentinel)
                    if _warmup_digest(sentinel) != command.get("warmup_sha256"):
                        raise ValueError("warmup audio changed during preparation")
                    evidence["warmup_sha256"] = command.get("warmup_sha256")
                    if cache_evidence is not None:
                        evidence["cache"] = cache_evidence
                    responses.put({**envelope, "ok": True, "preparation": evidence})
                    continue
                from bili_asr.artifact_child import child_artifact_access
                with child_artifact_access(command.get("artifact_context"), command["audio_path"]):
                    segments = two_pass_transcribe(runner, command["audio_path"],
                                                  paired_subtitle_text=command["paired_subtitle_text"])
                responses.put({**envelope, "ok": True, "segments": segments,
                               "provenance": runner.provenance(), "coverage": runner.transcribed_coverage(),
                               "diagnostics": runner.diagnostics()})
            except BaseException as exc:  # noqa: BLE001 - complete the process protocol on model errors.
                responses.put({**envelope, "ok": False,
                               "diagnostic": capture_failure(exc, phase=progress_state["phase"])})
                # A failure may leave CUDA state or task-local state corrupt. Never reuse it.
                return
            finally:
                set_progress_hook(None)
    finally:
        if runner is not None:
            runner.release()


class AsrInferenceSession:
    """One sequential GPU lane, rebuilt on configuration changes or any failed request."""

    def __init__(self, *, persistent: bool = True, runner_factory: Callable = ASRRunner,
                 context: Any = None, prefetch: bool = False, prefetch_bytes: int = 64 * 1024 * 1024,
                 cache_root: str | None = None, cache_max_bytes: int = 10 * 1024**3):
        if isinstance(prefetch_bytes, bool) or prefetch_bytes < 1:
            raise ValueError("prefetch_bytes must be positive")
        self.persistent = persistent
        self.runner_factory = runner_factory
        self.context = context or multiprocessing.get_context("spawn")
        self.prefetch, self.prefetch_bytes = prefetch, prefetch_bytes
        if type(cache_max_bytes) is not int or cache_max_bytes < 1:
            raise ValueError("cache startup byte limit must be positive")
        if cache_root is not None and not os.path.isabs(cache_root):
            raise ValueError("cache root must be absolute")
        self.cache_root, self.cache_max_bytes = cache_root, cache_max_bytes
        self._process = self._commands = self._responses = None
        self._key = None
        self._generation = 0
        self._lock = threading.Lock()
        self._previous_finish: float | None = None
        self._reason = "initial"
        self.state = "stopped"
        self._ready_identity = None
        self._preparation = None

    @staticmethod
    def configuration_key(config: ASRConfig, request: InferenceRequest) -> str:
        raw = json.dumps({"config": asdict(config), "runtime_binding": dict(request.runtime_binding)},
                         sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _start(self, config: ASRConfig, key: str) -> None:
        self.state = "starting"
        self._generation += 1
        self._commands = self.context.Queue(maxsize=1)
        self._responses = self.context.Queue(maxsize=1)
        self._process = self.context.Process(target=_session_worker,
            args=(config, self._commands, self._responses, self._generation,
                  self.runner_factory, self.prefetch, self.prefetch_bytes, self.cache_root, self.cache_max_bytes),
            name=f"bili-asr-session-{self._generation}", daemon=True)
        try:
            self._process.start()
        except BaseException:
            self.terminate()
            raise
        self._key = key

    def _receive(self, envelope: dict, deadline: float, checkpoint: Callable[[], None],
                 watchdog: ProgressWatchdog | None = None) -> dict:
        while True:
            checkpoint()
            if watchdog is not None:
                reason = watchdog.expired(time.monotonic())
                if reason is not None:
                    raise ASRInferenceTimeoutError(reason=reason)
            if time.monotonic() >= deadline:
                raise ASRInferenceTimeoutError("ASR session deadline elapsed")
            try:
                result = self._responses.get(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                if self._process.is_alive():
                    continue
                try:
                    result = self._responses.get(timeout=min(0.2, max(0.001, deadline - time.monotonic())))
                except queue.Empty:
                    raise InferenceSessionError("ASR session exited without a result",
                        diagnostic={"phase": "ipc", "reason": "child_exited",
                                    "exit_code": self._process.exitcode}) from None
            if not isinstance(result, dict) or any(result.get(key) != value for key, value in envelope.items()):
                raise InferenceSessionError("ASR session response identity mismatch",
                    diagnostic={"phase": "ipc", "reason": "response_identity_mismatch"})
            checkpoint()
            now = time.monotonic()
            if watchdog is not None:
                reason = watchdog.expired(now)
                if reason is not None:
                    raise ASRInferenceTimeoutError(reason=reason)
            if now >= deadline:
                raise ASRInferenceTimeoutError("ASR result arrived after the request deadline")
            if "progress" in result:
                if watchdog is not None and not watchdog.observe(result["progress"], now):
                    raise InferenceSessionError("invalid ASR progress response", diagnostic={"phase": "ipc"})
                continue
            if not result.get("ok"):
                raise InferenceSessionError("ASR session failed; restart required",
                                            diagnostic=filter_failure_details(result.get("diagnostic")))
            return result

    def prepare(self, config: ASRConfig, *, request: InferenceRequest, timeout_seconds: float,
                audio_path: str | None = None, checkpoint: Callable[[], None] = lambda: None) -> dict:
        """Establish readiness for exactly this profile/binding without claiming work."""
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("warmup timeout must be finite and positive")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("an inference session accepts one active request")
        try:
            checkpoint()
            key = self.configuration_key(config, request)
            warmup_sha256 = _warmup_digest(audio_path)
            identity = (key, request.profile_digest, warmup_sha256)
            if (self._process is not None and self._process.is_alive()
                    and self._ready_identity == identity):
                return dict(self._preparation)
            if self._process is not None:
                self.terminate()
            self._start(config, key)
            self.state = "warming" if audio_path is not None else "loading"
            envelope = {"protocol": 1, "generation": self._generation,
                        "request_id": uuid4().hex, "identity": request.identity()}
            started = time.monotonic()
            deadline = started + timeout_seconds
            self._commands.put({**envelope, "operation": "prepare", "audio_path": audio_path,
                                "warmup_sha256": warmup_sha256},
                               timeout=min(1, timeout_seconds))
            response = self._receive(envelope, deadline, checkpoint)
            evidence = response.get("preparation")
            if not isinstance(evidence, dict) or evidence.get("state") not in ("loaded", "ready"):
                raise InferenceSessionError("invalid ASR preparation response")
            if audio_path is not None and evidence["state"] != "ready":
                raise InferenceSessionError("warmup did not establish readiness")
            self.state = "ready"
            self._ready_identity = identity
            self._preparation = {**evidence, "generation": self._generation,
                                 "profile_digest": request.profile_digest,
                                 "parent_wall_s": time.monotonic() - started}
            return dict(self._preparation)
        except BaseException:
            self.terminate()
            self.state = "degraded"
            raise
        finally:
            self._lock.release()

    def transcribe(self, config: ASRConfig, audio_path: str, *, request: InferenceRequest,
                   paired_subtitle_text: str | None, timeout_seconds: float,
                   checkpoint: Callable[[], None] = lambda: None,
                   diagnostics_sink: dict[str, Any] | None = None, artifact_context: dict | None = None):
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("an inference session accepts one active request")
        try:
            checkpoint()
            key = self.configuration_key(config, request)
            if self._ready_identity is not None and (
                    self._ready_identity[:2] != (key, request.profile_digest)
                    or self._process is None or not self._process.is_alive()):
                raise InferenceSessionError("prepared session identity changed before inference")
            if self._process is not None and (self._key != key or not self._process.is_alive()):
                self._reason = "configuration_changed" if self._key != key else "child_exited"
                self.terminate()
            reused = self._process is not None
            if self._process is None:
                self._start(config, key)
            request_id = uuid4().hex
            envelope = {"protocol": 1, "generation": self._generation,
                        "request_id": request_id, "identity": request.identity()}
            start = time.monotonic()
            parent_clock = clock_anchor()
            deadline = start + timeout_seconds
            scaled = config.inference_timeout_policy == "audio-scaled"
            watchdog = ProgressWatchdog(started=start, total_seconds=timeout_seconds,
                startup_seconds=config.inference_startup_seconds if scaled else None,
                no_progress_seconds=config.inference_no_progress_seconds if scaled else None)
            self._commands.put({**envelope, "audio_path": audio_path,
                                "artifact_context": artifact_context,
                                "paired_subtitle_text": paired_subtitle_text}, timeout=min(1, timeout_seconds))
            result = self._receive(envelope, deadline, checkpoint, watchdog)
            diagnostics = dict(result.get("diagnostics") or {})
            diagnostics["session"] = {"schema_version": 1, "generation": self._generation,
                "request_id": request_id, "identity": request.identity(), "reused": reused,
                "rebuild_reason": None if reused else self._reason,
                "parent_wall_s": time.monotonic() - start,
                "parent_clock": parent_clock, "configuration_key": key,
                "task_gap_s": None if self._previous_finish is None else start - self._previous_finish,
                "mode": "persistent" if self.persistent else "oneshot"}
            if self._preparation is not None:
                diagnostics["session"]["preparation"] = dict(self._preparation)
            if diagnostics_sink is not None:
                diagnostics_sink.clear()
                diagnostics_sink.update(diagnostics)
            self._previous_finish = time.monotonic()
            if not self.persistent:
                self.close()
            return list(result["segments"]), dict(result["provenance"]), result.get("coverage")
        except BaseException:
            self._reason = "request_failed"
            self.terminate()
            raise
        finally:
            self._lock.release()

    def terminate(self) -> None:
        self.state = "stopped"
        self._ready_identity = self._preparation = None
        process, self._process = self._process, None
        if process is not None and process.pid is not None:
            if process.is_alive():
                try:
                    if os.name == "posix" and os.getpgid(process.pid) == process.pid:
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                except ProcessLookupError:
                    pass
                process.join(timeout=1)
                if process.is_alive():
                    try:
                        if os.name == "posix" and os.getpgid(process.pid) == process.pid:
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    except ProcessLookupError:
                        pass
                    process.join(timeout=1)
            else:
                process.join(timeout=0)
            if os.name == "posix":
                # A crashed group leader can leave ffmpeg alive in its owned group.
                # Group IDs remain reserved while members exist; never scan or kill
                # unrelated PIDs to find these descendants.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.close()
        self._key = None
        for name in ("_commands", "_responses"):
            channel = getattr(self, name)
            if channel is not None:
                channel.cancel_join_thread()
                channel.close()
                setattr(self, name, None)

    def close(self) -> None:
        self.state = "draining"
        if self._process is not None and self._process.is_alive():
            try:
                self._commands.put(None, timeout=0.1)
                self._process.join(timeout=1)
            except queue.Full:
                pass
        self.terminate()
