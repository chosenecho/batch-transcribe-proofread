"""Deployment/readiness contracts with real spawn and SQLite, no GPU."""

from dataclasses import replace
import hashlib
import json
import os
import sys
import time
import multiprocessing
from types import SimpleNamespace

import pytest

from bili_asr.asr.config import ASRConfig, default_config
from bili_asr.asr.deployment import cache_identity, deployment_report
from bili_asr.asr.device_probe import probe_gpu
from bili_asr.asr.runner import ASRInferenceTimeoutError, _load_qwen_models
from bili_asr.asr.runtime_cache import configure_cache, RuntimeCacheError
from bili_asr.asr.session import AsrInferenceSession, InferenceSessionError
from bili_asr.storage import open_database, WorkflowRepository
from bili_asr.workflow import WorkflowExecutor
from bili_asr.workflow_models import AsrPolicy, AsrProfile, JobKind
from test_asr_sessions import ProcessRunner, request
from test_workflow_control_plane import _seed_part


class ReadyRunner(ProcessRunner):
    def prepare(self, audio_path=None):
        if self.config.model_name == "warmup-hang":
            time.sleep(20)
        if self.config.model_name == "warmup-crash":
            os._exit(9)
        if self.config.model_name == "warmup-error":
            raise ValueError("secret-cookie /private/model")
        return {"state": "ready" if audio_path is not None else "loaded"}


def test_precision_extension_preserves_v2_bytes_and_new_profile_identity(tmp_path):
    connection = open_database(tmp_path)
    repository = WorkflowRepository(connection)
    profile = AsrProfile("default", "local", device="cpu")
    # Freeze the historical fields; deriving them from today's dataclass would
    # silently extend the expected old snapshot whenever an opt-in knob is added.
    old_values = {"model_name": "local", "model_revision": "", "aligner_name": "Qwen/Qwen3-ForcedAligner-0.6B-hf",
        "aligner_revision": None, "device": "cpu", "language": None, "hotwords": (),
        "chunk_seconds": 180.0, "inference_timeout_seconds": 1800.0, "tokens_per_second": 8.0,
        "min_new_tokens": 256, "second_pass_use_cache": False, "offline": True, "model_id": None}
    old = json.dumps({"schema_version": 2, **old_values}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert profile.canonical() == old
    original_id = repository.register_profile(profile)
    original_digest = repository.profile_digest(original_id)
    changed_id = repository.register_profile(replace(profile, model_dtype="float16"))
    assert changed_id != original_id
    assert repository.profile(changed_id).asr_config().model_dtype == "float16"
    assert repository.profile(changed_id).asr_config().aligner_dtype == "bfloat16"
    assert repository.profile(original_id).canonical() == old
    assert repository.profile_digest(original_id) == original_digest == hashlib.sha256(old.encode()).hexdigest()
    connection.close()


@pytest.mark.parametrize("value", ["fp8", "", None, True, 16, []])
def test_unknown_precision_never_falls_back(value):
    with pytest.raises(ValueError, match="dtype"):
        ASRConfig("local", model_dtype=value)


def test_precision_loads_both_models_independently(monkeypatch):
    calls = []
    class Loader:
        @classmethod
        def from_pretrained(cls, name, **kwargs):
            calls.append((name, kwargs))
            return SimpleNamespace(eval=lambda: None)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(bfloat16="bf16", float16="fp16"))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoProcessor=Loader, AutoModelForMultimodalLM=Loader, AutoModelForTokenClassification=Loader))
    _load_qwen_models(model_name="asr", aligner_name="aligner", device="cuda",
                      model_dtype="float16", aligner_dtype="bfloat16")
    assert [(name, kwargs["dtype"]) for name, kwargs in calls if "dtype" in kwargs] == [
        ("asr", "fp16"), ("aligner", "bf16")]
    monkeypatch.setenv("BILI_ASR_MODEL_DTYPE", "float16")
    assert default_config().model_dtype == "float16"


