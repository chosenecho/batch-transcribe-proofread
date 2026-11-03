"""The Qwen3-ASR boundary: its two pure steps, its contract, and its products.

The engine's own output shapes are exercised on the host (see the plan's §13.5 measurement);
what is pinned here is everything that must hold without a GPU: the chunker's tiling promise, the
mark threading that keeps "cue text is the recognised text" true, the cue state machine that
survived from the FunASR era, the runner's lazy/reuse/retry contract, and the redaction of the
provenance slots.

**Retired by name when the engine changed** (plan §13.7) — these files described FunASR's shapes and
were removed rather than adapted, and each assertion that still describes the product was carried
here first:

* ``tests/test_asr_reproducibility.py`` (1843 lines, ``fake_funasr``) — FunASR's ``AutoModel`` kwargs,
  its token-timestamp normalization, its ``normalize_result`` and its VAD configuration.  Surviving
  and carried here: the runner's construction/attempt counters and their bounds, the dependency and
  model error taxonomy, provenance stability and redaction, and the offline guard.
* ``tests/test_asr_format.py`` — ``_RICH_TAG`` cleanup, ``normalize_result``'s fallback to a
  zero-length segment, and the token-shaped cue shaping.  Surviving and carried here: the SRT/TXT
  writers and the SRT clock.
* ``tests/test_asr_cues.py`` — cue shaping from FunASR token timings, including the per-cue confidence
  it carried.  Surviving and carried here: the cue rules, now driven by aligned pieces.
"""

from __future__ import annotations

import builtins
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

import bili_asr.asr.alignment as _module_asr_alignment
import bili_asr.asr.audio as _module_asr_audio
import bili_asr.asr.config as _module_asr_config
import bili_asr.asr.constants as _module_asr_constants
import bili_asr.asr.errors as _module_asr_errors
import bili_asr.asr.runner as _module_asr_runner
from bili_asr import asr
from bili_asr.asr.provenance import apply_provenance_evidence


def test_missing_asr_provenance_uses_named_language_fallback():
    class Runner:
        def provenance(self):
            return {}

    entry = {}
    apply_provenance_evidence(entry, Runner())
    assert entry["language"] == _module_asr_constants.DEFAULT_TRANSCRIPT_LANGUAGE == "und"


# ---------------------------------------------------------------------------------------
# Fakes.  The boundary talks to exactly four objects, so the fakes are four small ones — and they
# are plain Python, which keeps this suite runnable on a host with no torch.  numpy and soundfile,
# by contrast, are real test dependencies: the audio path calls them, so they are imported directly.
# ---------------------------------------------------------------------------------------


class _Shape:
    def __init__(self, tokens: "_Tokens") -> None:
        self._tokens = tokens

    def __getitem__(self, index: int) -> int:
        return self._tokens.shape[index]


class _Tokens:
    """Just enough of a tensor: ``[:, n:]``, ``.shape``, ``.sum(-1).max()`` and ``float()``."""

    def __init__(self, length: int, shape: tuple[int, int] | None = None) -> None:
        self.length = length
        self.shape = shape or (1, length)
        self.nbytes = length * 8

    def __getitem__(self, key: tuple) -> "_Tokens":
        start = key[1].start or 0
        return _Tokens(self.length - start, (1, self.length - start))

    def __float__(self) -> float:
        # ``_transcribe_chunk`` sizes the token budget from the number of valid mel frames.
        return float(self.length)

    def sum(self, *args, **kwargs) -> "_Tokens":
        return self

    def max(self, *args, **kwargs) -> "_Tokens":
        return self


class _Inputs(dict):
    def to(self, *args, **kwargs) -> "_Inputs":
        return self


class _FakeProcessor:
    def __init__(self, text: str, language: str = "Chinese") -> None:
        self.text = text
        self.language = language
        self.requests: list[dict] = []

    def apply_transcription_request(self, audio=None, language=None, prompt=None) -> _Inputs:
        self.requests.append({"audio": audio, "language": language, "prompt": prompt})
        return _Inputs({
            "input_ids": _Tokens(8),
            "input_features_mask": _Tokens(100 * 10, (1, 100 * 10)),
        })

    def decode(self, tokens, return_format="raw", **kwargs):
        if return_format == "raw":
            return [f"language {self.language}<asr_text>{self.text}<|im_end|>"]
        if return_format == "parsed":
            return [{"language": self.language, "transcription": self.text}]
        return [self.text]


class _FakeModel:
    device = "cpu"
    dtype = None

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate(self, **inputs) -> _Tokens:
        self.calls.append(inputs)
        return _Tokens(12)


class _AlignInputs(dict):
    def to(self, *args, **kwargs) -> "_AlignInputs":
        return self


class _FakeAlignerProcessor:
    def __init__(self, units: list[dict]) -> None:
        self.units = units
        self.seen: list[dict] = []

    def prepare_forced_aligner_inputs(self, audio=None, transcript=None, language=None):
        self.seen.append({"audio": audio, "transcript": transcript, "language": language})
        return _AlignInputs({"input_ids": _Tokens(16)}), [[u["text"] for u in self.units]]

    def decode_forced_alignment(self, **kwargs):
        return [list(self.units)]


class _FakeAligner:
    device = "cpu"
    dtype = None
    config = type("Config", (), {"timestamp_token_id": 151705})()

    def __call__(self, **inputs):
        return type("Out", (), {"logits": _Tokens(16)})()


class _FakeModelSet:
    def __init__(self, text: str, units: list[dict]) -> None:
        self.processor = _FakeProcessor(text)
        self.model = _FakeModel()
        self.aligner_processor = _FakeAlignerProcessor(units)
        self.aligner = _FakeAligner()


def _units(text: str, step: float = 0.4) -> list[dict]:
    """One timed unit per character of ``text`` (marks excluded, as the aligner really returns)."""

    return [
        {"text": char, "start_time": index * step, "end_time": index * step + step - 0.05}
        for index, char in enumerate(text)
        if char not in "。！？!?，、；：,;: "
    ]


def _pieces(text: str, step: float = 0.4) -> list[dict]:
    return _module_asr_alignment._thread_text(text, [
        {"text": u["text"], "start_time": u["start_time"], "end_time": u["end_time"]}
        for u in _units(text, step)
    ])


# ---------------------------------------------------------------------------------------
# The chunker: it must tile the input, never overlap and never leave a gap.
# ---------------------------------------------------------------------------------------


def test_split_audio_tiles_the_input_exactly() -> None:
    import numpy as np
    samples = np.random.default_rng(7).standard_normal(_module_asr_constants.SAMPLE_RATE * 10).astype("float32")
    chunks = _module_asr_audio._split_audio(samples, _module_asr_constants.SAMPLE_RATE, 3.0)
    assert len(chunks) >= 4
    assert sum(len(chunk) for chunk, _ in chunks) == len(samples)
    offsets = [offset for _, offset in chunks]
    assert offsets == sorted(offsets)
    assert offsets[0] == 0.0


