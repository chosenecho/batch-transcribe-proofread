"""A merged aligner unit may span original marks, but not different speech."""

from copy import deepcopy

import pytest

from bili_asr.asr.admission import assess_output
from bili_asr.asr.alignment import (
    _aligned_cues, _characters_from_pieces, _thread_text, threading_evidence,
)
from bili_asr.archive import write_archive
from tests.support.asr_quality import coverage
from tests.test_raw_characters import _entry


def _unit(text, start=0.0, end=1.0):
    return {"text": text, "start_time": start, "end_time": end}


@pytest.mark.parametrize("separator", ["，", ",", "。", "!?", " ", "\t", "\n", "\u00a0", " — "])
def test_merged_repeated_words_preserve_text_and_one_original_interval(separator):
    word = "consciousness"
    text = f"{word}{separator}{word} 是"
    units = [_unit(word * 2, 0, 2), _unit("是", 2.5, 3)]
    original = deepcopy(units)
    pieces = _thread_text(text, units)
    assert pieces == [
        {"text": f"{word}{separator}{word}", "start": 0, "end": 2, "timing_source": "aligned"},
        {"text": " ", "start": 2, "end": 2, "timing_source": "mark"},
        {"text": "是", "start": 2.5, "end": 3, "timing_source": "aligned"},
    ]
    assert units == original
    assert "".join(p["text"] for p in pieces) == text
    report = threading_evidence(text, units, pieces)
    assert report["unmatched_text_characters"] == report["extra_alignment_units"] == 0
    assert "private_witness" not in report
    cues = _aligned_cues(pieces)
    # The existing cue character ceiling drops whitespace at a cue boundary.
    published = text.replace(" 是", "是")
    assert "".join(cue["text"] for cue in cues) == published
    characters = _characters_from_pieces(pieces, cues)
    assert characters["text"] == published
    n = len(word) * 2 + len(separator)
    assert characters["starts"][:n] == [0] * n
    assert characters["ends"][:n] == [2] * n
    assert assess_output(cues, coverage=coverage(3, 3),
                         diagnostics={"passes": [{"chunks": [{"alignment": report}]}]})["status"] == "usable"


def test_separators_at_unit_edges_keep_the_adjacent_instant():
    pieces = _thread_text("，hello world。", [_unit("hello", 1, 2), _unit("world", 3, 4)])
    assert [p["text"] for p in pieces] == ["，", "hello", " ", "world", "。"]
    assert [(p["start"], p["end"]) for p in pieces] == [(1, 1), (1, 2), (2, 2), (3, 4), (4, 4)]


@pytest.mark.parametrize("text,unit", [
    ("consciousness，conscience", "consciousnessconsciousness"),
    ("foo，Xbar", "foobar"), ("foo+bar", "foobar"), ("foo😀bar", "foobar"),
    ("foo\u200dbar", "foobar"), ("foo，Bar", "foobar"), ("café，bar", "cafebar"),
])
def test_real_content_differences_do_not_match_across_separators(text, unit):
    units = [_unit(unit)]
    pieces = _thread_text(text, units)
    report = threading_evidence(text, units, pieces)
    assert report["unmatched_text_characters"] > 0 and report["extra_alignment_units"] == 1
    assert report["private_witness"]["units"] == units
    # A partial prefix cannot consume a unit or erase the remaining evidence.
    assert not any(p["timing_source"] == "aligned" for p in pieces)
    quality = assess_output(_aligned_cues(pieces), coverage=coverage(1, 1),
                            diagnostics={"passes": [{"chunks": [{"alignment": report}]}]})
    assert quality["status"] == "unusable"


@pytest.mark.parametrize("start,end", [(1, 1), (2, 1)])
def test_separator_matching_does_not_repair_invalid_unit_times(start, end):
    units = [_unit("foobar", start, end)]
    pieces = _thread_text("foo，bar", units)
    assert pieces[0]["start"] == start and pieces[0]["end"] == end
    quality = assess_output(_aligned_cues(pieces), coverage=coverage(2, 2),
                            diagnostics={"passes": [{"chunks": [{"alignment": threading_evidence("foo，bar", units, pieces)}]}]})
    assert quality["status"] == "unusable"


def test_merged_unit_passes_real_bundle_and_character_publication_contract(tmp_path):
    text = "consciousness，consciousness 是这个意思。"
    units = [_unit("consciousnessconsciousness", 0, 1),
             *[_unit(char, 1 + i * .5, 1.5 + i * .5) for i, char in enumerate("是这个意思")]]
    pieces = _thread_text(text, units)
    cues = _aligned_cues(pieces)
    characters = _characters_from_pieces(pieces, cues)
    paths = write_archive(tmp_path, _entry("BV1merged"), cues, source="asr", characters=characters)
    assert characters["text"] == text.replace(" 是", "是")
    assert all((tmp_path / path).is_file() for path in paths.values())


def test_runner_reports_clean_alignment_after_matching_a_merged_unit(tmp_path, mock_torch):
    import numpy as np
    import soundfile as sf

    from bili_asr.asr.config import ASRConfig
    from bili_asr.asr.runner import ASRRunner
    from tests.test_asr_qwen import _FakeModelSet

    text = "hello，world是正常的内容。"
    units = [_unit("helloworld", 0, 1),
             *[_unit(char, 1 + i * .5, 1.5 + i * .5) for i, char in enumerate("是正常的内容")]]
    path = tmp_path / "speech.wav"
    sf.write(path, np.ones(64000, dtype=np.float32) * .1, 16000)
    models = _FakeModelSet(text, units)
    runner = ASRRunner(ASRConfig(model_name="offline", device="cpu"), model_factory=lambda **kwargs: models)
    try:
        cues = runner.transcribe(str(path))
        diagnostics = runner.diagnostics()
        alignment = diagnostics["passes"][-1]["chunks"][0]["alignment"]
        assert alignment["unmatched_text_characters"] == alignment["extra_alignment_units"] == 0
        assert diagnostics["quality"]["status"] == "no-known-risk"
        assert runner.characters()["text"] == text
        assert assess_output(cues, coverage=runner.transcribed_coverage(), diagnostics=diagnostics)["status"] == "usable"
    finally:
        runner.release()