def test_real_spawn_prepares_reuses_and_rebuilds_for_precision_and_sentinel(tmp_path):
    session = AsrInferenceSession(runner_factory=ReadyRunner)
    config = ASRConfig("local", device="cpu")
    sentinel = tmp_path / "sample.wav"
    sentinel.write_bytes(b"first")
    try:
        evidence = session.prepare(config, request=request(), timeout_seconds=8, audio_path=str(sentinel))
        generation = evidence["generation"]
        assert session.state == "ready" and evidence["state"] == "ready"
        assert session.prepare(config, request=request(2), timeout_seconds=8,
                               audio_path=str(sentinel))["generation"] == generation
        diagnostics = {}
        session.transcribe(config, "business", request=request(), paired_subtitle_text=None,
                           timeout_seconds=8, diagnostics_sink=diagnostics)
        assert diagnostics["calls"] == 1
        assert diagnostics["session"]["preparation"]["warmup_sha256"] == hashlib.sha256(b"first").hexdigest()
        sentinel.write_bytes(b"changed")
        assert session.prepare(config, request=request(), timeout_seconds=8,
                               audio_path=str(sentinel))["generation"] > generation
        changed = replace(config, model_dtype="float16")
        with pytest.raises(InferenceSessionError, match="identity"):
            session.transcribe(changed, "business", request=request(), paired_subtitle_text=None, timeout_seconds=8)
        assert session._process is None
        assert session.prepare(changed, request=request(), timeout_seconds=8)["state"] == "loaded"
    finally:
        session.close()


@pytest.mark.parametrize("name,error,timeout", [
    ("warmup-hang", ASRInferenceTimeoutError, 0.5),
    ("warmup-crash", InferenceSessionError, 8),
    ("warmup-error", InferenceSessionError, 8),
])
def test_preparation_faults_reap_owned_child_and_remain_retryable(name, error, timeout):
    session = AsrInferenceSession(runner_factory=ReadyRunner)
    try:
        with pytest.raises(error) as caught:
            session.prepare(ASRConfig(name, device="cpu"), request=request(), timeout_seconds=timeout)
        assert "secret" not in str(caught.value)
        assert session.state == "degraded" and session._process is None
        assert session.prepare(ASRConfig("local", device="cpu"), request=request(), timeout_seconds=8)["state"] == "loaded"
    finally:
        session.close()


@pytest.fixture
def queued_repository(tmp_path):
    connection = open_database(tmp_path)
    _seed_part(connection)
    repository = WorkflowRepository(connection)
    profile = repository.register_profile(AsrProfile("test", "local", device="cpu"))
    repository.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=profile)
    yield repository
    connection.close()


def test_preparation_failure_and_drain_do_not_create_attempts(queued_repository):
    repository = queued_repository
    def fail(candidate, checkpoint):
        assert repository.connection.in_transaction is False
        raise InferenceSessionError("warmup failed")
    executor = WorkflowExecutor(repository, worker_id="test", kinds=(JobKind.SUBTITLE,),
                                handlers={JobKind.SUBTITLE: lambda job: {}}, prepare_candidate=fail)
    with pytest.raises(InferenceSessionError):
        executor.run()
    assert repository.connection.execute("SELECT count(*) FROM workflow_attempts").fetchone()[0] == 0
    drained = False
    def drain(candidate, checkpoint):
        nonlocal drained
        drained = True
        checkpoint()
    executor.prepare_candidate = drain
    executor.drain_requested = lambda: drained
    assert executor.run().idle
    assert repository.count_by_status() == {"queued": 3}


def test_candidate_cancel_or_competing_claim_is_not_claimed_after_prepare(queued_repository):
    repository = queued_repository
    candidate = repository.peek_candidate(kinds=(JobKind.SUBTITLE,))
    competing = repository.claim("other", kinds=(JobKind.SUBTITLE,))
    assert candidate.job_id == competing.job_id
    assert repository.claim("test", expected_candidate=candidate) is None
    audio = repository.peek_candidate(kinds=(JobKind.AUDIO,))
    repository.cancel(job_ids=[audio.job_id])
    assert repository.claim("test", expected_candidate=audio) is None
    assert repository.connection.execute("SELECT count(*) FROM workflow_attempts").fetchone()[0] == 1


