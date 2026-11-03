"""Execution evidence is correlatable, explicit about unknowns, and content-free."""

import json
import sys
from dataclasses import replace
from types import SimpleNamespace

from bili_asr.asr.execution import clock_anchor, execution_policy, hardware_evidence
from bili_asr.asr.runner import two_pass_transcribe
from test_asr_qwen import _runner


def test_policy_reports_observed_precision_attention_cache_without_private_values(monkeypatch):
    runner, _ = _runner(monkeypatch, hotwords=("private-vocabulary",))
    models = runner._get_models()
    models.model.dtype = "torch.bfloat16"
    models.model.config = SimpleNamespace(_attn_implementation="sdpa", text_config=SimpleNamespace(use_cache=True))
    config = replace(runner.config, model_name="/secret/location", model_revision="private-token")
    policy = execution_policy(config, models, prefetch=True, prefetch_bytes=1024)
    assert policy["asr_precision"] == {"requested": "bfloat16", "resolved": "torch.bfloat16"}
    assert policy["asr_attention"]["resolved"] == "sdpa"
    assert policy["aligner_attention"]["resolved"] is None
    assert policy["generation_cache"]["checkpoint_default"] is True
    assert policy["generation_cache"]["second_pass"] == "disabled"
    assert not any(secret in json.dumps(policy) for secret in ("private-vocabulary", "secret/location", "private-token"))
    repeated = execution_policy(config, models, prefetch=True, prefetch_bytes=1024)
    assert repeated == policy
    changed = execution_policy(replace(config, second_pass_use_cache=True), models, prefetch=True, prefetch_bytes=1024)
    assert changed["runtime_config_sha256"] != policy["runtime_config_sha256"]


def test_gpu_properties_use_requested_device_without_execution_or_sync(monkeypatch):
    calls = []
    cuda = SimpleNamespace(get_device_properties=lambda device: calls.append(device) or SimpleNamespace(
        name="NVIDIA RTX 4090", total_memory=24 * 1024**3, multi_processor_count=128, major=8, minor=9))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=cuda))
    result = hardware_evidence("cuda:1")
    assert calls == ["cuda:1"]
    assert result["architecture"] == [8, 9]
    assert result["memory_bytes"] == 24 * 1024**3
    assert result["driver"] is None
    monkeypatch.delitem(sys.modules, "torch")
    assert hardware_evidence("cuda")["status"] == "unobserved"


def test_two_pass_evidence_has_separate_clock_domains_and_same_stable_policy(monkeypatch, tmp_path):
    runner, _ = _runner(monkeypatch, text="今天。", hotwords=("今天",))
    source = tmp_path / "audio"
    source.write_bytes(b"audio identity")
    two_pass_transcribe(runner, str(source), paired_subtitle_text=None)
    diagnostics = runner.diagnostics()
    first, second = diagnostics["passes"]
    assert first["clock"]["domain_id"] != second["clock"]["domain_id"]
    assert first["clock"]["process_id"] == second["clock"]["process_id"]
    assert [first["pass_kind"], second["pass_kind"]] == ["first", "hotword_second"]
    assert first["execution_policy"] == second["execution_policy"] == diagnostics["execution_policy"]
    assert second["resource_observation"]["gpu_sampled_peak_bytes"] is None
    for record in (first, second):
        assert record["clock"]["anchor_utc_epoch_ns"] > 0
        assert record["clock"]["anchor_uncertainty_s"] >= 0
        assert all(event["measurement"] == "wall" for event in record["trace"])


def test_clock_anchor_respects_pass_origin():
    anchor = clock_anchor(monotonic=12.5)
    assert anchor["origin_s"] == 12.5
    assert anchor["anchor_perf_counter_s"] >= anchor["origin_s"]