def test_split_audio_keeps_a_short_recording_whole() -> None:
    import numpy as np
    samples = np.zeros(_module_asr_constants.SAMPLE_RATE * 2, dtype="float32")
    chunks = _module_asr_audio._split_audio(samples, _module_asr_constants.SAMPLE_RATE, 180.0)
    assert len(chunks) == 1
    assert chunks[0][1] == 0.0
    assert len(chunks[0][0]) == len(samples)


def test_split_audio_keeps_a_degenerate_tail_short_and_still_tiles() -> None:
    """A tail shorter than the aligner's minimum stays short here; the runner pads it.

    Measured defect this pins: with the search window allowed to reach back to ``start``, the
    boundary landed on the window's first sample and the clamp walked one sample per chunk — a
    3.01 s recording came back as 161 chunks.
    """

    import numpy as np
    samples = np.zeros(int(_module_asr_constants.SAMPLE_RATE * 3.01), dtype="float32")
    chunks = _module_asr_audio._split_audio(samples, _module_asr_constants.SAMPLE_RATE, 3.0)
    assert len(chunks) == 2, chunks
    assert sum(len(chunk) for chunk, _ in chunks) == len(samples)
    assert len(chunks[-1][0]) < int(_module_asr_constants._CHUNK_MIN_SECONDS * _module_asr_constants.SAMPLE_RATE)


def test_split_audio_never_returns_a_degenerate_chunk() -> None:
    import numpy as np
    samples = np.random.default_rng(3).standard_normal(_module_asr_constants.SAMPLE_RATE * 4).astype("float32")
    chunks = _module_asr_audio._split_audio(samples, _module_asr_constants.SAMPLE_RATE, 1.0)
    floor = int(_module_asr_constants._CHUNK_MIN_WINDOW_MS / 1000.0 * _module_asr_constants.SAMPLE_RATE)
    assert all(len(chunk) >= floor for chunk, _ in chunks[:-1]), [len(c) for c, _ in chunks]


def test_split_audio_of_nothing_is_no_chunks() -> None:
    import numpy as np
    assert _module_asr_audio._split_audio(np.zeros(0, dtype="float32"), _module_asr_constants.SAMPLE_RATE, 180.0) == []


# ---------------------------------------------------------------------------------------
# Mark threading: what keeps "cue text is the recognised text" true.
# ---------------------------------------------------------------------------------------


def test_threading_keeps_every_character_once_in_order() -> None:
    text = "拟象论啊，拟象论。"
    assert "".join(piece["text"] for piece in _pieces(text)) == text


def test_threading_gives_a_mark_the_instant_beside_it() -> None:
    pieces = _pieces("今天讲两件事。")
    mark = pieces[-1]
    assert mark["text"] == "。"
    assert mark["start"] == mark["end"] == pieces[-2]["end"]


def test_threading_handles_latin_words() -> None:
    text = "International Employment Matters Tribunal 是这个"
    units = [
        {"text": "International", "start_time": 0.0, "end_time": 0.6},
        {"text": "Employment", "start_time": 0.6, "end_time": 1.1},
        {"text": "Matters", "start_time": 1.1, "end_time": 1.5},
        {"text": "Tribunal", "start_time": 1.5, "end_time": 2.0},
        {"text": "是", "start_time": 2.1, "end_time": 2.3},
        {"text": "这", "start_time": 2.3, "end_time": 2.5},
        {"text": "个", "start_time": 2.5, "end_time": 2.7},
    ]
    pieces = _module_asr_alignment._thread_text(text, units)
    assert "".join(p["text"] for p in pieces) == text
    assert [p["text"] for p in pieces if p["text"] == "International"] == ["International"]


def test_threading_keeps_a_unit_the_text_does_not_account_for() -> None:
    pieces = _module_asr_alignment._thread_text("好", [{"text": "好", "start_time": 0.0, "end_time": 0.2},
                                     {"text": "嗯", "start_time": 0.2, "end_time": 0.4}])
    assert "".join(p["text"] for p in pieces) == "好嗯"


# ---------------------------------------------------------------------------------------
# The cue state machine (survivors of the FunASR-era shaper).
# ---------------------------------------------------------------------------------------


def test_a_sentence_mark_closes_a_cue() -> None:
    cues = _module_asr_alignment._aligned_cues(_pieces("今天讲两件事。明天我们接着讲第三件事。"))
    assert [cue["text"] for cue in cues] == ["今天讲两件事。", "明天我们接着讲第三件事。"]
    assert "".join(cue["text"] for cue in cues) == "今天讲两件事。明天我们接着讲第三件事。"


def test_the_ceiling_closes_a_cue_and_the_tail_is_kept() -> None:
    """The ceiling is a readability target, not a guillotine: the tail stays with the cue before it.

    This is the FunASR-era rule verbatim — an undersized remainder is absorbed rather than published
    as a one-character subtitle line — so the assertion is about text preservation, and about the
    absorbed tail not being dropped.
    """

    long_text = "一" * _module_asr_constants._CUE_MAX_CHARS + "尾"
    cues = _module_asr_alignment._aligned_cues(_pieces(long_text))
    assert "".join(cue["text"] for cue in cues) == long_text
    assert [cue["text"][-1] for cue in cues] == ["尾"] or len(cues) == 1


def test_a_pause_closes_only_a_cue_that_can_stand_alone() -> None:
    pieces = _pieces("短短") + [
        {"text": "之后", "start": 4.0, "end": 4.4},
    ]
    cues = _module_asr_alignment._aligned_cues(pieces)
    assert len(cues) == 1, "a fragment separated by a pause is absorbed, not split"


def test_a_fragment_is_absorbed_by_the_cue_before_it() -> None:
    cues = _module_asr_alignment._aligned_cues(_pieces("这是一句足够长的话。好。"))
    assert len(cues) == 1
    assert cues[0]["text"].endswith("好。")


def test_a_mark_at_a_boundary_is_handed_back() -> None:
    pieces = [
        {"text": "一", "start": 0.0, "end": 0.4},
        {"text": "样", "start": 0.4, "end": 0.8},
        {"text": "长", "start": 0.8, "end": 1.2},
        {"text": "的", "start": 1.2, "end": 1.6},
        {"text": "句", "start": 1.6, "end": 2.0},
        {"text": "子", "start": 2.0, "end": 2.4},
        {"text": "，", "start": 2.4, "end": 2.4},
        {"text": "新", "start": 5.0, "end": 5.4},
        {"text": "句", "start": 5.4, "end": 5.8},
        {"text": "开", "start": 5.8, "end": 6.2},
        {"text": "始", "start": 6.2, "end": 6.6},
        {"text": "了", "start": 6.6, "end": 7.0},
        {"text": "吧", "start": 7.0, "end": 7.4},
    ]
    cues = _module_asr_alignment._aligned_cues(pieces)
    assert cues[0]["text"] == "一样长的句子，"
    assert cues[1]["text"] == "新句开始了吧"