def test_expired_candidate_recovery_preserves_attempt_and_fences_stale_hint(queued_repository):
    repository = queued_repository
    old = repository.claim("old", kinds=(JobKind.SUBTITLE,))
    with repository.connection:
        repository.connection.execute("UPDATE workflow_jobs SET lease_expires_at=0 WHERE job_id=?", (old.job_id,))
    candidate = repository.peek_candidate(kinds=(JobKind.SUBTITLE,))
    assert candidate.attempt_count == 1
    assert repository.connection.execute("SELECT outcome FROM workflow_attempts").fetchone()[0] == "running"
    replacement = repository.claim("new", expected_candidate=candidate)
    assert replacement.attempt_count == 2
    assert repository.claim("stale", expected_candidate=candidate) is None
    assert [row[0] for row in repository.connection.execute("SELECT outcome FROM workflow_attempts ORDER BY rowid")] == [
        "failed", "running"]


def test_deployment_doctor_stays_offline_and_reports_unknown_not_ready(monkeypatch, tmp_path):
    from bili_asr.asr import deployment
    monkeypatch.setattr(deployment, "installed_versions", lambda: {name: "test" for name in deployment.PACKAGES})
    monkeypatch.setattr(deployment.subprocess, "run", lambda *args, **kwargs: pytest.fail("offline doctor spawned"))
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    before = sorted(tmp_path.rglob("*"))
    report = deployment_report(ASRConfig(str(model), aligner_name=str(model)), backend="hcu",
                               cache_root=str(tmp_path / "cache"))
    assert report["state"] == "unverified" and report["model_execution"] == "not_run"
    assert report["model"]["identity"] == "unverified"
    assert str(tmp_path) not in json.dumps(report)
    assert sorted(tmp_path.rglob("*")) == before


def test_gpu_doctor_bounds_probe_and_redacts_failure(monkeypatch):
    from bili_asr.asr import deployment
    seen = []
    def child(command, **kwargs):
        seen.append(kwargs["timeout"])
        return SimpleNamespace(returncode=1, stdout='{"status":"failed","error_type":"secret"}', stderr="cookie")
    monkeypatch.setattr(deployment.subprocess, "run", child)
    report = deployment_report(ASRConfig("local"), backend="hcu", check_gpu=True, timeout_seconds=1)
    assert report["state"] == "blocked" and seen == [1]
    assert "secret" not in json.dumps(report) and "cookie" not in json.dumps(report)


def test_offline_hub_reference_is_unverified_and_missing_explicit_local_is_blocked(monkeypatch, tmp_path):
    from bili_asr.asr import deployment
    monkeypatch.setattr(deployment, "installed_versions", lambda: {name: "test" for name in deployment.PACKAGES})
    monkeypatch.setattr(deployment.subprocess, "run", lambda *args, **kwargs: pytest.fail("offline doctor spawned"))
    monkeypatch.chdir(tmp_path)
    report = deployment_report(ASRConfig("Qwen/Qwen3-ASR-1.7B-hf"), backend="cuda")
    assert report["state"] == "unverified"
    assert report["model"]["kind"] == report["aligner"]["kind"] == "unverified"
    assert report["model_execution"] == "not_run"
    missing = deployment_report(ASRConfig(str(tmp_path / "missing")), backend="cuda")
    assert missing["state"] == "blocked" and missing["model"]["kind"] == "local"
    assert list(tmp_path.iterdir()) == []


def test_hcu_identity_requires_real_named_device_and_precision_execution(monkeypatch):
    from test_verify_gpu import fake_torch
    fake_torch(monkeypatch, backend="rocm")
    torch = sys.modules["torch"]
    torch.float16 = "fp16"
    torch.cuda.get_device_name = lambda index: "HYGON BW1000"
    torch.cuda.synchronize = lambda index: None
    torch.allclose = lambda left, right: True
    original = torch.ones
    class Tensor:
        def __matmul__(self, other):
            return self
        def __mul__(self, value):
            return self
    torch.ones = lambda *args, **kwargs: Tensor()
    torch.isfinite = lambda value: original(0)
    report = probe_gpu("hcu", dtypes=("float16", "bfloat16"))
    assert report["precision_execution"] == {"float16": "passed", "bfloat16": "passed"}
    assert report["model_execution"] == "not_run"
    torch.cuda.get_device_name = lambda index: "AMD Radeon"
    with pytest.raises(RuntimeError, match="identity"):
        probe_gpu("hcu")
    torch.cuda.get_device_name = lambda index: "HYGON BW1000"
    torch.allclose = lambda left, right: False
    with pytest.raises(RuntimeError, match="multiplication"):
        probe_gpu("hcu")


