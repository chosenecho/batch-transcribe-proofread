"""The explicit profiling entry refuses occupied devices and protects content."""

from types import SimpleNamespace

import pytest

from scripts import profile_asr


def test_occupied_gpu_refused_before_importing_or_running_models(monkeypatch, tmp_path):
    monkeypatch.setattr(profile_asr, "_duration", lambda path: 12)
    monkeypatch.setattr(profile_asr.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="3456\n"))
    output = tmp_path / "output"
    args = SimpleNamespace(ack_exclusive_device=True, audio=tmp_path / "audio", output=output,
                           max_audio_seconds=30, warmup_audio=None, nvidia_device="GPU-explicit")
    with pytest.raises(RuntimeError, match="active compute owner"):
        profile_asr.run(args)
    assert not output.exists()


def test_profile_entry_requires_acknowledgement_and_does_not_overwrite(tmp_path):
    with pytest.raises(ValueError, match="acknowledgement"):
        profile_asr.run(SimpleNamespace(ack_exclusive_device=False))
    with pytest.raises(ValueError, match="already exist"):
        profile_asr.run(SimpleNamespace(ack_exclusive_device=True, output=tmp_path))


def test_idle_probe_targets_requested_device_with_bounded_command(monkeypatch):
    commands = []
    def invoke(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(stdout="\n")
    monkeypatch.setattr(profile_asr.subprocess, "run", invoke)
    profile_asr.assert_idle_gpu("GPU-example")
    assert commands[0][0][1:3] == ["-i", "GPU-example"]
    assert commands[0][1]["timeout"] == 15


def test_profiling_evidence_drops_transcript_without_changing_original():
    raw = {"passes": [{"chunks": [{"text": "private transcript", "language": "private text", "decode_s": 2}],
                       "clock": {"domain_id": "abc"}}]}
    projected = profile_asr.bounded_diagnostics(raw)
    assert projected["passes"][0]["chunks"] == [{"decode_s": 2}]
    assert raw["passes"][0]["chunks"][0]["text"] == "private transcript"


@pytest.mark.parametrize("reference,hypothesis,edits", [("中文音频", "中文音频", 0),
    ("中文音频", "中文视频", 1), ("中文音频", "中文", 2), ("中文", "中英文", 1),
    ("中文", "", 2), ("e\u0301 中文", "é中文", 0), ("中文。", "中文", 1)])
def test_reference_metric_measures_edits_with_explicit_normalization(reference, hypothesis, edits):
    report = profile_asr.character_error_rate(reference, hypothesis)
    assert report["edit_distance"] == edits
    assert report["cer"] == edits / report["reference_characters"]
    assert report["semantic_quality_accepted"] is False
    assert reference not in str(report)


def test_reference_metric_refuses_empty_and_bounds_long_comparisons():
    with pytest.raises(ValueError, match="reference"):
        profile_asr.character_error_rate(" \n", "text")
    assert profile_asr.character_error_rate("a" * 3000, "b" * 3000)["reason"] == "edit_distance_work_limit"
