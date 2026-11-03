"""Batch admission and result identity without CUDA or checkpoint downloads."""

import argparse
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from bili_asr.asr.config import ASRConfig
from bili_asr.asr.runner import ASRRunner, two_pass_transcribe
from bili_asr.cli.workflow import _cmd_workflow, add_workflow_parser
from bili_asr.storage import AsrProfile, WorkflowRepository
from test_asr_quality_foundation import database as database  # Shared archive fixture.
from test_asr_qwen import _Inputs, _runner, _units


class Processor:
    def __init__(self):
        self.requests = []
        self.feature_frames = None
        self.invalid_result = False

    def apply_transcription_request(self, audio, language=None, prompt=None):
        arrays = audio if isinstance(audio, list) else [audio]
        self.requests.append((arrays, language, prompt))
        frames = [self.feature_frames(int(a[0])) if self.feature_frames else 100 for a in arrays]
        mask = np.zeros((len(arrays), max(frames)), dtype=np.int64)
        for row, count in enumerate(frames):
            mask[row, :count] = 1
        return _Inputs(input_ids=np.array([[int(a[0])] * 8 for a in arrays]), input_features_mask=mask)

    def decode(self, tokens, return_format):
        texts = [f"词{int(row[0])}。" if int(row[0]) else "" for row in tokens]
        if self.invalid_result and len(texts) > 1:
            texts.pop()
        if return_format == "transcription_only":
            return texts
        return [{"language": "Chinese" if int(row[0]) % 2 else "English"} for row in tokens]


class Model:
    device = "cpu"
    dtype = None
    generation_config = SimpleNamespace(eos_token_id=2)

    def __init__(self):
        self.calls = []

    def generate(self, **inputs):
        self.calls.append(inputs)
        # Distinct output per item and trailing padding after EOS.
        ids = inputs["input_ids"]
        suffix = np.array([[int(row[0]), 2, 0, 0] for row in ids])
        return np.concatenate([ids, suffix], axis=1)


class AlignerProcessor:
    def __init__(self):
        self.requests = []
        self.invalid_result = False

    def prepare_forced_aligner_inputs(self, audio, transcript, language):
        texts = transcript if isinstance(transcript, list) else [transcript]
        self.requests.append((texts, language))
        return _Inputs(input_ids=np.zeros((len(texts), 8), dtype=np.int64)), texts

    def decode_forced_alignment(self, *, word_lists, **kwargs):
        result = [_units(text, step=0.2) for text in word_lists]
        return result[:-1] if self.invalid_result and len(result) > 1 else result


class Aligner:
    device = "cpu"
    dtype = None
    config = SimpleNamespace(timestamp_token_id=10)

    def __init__(self):
        self.calls = []

    def __call__(self, **inputs):
        self.calls.append(inputs)
        return SimpleNamespace(logits=inputs["input_ids"])


def runner_for(monkeypatch, lengths=(1, 1, 1, 1), identifiers=(11, 12, 13, 14), **config):
    base, _ = _runner(monkeypatch, **config)
    chunks = []
    offset = 0.0
    for seconds, marker in zip(lengths, identifiers, strict=True):
        chunks.append((np.full(int(16000 * seconds), marker, dtype=np.float32), offset))
        offset += seconds
    monkeypatch.setattr("bili_asr.asr.audio._split_audio", lambda *args: chunks)
    models = SimpleNamespace(processor=Processor(), model=Model(),
                             aligner_processor=AlignerProcessor(), aligner=Aligner())
    return ASRRunner(base.config, model_factory=lambda **kwargs: models), models


def test_native_batches_restore_offsets_text_language_and_independent_stage_caps(monkeypatch):
    serial, _ = runner_for(monkeypatch)
    expected = serial.transcribe("/virtual/audio")
    runner, models = runner_for(monkeypatch, asr_batch_size=4, aligner_batch_size=2)
    assert runner.transcribe("/virtual/audio") == expected
    assert [len(call["input_ids"]) for call in models.model.calls] == [4]
    assert [len(call["input_ids"]) for call in models.aligner.calls] == [2, 2]
    assert models.aligner_processor.requests == [(["词11。", "词12。"], ["Chinese", "English"]),
                                                  (["词13。", "词14。"], ["Chinese", "English"])]
    report = runner.diagnostics()["passes"][0]
    assert [c["start_s"] for c in report["chunks"]] == [0, 1, 2, 3]
    assert all(c["generation"]["generated_tokens"] == 2 for c in report["chunks"])
    assert all(c["generation"]["ended_with_eos"] for c in report["chunks"])
    assert report["batching"]["pending_windows"] == report["batching"]["running_batches"] == 1
    assert report["batching"]["max_wait_s"] == 0
    assert report["batching"]["current"] == dict.fromkeys(report["batching"]["limits"], 0)
    assert report["batching"]["peaks"] == report["batching"]["limits"]
    assert report["batching"]["fallback_counts"] == {}
    assert all(e["measurement"] == "wall" for e in report["trace"])
    assert "decode_s" not in report["chunks"][0]  # A batch wall time is counted once.