def test_cache_namespace_reuse_corruption_limits_and_no_compilation(tmp_path, monkeypatch):
    monkeypatch.setenv("TORCHINDUCTOR_CACHE_DIR", "old")
    monkeypatch.setenv("TRITON_CACHE_DIR", "old")
    identity = cache_identity(ASRConfig("local"), hardware={"name": "BW1000", "arch": "actual"},
                              packages={"torch": "vendor"})
    report = configure_cache(str(tmp_path), identity, max_bytes=4096)
    assert report["compile_enabled"] is False
    assert configure_cache(str(tmp_path), identity, max_bytes=4096)["namespace_sha256"] == report["namespace_sha256"]
    namespace = tmp_path / report["namespace_sha256"]
    (namespace / "triton" / "binary").write_bytes(b"x" * 4097)
    with pytest.raises(RuntimeCacheError, match="byte limit"):
        configure_cache(str(tmp_path), identity, max_bytes=4096)
    (namespace / "triton" / "binary").unlink()
    (namespace / "identity.json").write_bytes(b"tampered")
    with pytest.raises(RuntimeCacheError, match="manifest"):
        configure_cache(str(tmp_path), identity, max_bytes=4096)
    assert (namespace / "identity.json").read_bytes() == b"tampered"


def _shared_cache_worker(root, start, results):
    start.wait(5)
    try:
        report = configure_cache(root, {"schema_version": 1, "identity": "shared"}, max_bytes=4096)
        results.put(("ok", report["namespace_sha256"]))
    except Exception as exc:
        results.put(("error", type(exc).__name__))


def test_real_spawn_concurrent_cache_initialization_is_compatible(tmp_path):
    context = multiprocessing.get_context("spawn")
    start, results = context.Event(), context.Queue()
    children = [context.Process(target=_shared_cache_worker, args=(str(tmp_path), start, results)) for _ in range(2)]
    try:
        for child in children:
            child.start()
        start.set()
        reports = [results.get(timeout=10) for _ in children]
        assert reports[0] == reports[1] and reports[0][0] == "ok"
    finally:
        for child in children:
            child.join(timeout=5)
            if child.is_alive():
                child.kill()
                child.join()
            child.close()
        results.close()


def test_nondefault_gpu_uses_requested_device_not_default_capability(monkeypatch):
    from scripts import verify_gpu
    from test_verify_gpu import fake_torch
    fake_torch(monkeypatch)
    torch = sys.modules["torch"]
    torch.cuda.is_bf16_supported = lambda: pytest.fail("default-device capability must not be queried")
    calls = []
    original = torch.ones
    def ones(*args, **kwargs):
        calls.append(kwargs["device"])
        return original(*args, **kwargs)
    torch.ones = ones
    monkeypatch.setattr(sys, "argv", ["verify_gpu.py", "--backend", "cuda", "--device", "cuda:1"])
    assert verify_gpu.main() == 0
    assert calls == ["cuda:1"]


def test_legacy_explicit_options_are_rejected_before_claim(tmp_path, queued_repository):
    from bili_asr.archive_session import ArchiveSession, ArchiveAccessMode
    from bili_asr.services.workflow_application import WorkflowApplication
    with ArchiveSession(tmp_path, mode=ArchiveAccessMode.WRITE) as archive:
        with pytest.raises(ValueError, match="supervised"):
            WorkflowApplication(archive).run(worker_id="test", sessdata=None, gpu_session="legacy",
                                             cache_root=str(tmp_path / "cache"))
    assert queued_repository.connection.execute("SELECT count(*) FROM workflow_attempts").fetchone()[0] == 0


