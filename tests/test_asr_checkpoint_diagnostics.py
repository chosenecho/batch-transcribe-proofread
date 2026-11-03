"""Resuming completed chunks must retain the evidence used by admission."""

import json

import numpy as np
import pytest

from bili_asr.asr.admission import assess_output
from bili_asr.asr.config import ASRConfig
from bili_asr.asr.runner import ASRRunner, set_progress_hook
from tests.test_asr_qwen import _FakeModelSet


def _units(starts):
    return [{"text": text, "start_time": start, "end_time": start + 1}
            for text, start in zip("甲乙丙丁", starts, strict=True)]


def _interrupted_checkpoint(tmp_path, first_text, first_units):
    import soundfile as sf
    path, checkpoint = tmp_path / "two.wav", tmp_path / "checkpoint.json"
    sf.write(path, np.zeros(128000, dtype=np.float32), 16000)
    config = ASRConfig(model_name="offline", device="cpu", chunk_seconds=4)
    runner = ASRRunner(config, model_factory=lambda **kwargs: _FakeModelSet(first_text, first_units))

    def interrupt(phase):
        if phase == "chunk_complete":
            raise RuntimeError("interrupted after committed chunk")

    set_progress_hook(interrupt)
    try:
        with pytest.raises(RuntimeError, match="interrupted after committed chunk"):
            runner.transcribe(str(path), checkpoint_path=checkpoint)
    finally:
        set_progress_hook(None)
        runner.release()
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["completed_chunks"] == 1
    return config, path, checkpoint


@pytest.mark.parametrize("risk,expected_status", [
    ("backwards", "unusable"), ("unmatched", "unusable"), ("healthy", "usable"),
    ("empty", "needs-review"), ("zero-duration", "unusable"),
])
def test_resume_retains_chunk_alignment_and_private_witness(tmp_path, mock_torch, risk, expected_status):
    first_text = "" if risk == "empty" else "甲乙丙丁"
    starts = [0, 2, 2, 3] if risk == "zero-duration" else [0, 2, 1, 3] if risk == "backwards" else [0, 1, 2, 3]
    units = [] if risk in {"unmatched", "empty"} else _units(starts)
    if risk == "zero-duration":
        units[2]["end_time"] = units[2]["start_time"]
    config, path, checkpoint = _interrupted_checkpoint(tmp_path, first_text, units)
    original = json.loads(checkpoint.read_text(encoding="utf-8"))
    runner = ASRRunner(config, model_factory=lambda **kwargs: _FakeModelSet("甲乙丙丁", _units([0, 1, 2, 3])))
    try:
        cues = runner.transcribe(str(path), checkpoint_path=checkpoint)
        diagnostics = runner.diagnostics()
        quality = assess_output(cues, coverage=runner.transcribed_coverage(), diagnostics=diagnostics)
        assert quality["status"] == expected_status
        chunks = diagnostics["passes"][-1]["chunks"]
        assert [chunk["chunk_index"] for chunk in chunks] == [0, 1]
        assert chunks[0]["reused_from_checkpoint"] is True
        assert chunks[0].get("alignment") == original["chunk_reports"][0].get("alignment")
        assert chunks[0]["flags"] == original["chunk_reports"][0]["flags"]
        assert "".join(cue["text"] for cue in cues) == first_text + "甲乙丙丁"
        assert not checkpoint.exists()
    finally:
        runner.release()


@pytest.mark.parametrize("damage", ["legacy", "missing-report"])
def test_checkpoint_without_complete_diagnostics_recomputes_the_prefix(tmp_path, mock_torch, damage):
    config, path, checkpoint = _interrupted_checkpoint(tmp_path, "甲乙丙丁", _units([0, 2, 1, 3]))
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    if damage == "legacy":
        saved["schema_version"] = 1
        saved.pop("chunk_reports", None)
    else:
        saved["chunk_reports"] = []
    checkpoint.write_text(json.dumps(saved), encoding="utf-8")
    runner = ASRRunner(config, model_factory=lambda **kwargs: _FakeModelSet("甲乙丙丁", _units([0, 1, 2, 3])))
    try:
        cues = runner.transcribe(str(path), checkpoint_path=checkpoint)
        diagnostics = runner.diagnostics()
        assert [chunk["chunk_index"] for chunk in diagnostics["passes"][-1]["chunks"]] == [0, 1]
        assert all(not chunk.get("reused_from_checkpoint") for chunk in diagnostics["passes"][-1]["chunks"])
        assert assess_output(cues, coverage=runner.transcribed_coverage(), diagnostics=diagnostics)["status"] == "usable"
    finally:
        runner.release()