def test_latin_words_keep_their_separator_when_a_fragment_is_absorbed() -> None:
    assert _module_asr_alignment._join_text("Idea", "Moments") == "Idea Moments"
    assert _module_asr_alignment._join_text("思想", "观念") == "思想观念"


def test_latin_tokens_keep_their_separator_inside_a_cue() -> None:
    """Aligned word pieces may omit spaces even though each belongs to one cue."""

    pieces = [
        {"text": "International", "start": 0.0, "end": 0.6},
        {"text": "Employment", "start": 0.6, "end": 1.2},
        {"text": "Matters", "start": 1.2, "end": 1.8},
    ]
    cues = _module_asr_alignment._aligned_cues(pieces)

    assert [cue["text"] for cue in cues] == ["International Employment Matters"]


def test_cues_are_monotonic_and_carry_the_recognised_text() -> None:
    text = "第一句话说完了。第二句话也说完了吧。"
    cues = _module_asr_alignment._aligned_cues(_pieces(text))
    assert "".join(cue["text"] for cue in cues) == text
    assert all(cues[i]["end"] <= cues[i + 1]["start"] for i in range(len(cues) - 1))


# ---------------------------------------------------------------------------------------
# Configuration guards.
# ---------------------------------------------------------------------------------------


def test_config_refuses_a_declaration_that_contradicts_a_hub_level_model() -> None:
    with pytest.raises(ValueError):
        _module_asr_config.ASRConfig(model_name="Qwen/Qwen3-ASR-1.7B-hf", model_id="Other/Thing")


def test_config_accepts_a_truthful_declaration_beside_a_real_local_path(
    tmp_path, monkeypatch
) -> None:
    """The relative checkpoint form the README documents is not a competing identity.

    Existence is what separates it from a hub id, so the test gives it a directory to exist in — the
    rule probes the filesystem exactly as the loader does.
    """

    (tmp_path / "models" / "Qwen3-ASR-1.7B-hf").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    config = _module_asr_config.ASRConfig(model_name="models/Qwen3-ASR-1.7B-hf",
                           model_id="Qwen/Qwen3-ASR-1.7B-hf")
    assert config.model_id == "Qwen/Qwen3-ASR-1.7B-hf"
    assert config.model_name == "models/Qwen3-ASR-1.7B-hf"


@pytest.mark.parametrize("value", [0, -1, "180"])
def test_config_refuses_a_bad_chunk_seconds(value: object) -> None:
    with pytest.raises(ValueError):
        _module_asr_config.ASRConfig(model_name="Qwen/Qwen3-ASR-1.7B-hf", chunk_seconds=value)  # type: ignore[arg-type]


def test_config_refuses_a_hostile_local_source() -> None:
    with pytest.raises(ValueError):
        _module_asr_config.ASRConfig(model_name="Qwen/Qwen3-ASR-1.7B-hf", local_source="https://host/x")


# ---------------------------------------------------------------------------------------
# The runner contract: lazy, one construction for N items, bounded retries, monotonic counters.
# ---------------------------------------------------------------------------------------


def _runner(monkeypatch, text: str = "今天讲两件事。明天我们接着讲第三件事。", **kwargs):
    """A runner over fake models and fake audio.

    The audio read is patched because these tests describe the runner's contract, not the decoder
    libraries.  numpy and soundfile are nonetheless imported for real — they are declared test
    dependencies, not an optional extra — so a host without them fails here rather than skipping.
    """

    import numpy as np
    import soundfile as sf
    samples = np.zeros(_module_asr_constants.SAMPLE_RATE * 3, dtype="float32")
    monkeypatch.setattr(sf, "read", lambda *args, **kwargs_: (samples, _module_asr_constants.SAMPLE_RATE))
    monkeypatch.setattr(sf, "write", lambda *args, **kwargs_: None)

    units = _units(text)
    builds: list[dict] = []

    def factory(**factory_kwargs):
        builds.append(factory_kwargs)
        return _FakeModelSet(text, units)

    config = _module_asr_config.ASRConfig(model_name="Qwen/Qwen3-ASR-1.7B-hf", device="cpu", **kwargs)
    return _module_asr_runner.ASRRunner(config, model_factory=factory), builds


def test_runner_diagnostics_capture_actual_chunks_and_model_reuse(monkeypatch):
    runner, _ = _runner(monkeypatch, text="今天。")
    runner.transcribe("/nonexistent/one.wav")
    report = runner.diagnostics()["passes"][0]
    assert report["completed"] is True
    assert report["decoded_s"] == 3
    assert report["model_reused"] is False
    assert report["chunks"][0]["text"] == "今天。"
    assert report["chunks"][0]["generation"]["max_new_tokens"] == 256
    assert report["chunks"][0]["alignment"]["aligned_unit_union_s"] > 0
    assert {"model_load", "audio_prepare", "split", "decode", "align", "total"} <= report["timings_s"].keys()
    runner.transcribe("/nonexistent/two.wav")
    assert runner.diagnostics()["passes"][0]["model_reused"] is True


def test_two_pass_diagnostics_retain_both_passes_and_configured_cache(monkeypatch):
    runner, _ = _runner(monkeypatch, text="今天。", hotwords=("今天",), second_pass_use_cache=True)
    _module_asr_runner.two_pass_transcribe(runner, "/nonexistent/two.wav", paired_subtitle_text=None)
    reports = runner.diagnostics()["passes"]
    assert len(reports) == 2
    assert [item["model_reused"] for item in reports] == [False, True]
    assert "use_cache" not in runner._get_models().model.calls[1]


def test_runner_records_empty_chunk_without_fabricated_alignment(monkeypatch):
    runner, _ = _runner(monkeypatch, text="")
    assert runner.transcribe("/nonexistent/empty.wav") == []
    diagnostics = runner.diagnostics()
    assert diagnostics["quality"]["flags"] == ["empty-output", "span-coverage-short"]
    assert "alignment" not in diagnostics["passes"][0]["chunks"][0]


def test_the_runner_is_lazy_and_constructs_one_model_set_for_many_items(monkeypatch) -> None:
    runner, builds = _runner(monkeypatch)
    assert runner.model_constructions == 0, "no model may be built before the first use"
    first = runner.transcribe("/nonexistent/one.wav")
    second = runner.transcribe("/nonexistent/two.wav")
    assert builds and len(builds) == 1
    assert runner.model_constructions == 1
    assert runner.model_load_attempts == 1
    assert first == second, "the same audio must produce the same cues"