@pytest.mark.parametrize("packages_present,expected_exit", [(False, 1), (True, 0)])
def test_cli_precision_is_frozen_and_offline_hcu_report_is_readonly(
    tmp_path, queued_repository, monkeypatch, capsys, packages_present, expected_exit,
):
    from bili_asr.cli import main
    from bili_asr.asr import deployment
    assert main(["workflow", "plan", "--archive-root", str(tmp_path), "--part-id", "1",
                 "--model", "local", "--model-dtype", "float16", "--aligner-dtype", "bfloat16",
                 "--profile-key", "explicit"]) == 0
    row = queued_repository.connection.execute(
        "SELECT profile_id FROM workflow_asr_profiles WHERE profile_key='explicit'").fetchone()
    profile = queued_repository.profile(row[0])
    monkeypatch.setenv("BILI_ASR_MODEL_DTYPE", "bfloat16")
    assert profile.asr_config().model_dtype == "float16"
    before = queued_repository.connection.execute("SELECT count(*) FROM workflow_attempts").fetchone()[0]
    # Offline inspection reports dependency presence without probing hardware.
    # Exercise both graphs explicitly; a CUDA host has the ASR extras installed.
    monkeypatch.setattr(deployment, "installed_versions", lambda: {
        name: "test-version" if packages_present else None for name in deployment.PACKAGES
    })
    monkeypatch.setattr(deployment.subprocess, "run", lambda *args, **kwargs: pytest.fail("offline probe"))
    capsys.readouterr()
    assert main(["check-asr-env", "--backend", "hcu"]) == expected_exit
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == ("unverified" if packages_present else "blocked")
    assert report["missing_packages"] == ([] if packages_present else ["torch", "transformers", "accelerate"])
    assert report["model_execution"] == "not_run"
    assert queued_repository.connection.execute("SELECT count(*) FROM workflow_attempts").fetchone()[0] == before


def test_concrete_gpu_handler_prepares_frozen_candidate_before_attempt(tmp_path, queued_repository, monkeypatch):
    from bili_asr.workflow_runtime import ArchiveWorkflowHandlers
    repository = queued_repository
    # Complete only the actual prerequisites so ASR becomes eligible.
    for kind in (JobKind.SUBTITLE, JobKind.AUDIO):
        job = repository.claim("setup", kinds=(kind,))
        repository.finish(job.job_id, worker_id="setup", expected_attempt_count=job.attempt_count, result={})
    profile_id = repository.register_profile(AsrProfile("gpu", "local", model_dtype="float16"))
    repository.cancel(job_ids=[repository.peek_candidate(kinds=(JobKind.ASR,)).job_id])
    repository.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=profile_id)
    attempts_before = repository.connection.execute("SELECT count(*) FROM workflow_attempts").fetchone()[0]
    calls = []
    class Session:
        def prepare(self, config, **kwargs):
            assert repository.connection.execute("SELECT count(*) FROM workflow_attempts").fetchone()[0] == attempts_before
            calls.append((config.model_dtype, kwargs["request"].profile_digest))
            kwargs["checkpoint"]()
        def close(self):
            pass
    handlers = ArchiveWorkflowHandlers(repository.connection, repository, archive_root=tmp_path,
                                       sessdata=None, inference_session=Session())
    monkeypatch.setenv("BILI_ASR_MODEL_DTYPE", "bfloat16")
    try:
        result = WorkflowExecutor(repository, worker_id="ready", kinds=(JobKind.ASR,),
                                  handlers={JobKind.ASR: lambda job: {"prepared": True}},
                                  prepare_candidate=lambda candidate, checkpoint:
                                      handlers.prepare_candidate(candidate, checkpoint, timeout_seconds=5)).run()
        assert result.succeeded == 1
        assert calls == [("float16", repository.profile_digest(profile_id))]
    finally:
        handlers.close()


@pytest.mark.skipif(os.name != "posix", reason="symlink privilege is host-dependent on Windows")
def test_cache_symlink_rejected_without_writing_outside(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeCacheError, match="symlink"):
        configure_cache(str(link), {"schema_version": 1}, max_bytes=4096)
    assert list(outside.iterdir()) == []
