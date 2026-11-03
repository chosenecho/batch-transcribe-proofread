"""Explicit strategies preserve defaults, bounded code reuse and request isolation."""

import argparse
from dataclasses import replace
from types import SimpleNamespace
import sys

import numpy as np
import pytest

from bili_asr.asr.config import ASRConfig
from bili_asr.asr.strategies import RuntimeStrategies
from bili_asr.cli.workflow import _cmd_workflow, add_workflow_parser
from bili_asr.storage import AsrProfile, WorkflowRepository
from test_asr_quality_foundation import database as database
from test_asr_batching import runner_for


def inputs(length=8):
    return {"input_ids": np.zeros((1, length), dtype=np.int64)}


class Generator:
    device = "cuda:0"

    def __init__(self):
        self.calls, self.resets = [], []

    def generate(self, **kwargs):
        assert not hasattr(self, "_cache"), "request reused preceding KV"
        self._cache = SimpleNamespace(reset=lambda: self.resets.append(True))
        self.calls.append(kwargs)
        return "result"


def compiler_config(monkeypatch):
    def configured(**kwargs):
        # HF forwards to torch.compile, where mode and options are exclusive.
        assert kwargs.get("mode") is None or kwargs.get("options") is None
        return kwargs
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(CompileConfig=configured))


def test_static_generation_limits_shapes_and_tokens_resets_cache_and_keeps_second_pass_rule(monkeypatch):
    compiler_config(monkeypatch)
    policy = RuntimeStrategies(ASRConfig(model_name="model", asr_cache_implementation="static", asr_compile=True,
        compile_max_buckets=1, compile_max_input_tokens=10, compile_max_output_tokens=32))
    model = Generator()
    for length, budget, cache_off in ((8, 16, False), (8, 16, False), (9, 16, False),
                                      (11, 16, False), (8, 33, False), (8, 16, True)):
        assert policy.generate(model, inputs(length), max_new_tokens=budget, disable_cache=cache_off) == "result"
        assert not hasattr(model, "_cache")
    assert [call["disable_compile"] for call in model.calls] == [False, False, True, True, True, True]
    assert model.calls[0]["cache_implementation"] == "static"
    assert model.calls[0]["compile_config"]["options"] == {"triton.cudagraphs": False}
    assert "cache_implementation" not in model.calls[2]
    assert model.calls[-1]["use_cache"] is False
    assert all("past_key_values" not in call for call in model.calls)
    evidence = policy.evidence()
    assert evidence["admitted_shapes"] == {"asr": 1, "align": 0}
    assert evidence["compile_api_calls"] == {"asr": 2, "align": 0}
    assert len(evidence["first_bucket_call_wall_s"]) == 1
    assert len(model.resets) == 6


def test_static_cache_without_compile_never_implicitly_compiles(monkeypatch):
    model = Generator()
    policy = RuntimeStrategies(ASRConfig(model_name="model", asr_cache_implementation="static"))
    policy.generate(model, inputs(), max_new_tokens=16, disable_cache=False)
    assert model.calls[0]["disable_compile"] is True
    assert model.calls[0]["cache_implementation"] == "static"
    assert "compile_config" not in model.calls[0]


def test_known_compiler_failure_retries_eager_once_and_does_not_retain_partial_cache(monkeypatch):
    compiler_config(monkeypatch)
    error = type("BackendCompilerFailed", (RuntimeError,), {"__module__": "torch._dynamo.exc"})
    model = Generator()
    original = model.generate

    def fail_compiled(**kwargs):
        result = original(**kwargs)
        if not kwargs["disable_compile"]:
            raise error("private input must not reach diagnostics")
        return result

    model.generate = fail_compiled
    policy = RuntimeStrategies(ASRConfig(model_name="model", asr_cache_implementation="static", asr_compile=True))
    assert policy.generate(model, inputs(), max_new_tokens=16, disable_cache=False) == "result"
    assert policy.generate(model, inputs(), max_new_tokens=16, disable_cache=False) == "result"
    assert [c["disable_compile"] for c in model.calls] == [False, True, True]
    assert not hasattr(model, "_cache")
    assert policy.evidence()["fallback_counts"]["asr_compile_BackendCompilerFailed"] == 1
    assert "private" not in str(policy.evidence())


def test_oom_or_model_failure_is_not_hidden_as_compiler_fallback(monkeypatch):
    compiler_config(monkeypatch)
    model = Generator()
    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("CUDA out of memory")

    model.generate = fail
    policy = RuntimeStrategies(ASRConfig(model_name="model", asr_cache_implementation="static", asr_compile=True))
    with pytest.raises(RuntimeError, match="out of memory"):
        policy.generate(model, inputs(), max_new_tokens=16, disable_cache=False)
    assert len(calls) == 1


def test_missing_compile_api_and_cpu_have_explained_default_execution(monkeypatch):
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace())
    model = Generator()
    policy = RuntimeStrategies(ASRConfig(model_name="model", asr_cache_implementation="static", asr_compile=True))
    assert policy.generate(model, inputs(), max_new_tokens=16, disable_cache=False) == "result"
    assert policy.evidence()["fallback_counts"]["asr_compile_api_unavailable"] == 1
    model.device = "cpu"
    assert policy.generate(model, inputs(), max_new_tokens=16, disable_cache=False) == "result"
    assert policy.evidence()["fallback_counts"]["asr_compile_requires_cuda"] == 1