@pytest.mark.parametrize("option,value,reason", [
    ("batch_max_audio_seconds", 1.1, "window_capacity_or_length_bucket"),
    ("batch_max_input_bytes", 1, "single_chunk_exceeds_batch_reservation"),
    ("batch_max_tokens", 300, "output_token_budget"),
])
def test_capacity_limits_fall_back_without_truncating_chunks(monkeypatch, option, value, reason):
    runner, models = runner_for(monkeypatch, asr_batch_size=4, **{option: value})
    result = runner.transcribe("/virtual/audio")
    assert "".join(item["text"] for item in result).replace(" ", "") == "词11。词12。词13。词14。"
    assert [len(call["input_ids"]) for call in models.model.calls] == [1, 1, 1, 1]
    assert runner.diagnostics()["passes"][0]["batching"]["fallback_counts"][reason] > 0


def test_short_tail_has_own_batch_and_heterogeneous_token_budgets_preserve_serial_formula(monkeypatch):
    runner, models = runner_for(monkeypatch, lengths=(3, 3, 0.1), identifiers=(11, 12, 13),
                                asr_batch_size=3, aligner_batch_size=3, min_new_tokens=1)
    models.processor.feature_frames = lambda marker: marker * 100
    assert runner.transcribe("/virtual/audio")
    assert [call["max_new_tokens"] for call in models.model.calls] == [88, 96, 104]
    report = runner.diagnostics()["passes"][0]
    assert report["chunks"][-1]["end_s"] == 6.1
    assert report["batching"]["fallback_counts"]["heterogeneous_token_budgets"] == 1
    assert report["batching"]["fallback_counts"]["window_capacity_or_length_bucket"] == 1


@pytest.mark.parametrize("which", ["processor", "aligner_processor"])
def test_partial_batch_result_rejects_task_and_leaves_no_publishable_segments(monkeypatch, which):
    runner, models = runner_for(monkeypatch, asr_batch_size=2, aligner_batch_size=2)
    assert runner.transcribe("/virtual/audio")
    getattr(models, which).invalid_result = True
    with pytest.raises(ValueError, match="result count mismatch"):
        runner.transcribe("/virtual/other")
    assert runner._last_transcribed_segments is None
    assert runner.diagnostics()["passes"][0]["completed"] is False
    evidence = runner.diagnostics()["passes"][0]["batching"]
    assert evidence["current"] == dict.fromkeys(evidence["limits"], 0)


def test_empty_item_is_not_aligned_or_written_into_neighbor(monkeypatch):
    runner, models = runner_for(monkeypatch, identifiers=(11, 0, 13, 14), asr_batch_size=4, aligner_batch_size=2)
    runner.transcribe("/virtual/audio")
    texts = [text for request in models.aligner_processor.requests for text in request[0]]
    assert texts == ["词11。", "词13。", "词14。"]
    assert runner.diagnostics()["passes"][0]["chunks"][1]["flags"] == ["empty-output"]


@pytest.mark.parametrize("observed,reason", [(None, "prepared_input_size_unknown"),
                                           (100_000_000, "prepared_input_budget")])
def test_measured_input_refusal_discards_batch_and_uses_singletons(monkeypatch, observed, reason):
    monkeypatch.setattr("bili_asr.asr.batching.prepared_input_bytes", lambda inputs: observed)
    runner, models = runner_for(monkeypatch, asr_batch_size=4, aligner_batch_size=4)
    assert runner.transcribe("/virtual/audio")
    assert all(len(call["input_ids"]) == 1 for call in models.model.calls + models.aligner.calls)
    assert runner.diagnostics()["passes"][0]["batching"]["fallback_counts"][reason] == 2


def test_backend_capability_refusal_and_prefetch_have_bounded_serial_fallback(monkeypatch):
    runner, models = runner_for(monkeypatch, asr_batch_size=4, aligner_batch_size=4)
    runner.backend.capabilities = replace(runner.backend.capabilities,
        native_asr_batch=False, native_alignment_batch=False)
    runner.configure_prefetch(enabled=True, max_bytes=64 * 1024**2)
    assert runner.transcribe("/virtual/audio")
    assert all(len(call["input_ids"]) == 1 for call in models.model.calls + models.aligner.calls)
    report = runner.diagnostics()["passes"][0]
    assert report["prefetch"]["processor_clone_attempts"] == 0
    assert report["prefetch"]["fallback"] == "batch_scheduler_owns_preparation"
    assert report["batching"]["fallback_counts"] == {
        "backend_asr_batch_unsupported": 1, "backend_alignment_batch_unsupported": 1}