def test_processors_receive_normalized_samples_without_optional_file_loader(tmp_path) -> None:
    """Real audio reaches both models without Transformers loading a file again."""
    import numpy as np
    import soundfile as sf

    samples = np.linspace(-0.25, 0.25, 16000, dtype=np.float32)
    path = tmp_path / "input.wav"
    sf.write(path, np.column_stack((samples, samples)), 8000, subtype="FLOAT")
    models = _FakeModelSet("今天。", _units("今天。"))
    runner = _module_asr_runner.ASRRunner(
        _module_asr_config.ASRConfig(model_name="local", device="cpu"),
        model_factory=lambda **kwargs: models,
    )
    assert runner.transcribe(str(path))
    decoded = models.processor.requests[0]["audio"]
    aligned = models.aligner_processor.seen[0]["audio"]
    assert isinstance(decoded, np.ndarray) and decoded.dtype == np.float32
    assert decoded.ndim == 1 and len(decoded) == 32000
    np.testing.assert_array_equal(decoded, aligned)
    assert np.max(np.abs(decoded)) > 0.2


def test_a_failed_load_pays_an_attempt_and_no_construction(monkeypatch) -> None:
    import numpy as np
    import soundfile as sf

    def factory(**kwargs):
        raise RuntimeError("boom")

    runner = _module_asr_runner.ASRRunner(
        _module_asr_config.ASRConfig(model_name="Qwen/Qwen3-ASR-1.7B-hf", device="cpu"), model_factory=factory
    )
    for _ in range(_module_asr_constants.MAX_MODEL_LOAD_ATTEMPTS):
        with pytest.raises(_module_asr_errors.ASRModelError):
            runner.transcribe("/nonexistent/one.wav")
    assert runner.model_constructions == 0
    assert runner.model_load_attempts == _module_asr_constants.MAX_MODEL_LOAD_ATTEMPTS
    with pytest.raises(_module_asr_errors.ASRModelError):
        runner.transcribe("/nonexistent/one.wav")
    assert runner.model_load_attempts == _module_asr_constants.MAX_MODEL_LOAD_ATTEMPTS, "the cap is not spent again"


def test_release_makes_a_rebuild_pay_a_second_construction(monkeypatch) -> None:
    runner, builds = _runner(monkeypatch)
    runner.transcribe("/nonexistent/one.wav")
    runner.release()
    runner.transcribe("/nonexistent/two.wav")
    assert len(builds) == 2
    assert runner.model_constructions == 2, "the counter is monotonic and is not reset by release"


def test_the_runner_uses_the_configured_chunk_size(monkeypatch) -> None:
    runner, _ = _runner(monkeypatch, chunk_seconds=0.5)
    assert runner.config.chunk_seconds == 0.5
    assert runner.provenance()["chunk_seconds"] == "0.5"


def test_the_runner_pads_a_short_final_chunk_before_alignment(monkeypatch) -> None:
    """The splitter tiles exactly; the aligner's minimum is met where that requirement lives."""

    runner, _ = _runner(monkeypatch, chunk_seconds=3.0)
    import numpy as np
    import soundfile as sf
    samples = np.zeros(int(_module_asr_constants.SAMPLE_RATE * 3.01), dtype="float32")
    monkeypatch.setattr(sf, "read", lambda *args, **kwargs_: (samples, _module_asr_constants.SAMPLE_RATE))
    runner.transcribe("/nonexistent/tail.wav")

    written = [len(request["audio"]) for request in runner._get_models().aligner_processor.seen]
    assert len(written) == 2, "3.01 s at a 3 s cap is two chunks"
    assert written[0] == int(_module_asr_constants.SAMPLE_RATE * 3.0)
    assert written[-1] >= int(_module_asr_constants._CHUNK_MIN_SECONDS * _module_asr_constants.SAMPLE_RATE), "the tail was padded"


def test_the_pipeline_stitches_per_chunk_timings_with_their_offset(monkeypatch) -> None:
    """Two chunks: the second chunk's units must land at their offset in the recording."""

    runner, _ = _runner(monkeypatch, chunk_seconds=1.0)
    import numpy as np
    import soundfile as sf
    long_audio = np.zeros(_module_asr_constants.SAMPLE_RATE * 3, dtype="float32")  # 3 chunks of 1 s
    monkeypatch.setattr(sf, "read", lambda *args, **kwargs_: (long_audio, _module_asr_constants.SAMPLE_RATE))
    cues = runner.transcribe("/nonexistent/long.wav")
    assert cues, "the fake models always produce text"
    assert max(cue["end"] for cue in cues) > 1.0, "the second chunk's timings were offset"


# ---------------------------------------------------------------------------------------
# The two-pass hotword contract (plan 20260928-hotword-injection-governance, and plan 001
# second-pass-asr-cache-bust): pass 1 decodes unguarded, the prompt is re-seeded with the
# tokens pass 1 produced. Pass 2 preserves the baseline use_cache=False policy.
# Separate generate calls do not share KV state unless explicitly passed a cache;
# this switch controls caching within the second generate call.
# ---------------------------------------------------------------------------------------


def test_two_pass_transcribe_reseeds_the_prompt_and_busts_the_cache_on_pass_2(monkeypatch) -> None:
    """Pass 2 observes the reseeded prompt and baseline intra-call cache policy."""

    runner, _ = _runner(monkeypatch, text="今天讲两件事。", hotwords=("今天",))
    runner.set_hotword_evidence(evidence_text=None, paired_subtitle_text=None)
    first_pass = runner.transcribe("/nonexistent/pass1.wav")
    kept = runner.rebuild_hotwords_from_first_pass(
        "".join(str(seg.get("text", "")) for seg in first_pass)
    )
    assert kept == ["今天"], "the first-pass transcript carries the configured token"
    segments = runner.transcribe("/nonexistent/pass2.wav", bust_cache=True)
    assert segments, "the fake models always produce text"

    model = runner._get_models().model
    assert len(model.calls) == 2, f"one chunk per pass, got {len(model.calls)} generate calls"
    first_call, second_call = model.calls
    # The reseed reached the prompt: pass 2's processor request carried the vocabulary line.
    pass2_prompt = runner._get_models().processor.requests[-1]["prompt"]
    assert pass2_prompt == "Vocabulary: 今天", pass2_prompt
    assert runner._get_models().processor.requests[0]["prompt"] is None
    # use_cache=False disables KV caching within this generation call.
    assert "use_cache" not in first_call, "pass 1 keeps the checkpoint cache default"
    assert second_call["use_cache"] is False, (
        "pass 2 preserves the baseline use_cache=False policy"
    )


