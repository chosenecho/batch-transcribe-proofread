"""Metric integrity for the optional real-model benchmark, without model mocks."""

from pathlib import Path
import argparse
import json
import runpy

import pytest


BENCHMARK = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "benchmark_public_asr.py"))


@pytest.mark.parametrize(
    ("reference", "hypothesis", "expected"),
    [
        ("交易，暂停。", "交易 暂停！", (0, 0, 0)),
        ("abc", "adc", (1, 0, 0)),
        ("abc", "ac", (0, 1, 0)),
        ("ac", "abc", (0, 0, 1)),
        ("abc", "", (0, 3, 0)),
        ("", "abc", (0, 0, 3)),
        ("ＡＢＣ１２", "abc12", (0, 0, 0)),
        ("十二", "12", (2, 0, 0)),
    ],
)
def test_character_error_counts(reference, hypothesis, expected):
    score = BENCHMARK["character_errors"](reference, hypothesis)
    assert tuple(score[key] for key in ("substitutions", "deletions", "insertions")) == expected
    assert score["errors"] == sum(expected)
    assert score["cer"] == (sum(expected) / score["reference_chars"] if score["reference_chars"] else None)


def test_normalization_preserves_meaningful_symbols_and_digits():
    assert BENCHMARK["normalize"](" ＡＢＣ，＋１２。\n") == "abc+12"


def test_failed_samples_remain_in_micro_cer():
    rows = [
        {"score": BENCHMARK["character_errors"]("a", "a"), "status": "ok", "duration_s": 1, "elapsed_s": 2},
        {"score": BENCHMARK["character_errors"]("abc", ""), "status": "failed", "duration_s": 3, "elapsed_s": 4},
    ]
    result = BENCHMARK["aggregate"](rows)
    assert result["cer"] == 0.75
    assert result["failed_samples"] == 1
    assert result["rtf"] == 1.5


def test_audio_hash_mismatch_is_rejected_before_inference(tmp_path):
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        BENCHMARK["verify_sample"](tmp_path, {"filename": audio.name, "sha256": "0" * 64, "id": "sample"})


def test_audio_cannot_escape_manifest_directory(tmp_path):
    with pytest.raises(ValueError, match="escaped cache"):
        BENCHMARK["verify_sample"](tmp_path, {"filename": "../sample.wav"})


def test_synthetic_composition_preserves_order_duration_and_source_hashes(tmp_path):
    import numpy as np
    import soundfile as sf

    source = tmp_path / "source"
    source.mkdir()
    samples = []
    for identifier, amplitude, reference in (("one", 0.25, "第一句"), ("two", -0.5, "第二句")):
        path = source / f"{identifier}.wav"
        sf.write(path, np.full(16000, amplitude, dtype=np.float32), 16000, subtype="FLOAT")
        samples.append({"id": identifier, "filename": path.name, "reference": reference,
                        "sha256": BENCHMARK["sha256"](path)})
    BENCHMARK["save_json"](source / "manifest.json", {"samples": samples})
    output = tmp_path / "derived"
    BENCHMARK["compose"](argparse.Namespace(source=source, cache_root=output, gap_seconds=0.5))
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["synthetic"] is True
    assert manifest["samples"][0]["duration_s"] == 2.5
    assert manifest["samples"][0]["reference"] == "第一句 第二句"
    assert manifest["components"][1]["start_s"] == 1.5
    audio, _ = sf.read(output / "concatenated.wav")
    assert np.all(audio[:16000] == 0.25)
    assert np.all(audio[16000:24000] == 0)
    assert np.all(audio[24000:] == -0.5)
    assert manifest["samples"][1]["reference"] == ""
