"""Fixture-shaped copy of the hotword A/B census scoring comparators.

Ported from the operator-local ``census.py`` (iteration
``iter-2026-09-qwen3-asr-closeout``, guide assets) so the measurement harness
scores runs on any machine.  Only the scoring rules live here — the corpus
tables, arm roots and route/caption readers stay operator-local, in
``scripts/measure_hotwords.py`` and the run's own record.

Comparator revision: the guide's census as of 2026-09-26.  The basis is pinned
by ``tests/test_hotword_census.py`` because the library default scores the same
pair differently, and the number is only reproducible with its basis stated.
"""

from __future__ import annotations

import difflib
import re

#: The SRT cue clock line: ``HH:MM:SS,mmm --> HH:MM:SS,mmm``.
SRT_CUE = re.compile(r"(\d\d:\d\d:\d\d,\d+) --> (\d\d:\d\d:\d\d,\d+)")


def ratio(a: str, b: str) -> float:
    """The knowledge note's fixed basis: autojunk off, because the default scores differently."""

    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def read_cues(srt_text: str) -> dict[tuple[str, str], str]:
    """Cues keyed by their srt timestamp span — the protocol's shared-bucket key."""

    cues: dict[tuple[str, str], str] = {}
    for block in srt_text.split("\n\n"):
        match = SRT_CUE.search(block)
        if not match:
            continue
        text = block[match.end():].strip()
        cues[(match.group(1), match.group(2))] = text
    return cues


def srt_seconds(stamp: str) -> float:
    hh, mm, rest = stamp.split(":")
    ss, ms = rest.split(",")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000


def _fmt_key(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    hh, rem = divmod(ms, 3_600_000)
    mm, rem = divmod(rem, 60_000)
    ss, mss = divmod(rem, 1_000)
    return f"{hh:02}:{mm:02}:{ss:02},{mss:03}"


def arm_b_text_over(cues_b: dict[tuple[str, str], str], start: float, end: float) -> tuple[str, bool]:
    """Arm B's text over a time interval, by **overlap** rather than by key equality.

    Why not ``cues_b[(start_stamp, end_stamp)]``: the two arms segment
    independently, so the same speech lands in buckets whose boundaries differ
    by a few hundred milliseconds.  An exact-key lookup misses a span arm B
    *does* carry, reads B as empty, and classifies every inherited occurrence
    as a recovery — inflating ``R``, the one number the census exists to
    produce.  Measured on ``BV1wLTP6NE9h`` 2026-09-26.

    Returns ``(text, exact_key_hit)``.
    """

    parts = []
    exact = False
    for (b_start, b_end), text in cues_b.items():
        bs, be = srt_seconds(b_start), srt_seconds(b_end)
        if bs < end and be > start:
            parts.append((bs, text))
            if (b_start, b_end) == (_fmt_key(start), _fmt_key(end)):
                exact = True
    parts.sort()
    return "".join(t for _, t in parts), exact
