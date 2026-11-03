"""Pure structural admission boundaries preserve source order and values."""

import json

import pytest

from bili_asr.asr.admission import assess_output
from tests.support.asr_quality import coverage, cue


@pytest.mark.parametrize("cues,evidence,code", [
    ([cue(2, 3), cue(1, 2)], coverage(4, 2), "asr_timestamp_backward"),
    ([cue(0, 4.1)], coverage(), "asr_timestamp_out_of_bounds"),
    ([cue(float("nan"), 1)], coverage(), "asr_timestamp_nonfinite"),
    ([cue(0, float("inf"))], coverage(), "asr_timestamp_nonfinite"),
    ([cue(1, 1)], coverage(), "asr_timestamp_nonpositive"),
    ([cue(0, .0001)], coverage(), "asr_timestamp_nonpositive"),
    ([cue(-.0001, 4)], coverage(), "asr_timestamp_negative"),
    ([cue("0", 4)], coverage(), "asr_timestamp_invalid"),
    ([cue(10**400, 10**401)], coverage(), "asr_timestamp_out_of_range"),
    ([cue(0, 4, "\x00text")], coverage(), "asr_text_unrepresentable"),
    ([cue(0, 4, "text\n\ntext")], coverage(), "asr_text_unrepresentable"),
    ([cue()], {**coverage(), "coverage": float("nan")}, "asr_coverage_invalid"),
    ([cue()], {**coverage(), "produced_s": 8}, "asr_coverage_invalid"),
    ([cue()], {**coverage(), "coverage_short": True}, "asr_coverage_invalid"),
    ([cue(0, 4, " ")], coverage(), "asr_text_empty"),
    ([cue()], coverage(4, 2), "asr_coverage_inconsistent"),
    ([], coverage(4, 0), "asr_empty_output"),
])
def test_invalid_output_has_stable_diagnostic_without_rewriting(cues, evidence, code):
    before = json.dumps(cues)
    measured = assess_output(cues, duration_ms=4000, coverage=evidence)
    assert measured["status"] == "unusable"
    assert measured["issue_counts"][code] >= 1
    assert measured["inference_completed"] is True and measured["confirmed_silence"] is None
    assert json.dumps(cues) == before


def test_overlap_and_equal_starts_are_usable_without_union_coverage_penalty():
    measured = assess_output([cue(0, 3), cue(0, 2), cue(1, 4)], coverage=coverage())
    assert measured["status"] == "usable" and measured["issue_counts"] == {}


def test_ordinary_silence_gaps_are_not_mistaken_for_coverage_defects():
    assert assess_output([cue(0, 1), cue(3, 4)], coverage=coverage())["status"] == "usable"
    assert assess_output([cue(0, 2)], coverage=coverage(4, 2))["status"] == "needs-review"
    assert assess_output([cue()], duration_ms=4000)["status"] == "unknown"


def test_missing_flags_still_preserves_explicit_diagnostics_review():
    assert assess_output([cue()], coverage=coverage(),
                         diagnostics={"quality": {"status": "needs-review", "flags": []}})["status"] == "needs-review"


def test_bounded_diagnostics_keep_all_counts_without_dumping_raw_text():
    cues = [cue(float("nan"), 1, "secret source body") for _ in range(40)]
    quality = assess_output(cues, coverage=coverage())
    assert len(quality["issues"]) == 32 and quality["issues_truncated"] is True
    assert quality["issue_counts"]["asr_timestamp_nonfinite"] == 40
    assert "secret source body" not in json.dumps(quality)
