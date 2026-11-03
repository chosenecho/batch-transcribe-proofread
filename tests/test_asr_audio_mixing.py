"""Mono preparation preserves the decoder waveform without mutating its input."""

from __future__ import annotations

import numpy as np
import pytest
import soxr

from bili_asr.asr import audio
from bili_asr.asr.config import default_config
from bili_asr.asr.runner import ASRRunner
from support import asr_fakes


@pytest.mark.parametrize("layout", ["contiguous", "reversed", "strided", "fortran", "readonly"])
def test_stereo_mixing_matches_original_float32_bits_and_preserves_input(layout):
    rng = np.random.default_rng(1011)
    samples = rng.uniform(-1, 1, (4096, 2)).astype(np.float32)
    if layout == "reversed":
        samples = samples[::-1, ::-1]
    elif layout == "strided":
        samples = samples[::2]
    elif layout == "fortran":
        samples = np.asfortranarray(samples)
    elif layout == "readonly":
        samples.flags.writeable = False
    before = samples.copy()
    expected = samples.mean(-1).astype(np.float32)

    actual = audio._mono_float32(samples)

    assert actual.dtype == np.float32
    assert actual.shape == expected.shape
    np.testing.assert_array_equal(actual.view(np.uint32), expected.view(np.uint32))
    np.testing.assert_array_equal(samples.view(np.uint32), before.view(np.uint32))
    assert not np.shares_memory(actual, samples)


def test_stereo_mixing_preserves_signed_zero_subnormal_and_overflow_behavior():
    tiny = np.nextafter(np.float32(0), np.float32(1))
    limit = np.finfo(np.float32).max
    values = [0.0, -0.0, tiny, -tiny, np.finfo(np.float32).tiny, 1.0, -1.0, limit, -limit]
    samples = np.array([(left, right) for left in values for right in values], dtype=np.float32)
    with np.errstate(over="ignore", under="ignore"):
        expected = samples.mean(-1).astype(np.float32)
        actual = audio._mono_float32(samples)
    np.testing.assert_array_equal(actual.view(np.uint32), expected.view(np.uint32))


def test_stereo_mixing_preserves_nan_and_infinity_results():
    samples = np.array([[np.inf, -np.inf], [np.inf, 1], [-np.inf, 1], [np.nan, 0]], dtype=np.float32)
    with np.errstate(invalid="ignore"):
        expected = samples.mean(-1).astype(np.float32)
        actual = audio._mono_float32(samples)
    np.testing.assert_array_equal(actual, expected)


@pytest.mark.parametrize("shape", [(0, 2), (1, 2), (7,), (7, 1), (7, 3), (2, 3, 4)])
@pytest.mark.parametrize("dtype", [np.float32, np.float64, np.int16])
def test_mono_and_other_channel_shapes_keep_the_existing_conversion(shape, dtype):
    samples = np.arange(np.prod(shape), dtype=dtype).reshape(shape)
    converted = np.asarray(samples, dtype=np.float32)
    expected = converted.mean(-1).astype(np.float32) if converted.ndim > 1 else converted
    actual = audio._mono_float32(samples)
    np.testing.assert_array_equal(actual.view(np.uint32), expected.view(np.uint32))


def test_mono_float32_input_can_be_reused_without_copying():
    samples = np.array([0.1, -0.2], dtype=np.float32)
    assert audio._mono_float32(samples) is samples


@pytest.mark.parametrize("rate", [16000, 44100, 48000])
def test_runner_supplies_the_original_mono_resampled_waveform_to_processor(monkeypatch, rate):
    models = asr_fakes.install(monkeypatch)
    samples = np.random.default_rng(1011).uniform(-0.5, 0.5, (rate * 3, 2)).astype(np.float32)
    before = samples.copy()
    samples.flags.writeable = False
    monkeypatch.setattr(audio, "_read_audio", lambda path: (samples, rate))
    expected = samples.mean(-1).astype(np.float32)
    if rate != 16000:
        expected = np.asarray(soxr.resample(expected, rate, 16000), dtype=np.float32)

    runner = ASRRunner(default_config(device="cpu", hotwords=()))
    try:
        assert runner.transcribe("stereo.audio")
        assert len(models.processor.audio) == 1
        np.testing.assert_array_equal(models.processor.audio[0].view(np.uint32), expected.view(np.uint32))
        assert runner.diagnostics()["passes"][0]["decoded_s"] == 3.0
        np.testing.assert_array_equal(samples.view(np.uint32), before.view(np.uint32))
    finally:
        runner.release()


def test_stereo_splitter_keeps_original_samples_and_offsets():
    samples = np.random.default_rng(1011).uniform(-1, 1, (48000, 2)).astype(np.float32)
    expected = audio._split_audio(samples.mean(-1).astype(np.float32), 16000, 1.0)
    actual = audio._split_audio(samples, 16000, 1.0)
    assert [offset for _, offset in actual] == [offset for _, offset in expected]
    assert sum(len(chunk) for chunk, _ in actual) == len(samples)
    for (chunk, _), (reference, _) in zip(actual, expected, strict=True):
        np.testing.assert_array_equal(chunk.view(np.uint32), reference.view(np.uint32))