def test_two_pass_transcribe_helper_busts_the_cache_on_pass_2(monkeypatch) -> None:
    """The production wiring: ``asr.two_pass_transcribe`` is what the CLI and the
    coordinator call, and its pass 2 must reach ``generate`` with ``use_cache=False``.

    The knob-level test above pins ``transcribe(bust_cache=True)`` directly; this one
    drives the shared helper the production call sites use, so a regression that leaves
    the knob unwired (QC finding C1) fails here even though the knob test still passes.
    """

    runner, _ = _runner(monkeypatch, text="今天讲两件事。", hotwords=("今天",))
    segments = _module_asr_runner.two_pass_transcribe(
        runner, "/nonexistent/two-pass.wav", paired_subtitle_text=None
    )
    assert segments, "the fake models always produce text"

    model = runner._get_models().model
    assert len(model.calls) == 2, f"one chunk per pass, got {len(model.calls)} generate calls"
    first_call, second_call = model.calls
    # The reseed reached the prompt: pass 2's processor request carried the vocabulary line.
    pass2_prompt = runner._get_models().processor.requests[-1]["prompt"]
    assert pass2_prompt == "Vocabulary: 今天", pass2_prompt
    assert runner._get_models().processor.requests[0]["prompt"] is None
    assert "use_cache" not in first_call, "pass 1 keeps the checkpoint cache default"
    assert second_call["use_cache"] is False, (
        "the helper must forward the baseline second-pass cache policy"
    )


def test_two_pass_transcribe_helper_skips_pass_2_when_nothing_is_kept(monkeypatch) -> None:
    """No kept tokens means pass 2 cannot change the transcript: one decode, pass 1's segments."""

    runner, _ = _runner(monkeypatch, text="今天讲两件事。", hotwords=("未明子",))
    segments = _module_asr_runner.two_pass_transcribe(
        runner, "/nonexistent/two-pass.wav", paired_subtitle_text=None
    )
    assert segments, "the fake models always produce text"

    model = runner._get_models().model
    assert len(model.calls) == 1, (
        "pass 2 is skipped when the guard keeps nothing; got "
        f"{len(model.calls)} generate calls"
    )
    assert "use_cache" not in model.calls[0], "the single decode keeps the checkpoint cache default"


# ---------------------------------------------------------------------------------------
# Provenance: the new keys, and the redaction that must survive the engine change.
# ---------------------------------------------------------------------------------------


def test_provenance_redacts_a_windows_path_and_publishes_a_relative_one() -> None:
    """The redaction rule's real coverage, pinned on both sides.

    It catches a Windows path, a URL and a credential-marked value — and it deliberately lets the
    documented *relative* checkpoint path through, because that is how a local checkpoint is named.
    """

    runner = _module_asr_runner.ASRRunner(_module_asr_config.ASRConfig(model_name="Qwen/Qwen3-ASR-1.7B-hf", device="cpu"))
    provenance = runner.provenance()
    assert provenance["model_name"] == "Qwen/Qwen3-ASR-1.7B-hf"
    assert provenance["aligner_model"] == _module_asr_constants.DEFAULT_ALIGNER_MODEL
    assert "vad_model" not in provenance, "the VAD component left with the engine"

    for hostile in ("C:\\models\\Qwen3-ASR-1.7B-hf", "https://host/models", "myorg/token_abc"):
        assert _module_asr_runner.ASRRunner(_module_asr_config.ASRConfig(model_name=hostile, device="cpu")).provenance()[
            "model_name"
        ] == "[redacted]", hostile

    relative = _module_asr_runner.ASRRunner(
        _module_asr_config.ASRConfig(model_name="models/Qwen3-ASR-1.7B-hf", device="cpu")
    ).provenance()["model_name"]
    assert relative == "models/Qwen3-ASR-1.7B-hf"


def test_default_config_resolves_the_declared_language_and_the_prompt_terms(monkeypatch) -> None:
    monkeypatch.setenv("BILI_ASR_LANGUAGE", "Chinese")
    monkeypatch.setenv("BILI_ASR_HOTWORDS", "新词,攻势")
    monkeypatch.setenv("BILI_ASR_CHUNK_SECONDS", "90")
    config = _module_asr_config.default_config()
    assert config.language == "Chinese"
    # 2026-09-28 governance ruling: the default config seeds nothing (empty
    # DEFAULT_HOTWORDS); the operator env knob still appends its terms, which
    # the evidence guard admits at run time (ASRRunner.set_hotword_evidence).
    assert "新词" in config.hotwords and config.hotwords.count("攻势") == 1
    assert config.chunk_seconds == 90.0


# ---------------------------------------------------------------------------------------
# Products: unchanged, and still the shape the archive writes.
# ---------------------------------------------------------------------------------------


def test_srt_and_txt_are_built_from_the_cues() -> None:
    cues = [{"start": 0.0, "end": 2.5, "text": "第一句。"},
            {"start": 3.0, "end": 4.25, "text": "第二句。"}]
    srt = asr.segments_to_srt(cues)
    assert "1\n00:00:00,000 --> 00:00:02,500\n第一句。\n" in srt
    assert "2\n00:00:03,000 --> 00:00:04,250\n第二句。\n" in srt
    assert asr.segments_to_txt(cues) == "第一句。\n第二句。"


def test_the_srt_clock_rounds_to_milliseconds() -> None:
    assert asr._fmt_srt_time(0.0) == "00:00:00,000"
    assert asr._fmt_srt_time(3661.2349) == "01:01:01,235"
    assert asr._fmt_srt_time(-1) == "00:00:00,000"


# ---------------------------------------------------------------------------------------
# The offline guard, carried over from the retired reproducibility suite: transcribing must not
# reach the network, whatever the engine is.
# ---------------------------------------------------------------------------------------