def test_two_pass_alignment_uses_each_pass_text_and_cache_policy(monkeypatch):
    runner, models = runner_for(monkeypatch, asr_batch_size=4, aligner_batch_size=4, hotwords=("词",))
    original = models.processor.decode

    def decode(tokens, return_format):
        result = original(tokens, return_format)
        return [text.replace("词", "新词") for text in result] if len(models.model.calls) == 2 and return_format == "transcription_only" else result

    models.processor.decode = decode
    result = two_pass_transcribe(runner, "/virtual/audio", paired_subtitle_text="词11")
    assert "新词11。" in "".join(item["text"] for item in result).replace(" ", "")
    assert models.aligner_processor.requests[0][0][0] == "词11。"
    assert models.aligner_processor.requests[1][0][0] == "新词11。"
    assert "use_cache" not in models.model.calls[0]
    assert models.model.calls[1]["use_cache"] is False
    reports = runner.diagnostics()["passes"]
    assert len(reports) == 2 and all(r["batching"]["batches"][0]["batch_index"] == 0 for r in reports)


@pytest.mark.parametrize("name,value", [("asr_batch_size", True), ("asr_batch_size", 9),
    ("aligner_batch_size", 0), ("batch_max_audio_seconds", float("nan")),
    ("batch_max_audio_seconds", 1801), ("batch_max_input_bytes", 0), ("batch_max_tokens", 1.5)])
def test_invalid_batch_policy_rejected_before_model_load(name, value):
    with pytest.raises(ValueError):
        ASRConfig(model_name="model", **{name: value})


def test_default_profile_canonical_bytes_preserve_historical_digest(database):
    base = AsrProfile("stable", "model")
    # Pinned from the pre-batching main profile canonical representation.
    assert hashlib.sha256(base.canonical().encode()).hexdigest() == "5251f6e6e4b3c61f45c424e4eb762e7ee5ea6ab74b96876f9dc04d5904855d83"
    assert "batching" not in json.loads(base.canonical())
    tuned = replace(base, asr_batch_size=4, aligner_batch_size=2, batch_max_audio_seconds=120)
    repository = WorkflowRepository(database)
    first, second = repository.register_profile(base), repository.register_profile(tuned)
    assert first != second
    assert repository.profile(second) == tuned
    assert repository.profile(first) == base


def test_cli_batch_policy_freezes_in_profile(database, tmp_path):
    parser = argparse.ArgumentParser()
    add_workflow_parser(parser.add_subparsers(), archive_root=str(tmp_path))
    args = parser.parse_args(["workflow", "plan", "--part-id", "1", "--device", "cpu",
        "--asr-batch-size", "4", "--aligner-batch-size", "2", "--batch-max-audio-seconds", "60",
        "--batch-max-input-bytes", "100000000", "--batch-max-tokens", "4000"])
    assert _cmd_workflow(args) == 0
    profile_id = database.execute("SELECT profile_id FROM workflow_asr_profiles").fetchone()[0]
    config = WorkflowRepository(database).profile(profile_id).asr_config()
    assert (config.asr_batch_size, config.aligner_batch_size) == (4, 2)
    assert (config.batch_max_audio_seconds, config.batch_max_input_bytes, config.batch_max_tokens) == (60, 100000000, 4000)


def test_dispatch_is_immediate_and_does_not_build_next_window_before_current_is_consumed(monkeypatch):
    from bili_asr.asr.batching import BatchExecutor

    runner, models = runner_for(monkeypatch, asr_batch_size=2, aligner_batch_size=2)
    report = {}
    executor = BatchExecutor(runner, models, report, minimum_samples=1600, bust_cache=False)
    chunks = [(np.full(16000, marker, dtype=np.float32), index) for index, marker in enumerate((11, 12, 13))]
    original = models.model.generate
    observed = []
    def generate(**kwargs):
        observed.append(dict(report["batching"]["current"]))
        return original(**kwargs)
    models.model.generate = generate
    # There is no batch fill timer or asynchronous queue to wait for capacity.
    monkeypatch.setattr("time.sleep", lambda *_: pytest.fail("same-task batch must not wait to fill"))
    pending = executor.run(chunks)
    first = next(pending)
    assert first["chunk_index"] == 0
    assert len(models.model.calls) == 1 and len(models.processor.requests) == 1
    assert report["batching"]["current"] == {"active_windows": 1, "pending_windows": 0,
        "running_batches": 0, "build_workers": 0}
    assert observed == [{"active_windows": 1, "pending_windows": 0, "running_batches": 1, "build_workers": 0}]
    pending.close()
    assert report["batching"]["current"] == dict.fromkeys(report["batching"]["limits"], 0)
    assert len(models.model.calls) == 1  # Unconsumed tail never prepared or submitted.
    admission = report["batching"]["resource_admission"]
    assert admission["requested_reservation_peak_bytes"] == 16000 * 2 * 4 * 64
    assert admission["total_gpu_vram"] == "not_enforced" and "other_processes" in admission["excluded"]


def test_single_available_chunk_runs_immediately_when_batch_limit_is_larger(monkeypatch):
    runner, models = runner_for(monkeypatch, lengths=(1,), identifiers=(11,), asr_batch_size=8, aligner_batch_size=8)
    monkeypatch.setattr("time.sleep", lambda *_: pytest.fail("must not wait for a batch peer"))
    assert runner.transcribe("/virtual/audio")
    assert len(models.model.calls) == len(models.aligner.calls) == 1
    assert runner.diagnostics()["passes"][0]["batching"]["dispatch"]["batch_fill_wait_s"] == 0
