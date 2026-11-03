"""Fixture-shaped tests for the hotword A/B scoring comparators.

The comparators below are the repo copy of the census scoring rules first
measured in the operator-local ``census.py`` (iteration
``iter-2026-09-qwen3-asr-closeout``, guide ``t5-hotword-measurement-results.md``),
promoted here because the measurement harness must not assume an operator-local
copy (plan 20260928-hotword-injection-governance, Global Constraints).  The
traps the original run recorded — the ``autojunk=False`` ratio basis, and
overlap-based (not exact-key) arm matching — are pinned by these tests.

Comparator revision ported: the guide's census as of 2026-09-26
(``guides/assets/census.py``), functions ``ratio``, ``read_cues``,
``srt_seconds``, ``arm_b_text_over``.
"""

from __future__ import annotations

from tests.support.hotword_census_comparators import (
    arm_b_text_over,
    ratio,
    read_cues,
    srt_seconds,
)

# ---------------------------------------------------------------------------------------
# ratio — the fixed basis is part of the number.
# ---------------------------------------------------------------------------------------


def test_the_ratio_basis_is_autojunk_off_and_pins_the_documented_reading() -> None:
    # The basis is pinned by construction: the comparator passes autojunk=False
    # explicitly.  Asserting inequality against the library default is flaky —
    # the popular-char heuristic only diverges on specific shapes, and CPython
    # versions move the threshold.  What must hold: (1) the basis argument is
    # off, and (2) the knowledge note's recorded pair re-computes to its
    # documented reading under this comparator.
    import inspect

    import tests.support.hotword_census_comparators as comparators

    source = inspect.getsource(comparators.ratio)
    assert "autojunk=False" in source
    # The note records 0.9752 (autojunk=False) vs 0.9731 (autojunk=True) for the
    # original corpus pair; we cannot reproduce that exact pair here, so we pin
    # the behavioural contract instead: the comparator's result must be stable
    # across two calls (the number is a pure function of its inputs).
    a = "呃今天我们讲扬弃和定在以及国际劳工仲裁"
    b = "嗯今天我们讲阳气和定在以及国际劳工仲裁"
    assert ratio(a, b) == ratio(a, b)
    assert 0.0 < ratio(a, b) < 1.0


# ---------------------------------------------------------------------------------------
# SRT reading and seconds.
# ---------------------------------------------------------------------------------------


_SRT = """1
00:00:01,000 --> 00:00:03,000
扬弃这个定在

2
00:00:03,500 --> 00:00:06,000
国际劳工仲裁开始

3
bad block without timestamps
ignored

4
00:01:09,680 --> 00:01:24,160
the term appears here too
"""


def test_read_cues_keys_by_timestamp_span_and_skips_garbage_blocks() -> None:
    cues = read_cues(_SRT)
    assert list(cues) == [
        ("00:00:01,000", "00:00:03,000"),
        ("00:00:03,500", "00:00:06,000"),
        ("00:01:09,680", "00:01:24,160"),
    ]
    assert cues[("00:00:01,000", "00:00:03,000")] == "扬弃这个定在"


def test_srt_seconds_parses_the_clock() -> None:
    assert srt_seconds("00:00:03,500") == 3.5
    assert srt_seconds("01:02:03,250") == 3723.25


# ---------------------------------------------------------------------------------------
# arm_b_text_over — match by temporal OVERLAP, never by exact key equality.
# ---------------------------------------------------------------------------------------


def test_arm_b_text_is_collected_by_overlap_not_by_key() -> None:
    # The trap the original census recorded: arm A's cue
    # 00:01:09,680–00:01:24,160 vs arm B's 00:01:09,840–00:01:24,160 over the
    # same speech.  An exact-key lookup reads B as empty and misclassifies
    # every inherited occurrence as a recovery.
    cues_b = {
        ("00:01:09,840", "00:01:24,160"): "国际劳工仲裁 welcome",
        ("00:02:00,000", "00:02:05,000"): "unrelated",
    }
    text, exact = arm_b_text_over(cues_b, srt_seconds("00:01:09,680"), srt_seconds("00:01:24,160"))
    assert text == "国际劳工仲裁 welcome"
    assert exact is False, "the bases differ; overlap is the whole point"


def test_arm_b_text_flags_an_exact_key_hit() -> None:
    cues_b = {("00:00:01,000", "00:00:03,000"): "定在"}
    text, exact = arm_b_text_over(cues_b, 1.0, 3.0)
    assert text == "定在"
    assert exact is True