def test_transcription_imports_no_network_module(monkeypatch) -> None:
    runner, _ = _runner(monkeypatch)
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "socket" or name.startswith(("socket.", "modelscope.", "requests.")):
            raise AssertionError(f"the ASR boundary imported {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert runner.transcribe("/nonexistent/offline.wav"), "the fake models always produce text"


def test_the_dependency_hint_names_the_extra() -> None:
    """The message a host without the extra prints must name what to install."""

    assert "[asr]" in _module_asr_constants._INSTALL_HINT
    assert _module_asr_errors.ASRDependencyError("x").args == ("x",)


# ---------------------------------------------------------------------------------------
# The built-in hotword list is a *measured* artifact, and it has already been rewritten once
# without anything noticing: commit 2548ca9 took it from 33 entries to 26 — deleting the six
# 2026-09-17 homophone entries and their rationale, re-adding ITEM/AITEM which an earlier commit
# had removed for measured harm, and dropping three Latin shards — while every gate stayed green
# because no test read the list (residual ``20260924-qwen3-asr-transformers · R2``).
#
# 2026-09-28 governance ruling (plan ``20260928-hotword-injection-governance``): the shipped
# list is **empty-with-guard-on** while the per-token keep/drop measurement is pending operator
# re-run — no speculative seeding.  The measured-candidate tokens that populated the list are
# preserved under ``MEASURED_HOTWORD_CANDIDATES`` (the pending ruling's subjects); the guard
# (:func:`asr.evidence_guard_hotwords`) is what admits any of them back into a run's prompt.
#
# These tests pin the parts the repository's own documentation claims.  They are deliberately
# exact: a change to the list is a change to what the decoder is asked to reproduce, so it must
# arrive with a deliberate edit to these expectations, not silently.
# ---------------------------------------------------------------------------------------

# The six homophone pairs added 2026-09-17 on measured mis-renderings.  `扬弃` is the one the error
# census named as the worst affected (10 correct vs 89 wrong across 14 lectures, README §vocabulary).
_HOMOPHONE_ENTRIES = ("扬弃", "自在", "变易", "此在", "感性", "实存")

# The Latin-script terms the corpus speaks, which the decoder shatters without them.
_LATIN_ENTRIES = ("International Employment Matters Tribunal", "International", "Employment", "Tribunal")

# Removed 2026-09-17 for measured harm: nine occurrences across the 14 archived lectures came out
# as if they were the English word.  Their absence is a decision, not an omission.
_WITHDRAWN_ENTRIES = ("ITEM", "AITEM")


def test_the_shipped_hotword_list_is_empty_pending_the_keep_drop_measurement() -> None:
    """Governance ruling 2026-09-28: no speculative seeding while the ruling is pending."""

    assert _module_asr_constants.DEFAULT_HOTWORDS == (), (
        "the shipped list is empty-with-guard-on; kept tokens return here only with a "
        "measured delta recorded in the plan's Measurement results"
    )


def test_the_measured_candidates_are_preserved_for_the_pending_ruling() -> None:
    """The tokens the pending ruling measures stay pinned — an empty shipped list is not a lost list."""

    for term in _HOMOPHONE_ENTRIES:
        assert term in _module_asr_constants.MEASURED_HOTWORD_CANDIDATES, f"the measured homophone entry {term!r} is missing"
    for term in _LATIN_ENTRIES:
        assert term in _module_asr_constants.MEASURED_HOTWORD_CANDIDATES, f"the Latin-script entry {term!r} is missing"


def test_the_hotword_list_still_excludes_the_withdrawn_acronyms() -> None:
    """The bare acronyms were removed on measured evidence and must not quietly return."""

    for term in _WITHDRAWN_ENTRIES:
        assert term not in _module_asr_constants.DEFAULT_HOTWORDS, (
            f"{term!r} was removed 2026-09-17 for measured harm; re-adding it needs its own measurement"
        )


def test_the_hotword_list_is_free_of_duplicates_and_blanks() -> None:
    """A duplicate or blank entry is a silent prompt defect; the list is shipped, not cleaned."""

    assert len(set(_module_asr_constants.DEFAULT_HOTWORDS)) == len(_module_asr_constants.DEFAULT_HOTWORDS), "duplicate entry in DEFAULT_HOTWORDS"
    assert all(term.strip() == term and term for term in _module_asr_constants.DEFAULT_HOTWORDS), (
        "blank or untrimmed entry in DEFAULT_HOTWORDS"
    )


# ---------------------------------------------------------------------------------------
# Descriptor materialization, carried over from the retired reproducibility suite: the CLI hands
# the boundary a confined descriptor, which the decoder libraries cannot always open.
# ---------------------------------------------------------------------------------------


@pytest.mark.skipif(not pathlib.Path("/proc/self/fd").is_dir(), reason="requires Linux procfs descriptors")
def test_a_descriptor_path_is_copied_to_a_readable_file(tmp_path) -> None:
    source = tmp_path / "audio.m4a"
    source.write_bytes(b"payload")
    handle = os.open(source, os.O_RDONLY)
    temporary = None
    try:
        path, temporary = _module_asr_audio._materialize_input(f"/proc/self/fd/{handle}")
        assert temporary is not None and path == temporary
        assert pathlib.Path(path).read_bytes() == b"payload"
    finally:
        os.close(handle)
        if temporary:
            os.unlink(temporary)


def test_a_plain_path_is_returned_untouched() -> None:
    assert _module_asr_audio._materialize_input("/tmp/whatever.m4a") == ("/tmp/whatever.m4a", None)


# ---------------------------------------------------------------------------------------
# The reader's codec boundary.  libsndfile reads WAV, FLAC, OGG and MP3 — not AAC, which is the
# codec inside the ``.m4a`` this archive's own downloader writes for the preferred DASH audio
# stream.  The fallback is the ``ffmpeg`` binary; these tests pin that it runs, that it produces the
# same cues as the primary reader, and that it leaves the formats the primary reader opens alone.
#
# The earlier revision of this file stubbed the fallback's *reader* and asserted only that the
# branch was entered, which is how a fallback that could not decode anything stayed green through
# every gate (residual ``iter-2026-09-qwen3-asr-closeout · R5``).  The real decode is therefore
# exercised on a generated AAC file below, not only described.
# ---------------------------------------------------------------------------------------


def _require_ffmpeg() -> str:
    """Return the ``ffmpeg`` path, or FAIL the test naming it as a missing prerequisite.

    Not ``pytest.skip``: this repository has already decided this question the other way for the same
    class of dependency. ``tests/installed_cli.py`` states the policy in its own docstring — *"fail
    the test (never pytest.skip / xfail) with a named prerequisite. Automated verification must not
    go green because a console script was absent."* ``ffmpeg`` is a **declared product requirement**
    (``AGENTS.md``, and the NOTE in ``pyproject.toml``), so a host without it cannot verify this
    product, and a green suite on such a host is the same false signal that let R5 ship.

    Measured before this change: with ``ffmpeg`` off ``PATH`` the suite reported
    ``43 passed, 3 skipped`` and exited 0, the three skips being exactly the tests that exercise the
    decode path this module exists to verify.
    """

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.fail(
            "prerequisite missing: the `ffmpeg` binary is not on PATH, and it is a declared "
            "requirement of this product (AGENTS.md) — the .m4a/AAC decode path cannot be verified "
            "without it. Install it (e.g. `apt install ffmpeg`) and re-run.",
            pytrace=False,
        )
    return ffmpeg


def _write_aac(path, *, seconds: float = 2.0, rate: int = 44100, channels: int = 2) -> None:
    """Encode real AAC audio with ffmpeg — the container the downloader writes."""

    ffmpeg = _require_ffmpeg()
    subprocess.run(
        [
            ffmpeg, "-v", "error", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={seconds}:sample_rate={rate}",
            "-ac", str(channels),
            "-c:a", "aac",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_a_real_aac_file_is_decoded_through_the_fallback(tmp_path) -> None:
    """The path the product actually needs: a real ``.m4a`` reads, with its rate and channels.

    This is the test the codec repair owed from the start.  It fails if the fallback cannot decode
    AAC — which is exactly what happened when ``librosa`` 1.0 dropped ``audioread`` while the
    suite stayed green.
    """

    import numpy as np
    import soundfile as sf
    _require_ffmpeg()

    path = tmp_path / "fixture.m4a"
    _write_aac(path, seconds=2.0, rate=44100, channels=2)

    samples, rate = _module_asr_audio._read_audio(str(path))

    assert rate == 44100, f"the native rate must survive the fallback, got {rate}"
    assert samples.ndim == 2 and samples.shape[1] == 2, (
        f"both channels must survive; soundfile's (samples, channels) convention, got {samples.shape}"
    )
    assert samples.shape[0] > rate, f"two seconds of audio, got {samples.shape[0]} samples"
    assert float(np.abs(samples).max()) > 0.01, "the decoded audio must not be silence"


def test_a_non_16k_rate_is_resampled_by_the_runner(monkeypatch) -> None:
    """The fallback's *rate* must be honoured — the resample branch is on the ``.m4a`` path.

    The fallback returns the file's native rate (48 kHz for this corpus), while the chunker works in
    ``SAMPLE_RATE``.  The fake aligner reports fixed timings unrelated to audio length, so cue times
    cannot measure this; what *can* be checked is that the resampler ran with the source rate and
    that the chunker then saw 16 kHz samples.  Without the branch, a 3 s file would be chunked as if
    it were 9 s long.
    """

    import numpy as np
    import soundfile as sf
    import soxr
    runner, _ = _runner(monkeypatch)  # installs the fake models, audio read and writes

    seen: list[tuple[int, int, int]] = []
    real_resample = soxr.resample

    def spy(x, in_rate, out_rate, *args, **kwargs):
        seen.append((len(x), int(in_rate), int(out_rate)))
        return real_resample(x, in_rate, out_rate, *args, **kwargs)

    # ``transcribe`` imports soxr locally, so the spy replaces the attribute on the module object
    # every import resolves to.
    monkeypatch.setattr(soxr, "resample", spy)

    captured: dict = {}
    real_split = _module_asr_audio._split_audio

    def spy_split(samples, sample_rate, max_chunk_seconds):
        captured["rate"] = sample_rate
        captured["length"] = len(samples)
        return real_split(samples, sample_rate, max_chunk_seconds)

    monkeypatch.setattr(_module_asr_audio, "_split_audio", spy_split)
    # three seconds at 48 kHz, i.e. the fallback's own shape and rate; set after _runner, whose own
    # patch returns 16 kHz and would otherwise win.
    monkeypatch.setattr(sf, "read", lambda *a, **k: (np.zeros(48_000 * 3, dtype="float32"), 48_000))

    runner.transcribe("/nonexistent/audio.m4a")

    assert seen == [(144_000, 48_000, _module_asr_constants.SAMPLE_RATE)], (
        f"the resampler must be called once with the source rate, got {seen}"
    )
    assert captured["rate"] == _module_asr_constants.SAMPLE_RATE
    assert captured["length"] == 48_000, (
        f"3 s at 16 kHz after resampling, got {captured['length']} samples"
    )


def test_a_real_file_round_trips_through_the_primary_reader(tmp_path) -> None:
    """The primary reader is exercised against a real file, not a stub.

    ``test_a_decodable_file_never_reaches_the_fallback`` replaces ``sf.read`` with
    ``lambda *args, **kwargs``, which discards every read option — so it cannot see a `frames=`,
    `start=` or `dtype=` change, and measured mutations of exactly those left the suite green at
    46 passed.  This test is the one that reads bytes off disk through the real code path, so a
    primary reader that truncates, seeks, or changes dtype fails here.

    The comparison is a tolerance rather than ``array_equal``: the fixture goes through a 24-bit
    PCM WAV, so the round trip is lossy at about 1e-5 by construction (measured: max abs difference
    3.05e-05 on a full-scale ramp).  A tolerance still catches every mutation this test exists for —
    truncation and seeking change the *shape*, and a dtype change alters the magnitude by orders of
    magnitude, not by 1e-5.
    """

    import numpy as np
    import soundfile as sf

    path = tmp_path / "fixture.wav"
    written = np.linspace(-0.75, 0.75, _module_asr_constants.SAMPLE_RATE, dtype="float32")
    sf.write(str(path), written, _module_asr_constants.SAMPLE_RATE)

    samples, rate = _module_asr_audio._read_audio(str(path))

    assert rate == _module_asr_constants.SAMPLE_RATE, f"the native rate must survive, got {rate}"
    assert samples.shape == written.shape, (
        f"the whole file must be read: got {samples.shape} for a {written.shape} file "
        "(a truncating or seeking reader changes this)"
    )
    assert samples.dtype == np.float32, f"the reader must return float32, got {samples.dtype}"
    assert np.abs(samples - written).max() < 1e-4, (
        "the samples must come back in order and unchanged (24-bit WAV quantisation is ~1e-5); "
        f"max abs difference was {np.abs(samples - written).max():.2e}"
    )


def test_a_real_file_keeps_its_channel_layout(tmp_path) -> None:
    """Stereo stays stereo and is returned samples-first, which is the shape callers index."""

    import numpy as np
    import soundfile as sf

    path = tmp_path / "stereo.wav"
    written = np.linspace(-0.5, 0.5, _module_asr_constants.SAMPLE_RATE * 2, dtype="float32").reshape(-1, 2)
    written[:, 1] *= -1.0  # make the channels distinguishable, so a swap is visible

    sf.write(str(path), written, _module_asr_constants.SAMPLE_RATE)
    samples, rate = _module_asr_audio._read_audio(str(path))

    assert rate == _module_asr_constants.SAMPLE_RATE
    assert samples.shape == written.shape, f"stereo must stay stereo, got {samples.shape}"
    # A channel swap or transpose is a sign/magnitude change, far above the 24-bit noise floor.
    assert np.abs(samples - written).max() < 1e-4, (
        "the channels must not be swapped or transposed; "
        f"max abs difference was {np.abs(samples - written).max():.2e}"
    )


def test_an_undecodable_file_raises_the_typed_error(monkeypatch, tmp_path) -> None:
    """A file the decoder cannot read raises ``AudioDecodeError``, the class the ledger records.

    Measured gap this closes: replacing the class with a bare ``RuntimeError``, and deleting the
    whole non-zero-exit guard, both left the suite green at 46 passed.
    """

    import numpy as np
    import soundfile as sf
    _require_ffmpeg()

    path = tmp_path / "not-audio.m4a"
    path.write_text("this is not audio\n", encoding="utf-8")

    def refuse(*args, **kwargs):
        raise sf.LibsndfileError(1, f"Error opening '{path}': ")

    monkeypatch.setattr(sf, "read", refuse)

    with pytest.raises(_module_asr_errors.AudioDecodeError) as caught:
        _module_asr_audio._read_audio(str(path))

    assert not isinstance(caught.value, _module_asr_errors.ASRDependencyError), (
        "an undecodable file is a file problem, not a missing dependency"
    )


def test_a_decodable_file_never_reaches_the_fallback(monkeypatch) -> None:
    """The primary reader's success is the whole path; WAV and FLAC keep their old behaviour."""

    import numpy as np
    import soundfile as sf

    known = np.linspace(-1.0, 1.0, 160, dtype="float32")
    monkeypatch.setattr(sf, "read", lambda *args, **kwargs: (known, 16000))
    monkeypatch.setattr(
        _module_asr_audio, "_decode_with_ffmpeg",
        lambda *a, **k: pytest.fail("the fallback must not run for a format libsndfile opens"),
    )

    samples, rate = _module_asr_audio._read_audio("x.wav")

    assert rate == 16000
    assert np.array_equal(samples, known)


def test_the_fallback_refuses_clearly_when_ffmpeg_is_missing(monkeypatch, tmp_path) -> None:
    """A host without ffmpeg must be told what to install, not handed a raw decoder error."""

    import numpy as np
    import soundfile as sf

    def refuse(*args, **kwargs):
        raise sf.LibsndfileError(1, "Error opening 'x.m4a': ")

    monkeypatch.setattr(sf, "read", refuse)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(_module_asr_errors.ASRDependencyError) as caught:
        _module_asr_audio._read_audio("x.m4a")

    assert "ffmpeg" in str(caught.value), "the message must name the missing binary"


def test_a_fallback_read_still_produces_the_same_cues_as_a_primary_read(monkeypatch) -> None:
    """Identical audio through either reader must produce identical cues, end to end.

    Note the limit of what this pins: the fake aligner's timings are derived from the *text* it is
    given, not from the samples, so this proves both paths deliver the same cue list — it cannot
    detect a wrong decode.  The real decode is pinned by
    ``test_a_real_aac_file_is_decoded_through_the_fallback``, and the rate by
    ``test_a_non_16k_rate_is_resampled_by_the_runner``.
    """

    import numpy as np
    import soundfile as sf
    runner, _ = _runner(monkeypatch)
    primary = runner.transcribe("/nonexistent/audio.m4a")
    assert primary, "the fake models always produce text"

    def refuse(*args, **kwargs):
        raise sf.LibsndfileError(1, "Error opening 'audio.m4a': ")

    monkeypatch.setattr(sf, "read", refuse)
    delivered = np.zeros(_module_asr_constants.SAMPLE_RATE * 3, dtype="float32")

    def fake_decode(path):
        # the fallback's real contract: soundfile's (samples, channels) shape, native rate
        return delivered, _module_asr_constants.SAMPLE_RATE

    monkeypatch.setattr(_module_asr_audio, "_decode_with_ffmpeg", fake_decode)

    assert runner.transcribe("/nonexistent/audio.m4a") == primary


def test_the_ffmpeg_decode_survives_a_piped_quit_key(tmp_path) -> None:
    """A caller whose stdin holds ``q`` must still get decoded audio.

    ``ffmpeg`` reads the inherited stdin by default and ``q`` is its quit key, so a cron/CI/xargs
    pipeline feeding stdin used to abort the decode of a perfectly good ``.m4a`` — reproduced before
    the fix as ``AudioDecodeError`` on a file that decoded fine with an empty stdin.

    Written as a real subprocess because that is the only way to give the product's child a stdin of
    our choosing; the assertion is on the *behaviour*, so it does not couple to which flags the fix
    happens to use.
    """

    import numpy as np
    import soundfile as sf
    _require_ffmpeg()

    path = tmp_path / "fixture.m4a"
    _write_aac(path, seconds=1.0)

    code = (
        "import sys;"
        "from bili_asr import asr;"
        "samples, rate = asr._read_audio(sys.argv[1]);"
        "print(samples.shape, rate)"
    )
    env = dict(os.environ)
    src = str(pathlib.Path(asr.__file__).resolve().parents[2])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        input=b"q\n",                      # the quit key, piped in as a caller's shell would
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, (
        "a piped stdin must not abort the decode; stderr was "
        + completed.stderr.decode("utf-8", "replace")[-300:]
    )
    assert b"44100" in completed.stdout, completed.stdout


def test_the_ffmpeg_decode_asks_for_rf64_so_a_long_item_is_not_truncated(
    tmp_path, monkeypatch
) -> None:
    """The WAVE muxer's 4 GiB ceiling must be opted out of, or long items lose their tail silently.

    The RIFF muxer cannot express a file over 4 GiB and, past that limit, ``ffmpeg`` **exits 0**
    while warning on the stderr this call discards.  Measured on an 11 600 s 48 kHz stereo source:
    the WAV held 536 870 911 of 552 000 000 frames, ``soundfile`` read the truncated array without
    raising, and ~3.1 h of audio would have vanished with no error anywhere.  ``-rf64 auto`` writes
    RF64 only when a plain WAV would overflow, and libsndfile reads RF64.

    Asserted on the argv rather than on a fixture: a >4 GiB input is not something a test suite can
    carry, and this is the one place the decision is expressed.
    """

    import numpy as np
    import soundfile as sf
    _require_ffmpeg()

    path = tmp_path / "fixture.m4a"
    _write_aac(path, seconds=1.0)

    captured: dict = {}
    real_run = subprocess.run

    def spy(argv, *args, **kwargs):
        captured.setdefault("argv", list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(asr.subprocess, "run", spy)
    _module_asr_audio._read_audio(str(path))

    argv = captured["argv"]
    # Order matters and ffmpeg is last-wins: verified on this build that `-rf64 auto … -rf64 never`
    # produces a RIFF (4 GiB-capped) container, so a set-membership check would pass while the
    # truncation this test exists to prevent came back.
    at = [i for i, a in enumerate(argv) if a == "-rf64"]
    assert len(at) == 1, f"exactly one -rf64, no later override; argv was {argv}"
    assert argv[at[0] + 1] == "auto", f"-rf64 must ask for auto; argv was {argv}"
    assert at[0] > argv.index("-i"), (
        f"-rf64 must come after the input so it applies to the output; argv was {argv}"
    )
    assert "-f" not in argv[at[0]:], (
        f"a later -f would re-select the muxer and undo -rf64; argv was {argv}"
    )