def test_aligner_compile_has_independent_bucket_limit_and_release(monkeypatch, mock_torch):
    compiled, ordinary = [], []

    class Aligner:
        device = "cuda:0"

        def __call__(self, **kwargs):
            ordinary.append(kwargs)
            return SimpleNamespace(logits="aligned")

    def compile_model(model, **kwargs):
        assert kwargs["dynamic"] is False
        assert kwargs["options"] == {"triton.cudagraphs": False}
        def invoke(**inputs):
            compiled.append(inputs)
            return model(**inputs)
        return invoke

    monkeypatch.setattr(sys.modules["torch"], "compile", compile_model, raising=False)
    policy = RuntimeStrategies(ASRConfig(model_name="model", aligner_compile=True, compile_max_buckets=1))
    model = Aligner()
    assert [policy.align(model, inputs(n)) for n in (8, 8, 9)] == ["aligned"] * 3
    assert len(compiled) == 2 and len(ordinary) == 3
    assert policy.evidence()["admitted_shapes"] == {"asr": 0, "align": 1}
    assert policy.evidence()["fallback_counts"]["align_compile_bucket_limit"] == 1
    policy.release()
    assert policy.aligner_call is None


def test_independent_attention_request_and_optional_dependency_fallback():
    calls = []

    def unsupported(value):
        raise ImportError("flash_attn optional dependency")

    models = SimpleNamespace(model=SimpleNamespace(set_attn_implementation=calls.append),
        aligner=SimpleNamespace(set_attn_implementation=unsupported))
    policy = RuntimeStrategies(ASRConfig(model_name="model", asr_attention="sdpa", aligner_attention="flash_attention_2"))
    policy.prepare_models(models)
    assert calls == ["sdpa"]
    assert policy.evidence()["attention"] == {"asr": "sdpa", "aligner": "checkpoint_default"}
    assert policy.evidence()["fallback_counts"] == {"aligner_attention_ImportError": 1}


def test_rejected_attention_mutation_restores_observed_previous_strategy():
    config = SimpleNamespace(_attn_implementation="sdpa")
    calls = []

    def setter(value):
        calls.append(value)
        config._attn_implementation = value
        if value == "flash_attention_2":
            raise ImportError("missing optional kernel")

    models = SimpleNamespace(model=SimpleNamespace(config=config, set_attn_implementation=setter), aligner=None)
    policy = RuntimeStrategies(ASRConfig(model_name="model", asr_attention="flash_attention_2"))
    policy.prepare_models(models)
    assert config._attn_implementation == "sdpa"
    assert calls == ["flash_attention_2", "sdpa"]


def test_aligner_known_compile_failure_falls_back_once_per_signature(monkeypatch):
    failed, ordinary = [], []
    error = type("Unsupported", (RuntimeError,), {"__module__": "torch._dynamo.exc"})

    class Aligner:
        device = "cuda:0"

        def __call__(self, **kwargs):
            ordinary.append(kwargs)
            return SimpleNamespace(logits="aligned")

    def compiled(**kwargs):
        failed.append(kwargs)
        raise error("unsupported operation")

    monkeypatch.setattr(sys.modules["torch"], "compile", lambda *args, **kwargs: compiled, raising=False)
    policy = RuntimeStrategies(ASRConfig(model_name="model", aligner_compile=True))
    model = Aligner()
    assert policy.align(model, inputs()) == policy.align(model, inputs()) == "aligned"
    assert len(failed) == 1 and len(ordinary) == 2
    assert policy.evidence()["fallback_counts"] == {"align_compile_Unsupported": 1,
                                                   "align_compile_previously_failed": 1}


def test_runner_records_strategy_evidence_and_rebuilds_after_release(monkeypatch):
    runner, models = runner_for(monkeypatch, asr_cache_implementation="dynamic")
    assert runner.transcribe("/virtual/audio")
    strategy = runner.backend.strategies
    assert runner.diagnostics()["passes"][0]["runtime_strategies"]["cache_cleanup_calls"] == 4
    runner.release()
    assert runner.backend.strategies is None
    assert runner.transcribe("/virtual/audio")
    assert runner.backend.strategies is not strategy


@pytest.mark.parametrize("changes", [{"asr_compile": True}, {"asr_compile": 1},
    {"aligner_compile": "yes"}, {"asr_attention": "bogus"}, {"asr_cache_implementation": "fp8"},
    {"compile_max_buckets": 9}, {"compile_max_input_tokens": 0}, {"compile_max_output_tokens": True}])
def test_invalid_policy_rejected_before_loading(changes):
    with pytest.raises(ValueError):
        ASRConfig(model_name="model", **changes)


def test_profile_roundtrip_and_cli_freeze_runtime_strategy(database, tmp_path):
    parser = argparse.ArgumentParser()
    add_workflow_parser(parser.add_subparsers(), archive_root=str(tmp_path))
    args = parser.parse_args(["workflow", "plan", "--part-id", "1", "--device", "cpu",
        "--asr-cache-implementation", "static", "--asr-compile", "--aligner-compile",
        "--asr-attention", "sdpa", "--compile-max-buckets", "1"])
    assert _cmd_workflow(args) == 0
    repository = WorkflowRepository(database)
    profile_id = database.execute("SELECT profile_id FROM workflow_asr_profiles").fetchone()[0]
    profile = repository.profile(profile_id)
    assert profile.asr_compile and profile.aligner_compile
    assert profile.asr_cache_implementation == "static" and profile.asr_attention == "sdpa"
    assert profile.compile_max_buckets == 1
    assert repository.register_profile(replace(profile, aligner_compile=False)) != profile_id
    assert "runtime_strategies" not in AsrProfile("default", "model").canonical()
