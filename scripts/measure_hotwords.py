#!/usr/bin/env python3
"""Hotword keep/drop A/B measurement — the repo harness for plan
``20260928-hotword-injection-governance`` (Task 2).

Scores a completed two-arm run against the run's own paired AI-subtitle text.
This is the **with-evidence-hotwords vs no-hotwords** forced choice the plan's
Task 2 names: same audio, same decoder settings, only the prompt vocabulary
differs.  Per-token word-accuracy deltas come from the repo's census
comparators (``tests/_hotword_census_comparators.py`` — the 20260918
text-precision basis, ``autojunk=False``), and the subtitle text supplies the
per-token reference so each term's occurrences split into recovered, inserted,
and unresolved.

    python3 scripts/measure_hotwords.py --ab-root <dir> [--terms a,b,c] [--out <dir>]

Layout of ``<dir>`` (each arm root is a complete archive):

    <dir>/with/     transcripts/<bvid>.p0/bundle.{txt,srt,md}   the evidence-hotword arm
    <dir>/without/  transcripts/<bvid>.p0/bundle.{txt,srt,md}   the no-hotword arm
    <dir>/subtitle/ subtitles/raw/<bvid>.p0.json          the paired AI-subtitle text

The corpus audio itself is *not* needed to score a completed run — only the
produced transcripts and the paired subtitle documents.  The per-token numbers
this prints are the ``## Measurement results`` ruling inputs.

The guard is what makes the "with" arm honest: only tokens the run's own
evidence admitted reach that arm's prompt (``evidence_guard_hotwords``); the
"without" arm sends no vocabulary line at all.

Exit codes: 0 = report written; 2 = bad arguments.
"""

from __future__ import annotations

import argparse
import difflib
import json
import pathlib
import re
import sys

# The six homophone tokens whose keep/drop ruling this plan decides, plus the
# three archive terms already verified (kept).  The default hotword list is
# empty-with-guard-on while this measurement is pending operator re-run; the
# ruling table records all nine.
MEASURED_TERMS = (
    "扬弃", "自在", "变易", "此在", "感性", "实存",  # the six unverified homophone tokens
    "国际劳工仲裁", "International Employment Matters Tribunal", "定在",  # the three verified
)

SRT_CUE = re.compile(r"(\d\d:\d\d:\d\d,\d+) --> (\d\d:\d\d:\d\d,\d+)")


# ---------------------------------------------------------------------------------------
# Scoring comparators — kept in step with tests/_hotword_census_comparators.py, which
# pins their basis (autojunk=False) and their overlap-not-key matching.
# ---------------------------------------------------------------------------------------


def ratio(a: str, b: str) -> float:
    """The text-precision basis: ``difflib`` with ``autojunk=False``.

    The library default scores the same pair differently (0.9752 vs 0.9731 on the
    original corpus), so the basis is part of the number and is pinned here and
    in ``tests/test_hotword_census.py``.
    """

    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def read_txt(root: pathlib.Path, bvid: str) -> str | None:
    path = root / "transcripts" / f"{bvid}.p0" / "bundle.txt"
    return path.read_text(encoding="utf-8") if path.exists() else None


def read_cues(srt_text: str) -> dict[tuple[str, str], str]:
    cues: dict[tuple[str, str], str] = {}
    for block in srt_text.split("\n\n"):
        match = SRT_CUE.search(block)
        if not match:
            continue
        cues[(match.group(1), match.group(2))] = block[match.end():].strip()
    return cues


def read_cues_file(root: pathlib.Path, bvid: str) -> dict[tuple[str, str], str]:
    path = root / "transcripts" / f"{bvid}.p0" / "bundle.srt"
    if not path.exists():
        return {}
    return read_cues(path.read_text(encoding="utf-8"))


def srt_seconds(stamp: str) -> float:
    hh, mm, rest = stamp.split(":")
    ss, ms = rest.split(",")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000


def arm_b_text_over(cues_b: dict[tuple[str, str], str], start: float, end: float) -> str:
    """Arm B's text over a time interval, by **overlap** rather than by key equality.

    The two arms segment independently, so the same speech lands in buckets whose
    boundaries differ by a few hundred milliseconds; an exact-key lookup misreads
    inherited occurrences as recoveries (the 20260918 trap, pinned in the tests).
    """

    parts = []
    for (b_start, b_end), text in cues_b.items():
        bs, be = srt_seconds(b_start), srt_seconds(b_end)
        if bs < end and be > start:
            parts.append((bs, text))
    parts.sort()
    return "".join(t for _, t in parts)


def subtitle_text(root: pathlib.Path, bvid: str) -> str | None:
    """The paired AI-subtitle text for one part (the evidence route's document)."""

    path = root / "subtitles" / "raw" / f"{bvid}.p0.json"
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return "".join(str(item.get("content", "")) for item in doc.get("body", []))


# ---------------------------------------------------------------------------------------
# Per-term census over the paired subtitle text.
# ---------------------------------------------------------------------------------------


def census_term(
    cues_with: dict[tuple[str, str], str],
    cues_without: dict[tuple[str, str], str],
    subtitle: str | None,
    term: str,
) -> dict:
    """Classify every occurrence of one term in the with-evidence arm.

    R — the term is in the with-arm where the without-arm differs, and the paired
        subtitle carries the term: the prompt recovered it.
    I — the term is in the with-arm where the without-arm differs, and the paired
        subtitle over that span does not carry it: an insertion (the speaker said
        something else).
    U — no subtitle coverage decides the span.
    """

    counts = {"R": 0, "I": 0, "U": 0}
    spans = []
    if subtitle is None:
        subtitle = ""
    for (start_stamp, end_stamp), cue_text in cues_with.items():
        if term not in cue_text:
            continue
        start, end = srt_seconds(start_stamp), srt_seconds(end_stamp)
        without_text = arm_b_text_over(cues_without, start, end)
        if term in without_text:
            continue  # inherited, not a recovery: both arms rendered it
        if term in subtitle:
            counts["R"] += 1
            cls = "R"
        elif not subtitle:
            counts["U"] += 1
            cls = "U"
        else:
            counts["I"] += 1
            cls = "I"
        spans.append({"start": start, "end": end, "class": cls, "with": cue_text[:80]})
    return {"counts": counts, "spans": spans, "exercised": bool(spans) or term in "".join(cues_with.values())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ab-root", required=True, help="directory holding with/ without/ subtitle/")
    parser.add_argument("--terms", default=",".join(MEASURED_TERMS),
                        help="comma-separated tokens to census (default: the nine measured terms)")
    parser.add_argument("--out", default=None, help="output directory (default: <ab-root>)")
    args = parser.parse_args()

    ab = pathlib.Path(args.ab_root)
    out_dir = pathlib.Path(args.out) if args.out else ab
    terms = [t for t in args.terms.split(",") if t.strip()]

    arm_with = ab / "with"
    arm_without = ab / "without"
    subtitle_root = ab / "subtitle"
    if not arm_with.is_dir() or not arm_without.is_dir():
        print("measure_hotwords: need <ab-root>/with and <ab-root>/without", file=sys.stderr)
        return 2

    # Shape A: a work's artifacts live in transcripts/{stem}/, so the stem is the
    # directory name and the file inside is always bundle.txt.
    transcripts_dir = arm_with / "transcripts"
    # The item identity is the *bvid*, not the directory name: the directory is
    # "{bvid}.p0" while every reader below is called with the bare bvid.
    items = sorted(
        d.name[: -len(".p0")]
        for d in transcripts_dir.glob("*.p0")
        if d.is_dir() and (d / "bundle.txt").is_file()
    ) if transcripts_dir.is_dir() else []
    if not items:
        print("measure_hotwords: no transcripts in arm 'with' — nothing to measure", file=sys.stderr)
        return 2

    report: dict = {"ab_root": str(ab), "items": items, "terms": {}, "item_ratios": {},
                    "corpus_ratio_with_vs_without": None}

    corpus_with: list[str] = []
    corpus_without: list[str] = []
    for bvid in items:
        a_text = read_txt(arm_with, bvid)
        b_text = read_txt(arm_without, bvid)
        cues_a = read_cues_file(arm_with, bvid)
        cues_b = read_cues_file(arm_without, bvid)
        sub = subtitle_text(subtitle_root, bvid)
        if a_text is not None and b_text is not None:
            report["item_ratios"][bvid] = round(ratio(a_text, b_text), 6)
            corpus_with.append(a_text)
            corpus_without.append(b_text)
        for term in terms:
            row = report["terms"].setdefault(term, {"R": 0, "I": 0, "U": 0, "spans": []})
            result = census_term(cues_a, cues_b, sub, term)
            for key in ("R", "I", "U"):
                row[key] += result["counts"][key]
            row["spans"].extend({"item": bvid, **s} for s in result["spans"])
    if corpus_with and corpus_without:
        report["corpus_ratio_with_vs_without"] = round(
            ratio("\n".join(corpus_with), "\n".join(corpus_without)), 6
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "hotword-census.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = ["# Hotword keep/drop census", "",
             f"Arms: `{arm_with}` (with-evidence-hotwords) vs `{arm_without}` (no-hotwords).",
             f"Items: {', '.join(items)}", "",
             f"Corpus ratio(with, without): **{report['corpus_ratio_with_vs_without']}** "
             "(difflib, autojunk=False)", "",
             "| term | R (recovered) | I (inserted) | U (undecided) |", "|---|---|---|---|"]
    for term in terms:
        row = report["terms"][term]
        lines.append(f"| {term} | {row['R']} | {row['I']} | {row['U']} |")
    lines += ["", "## Spans", ""]
    for term in terms:
        spans = report["terms"][term]["spans"]
        if not spans:
            continue
        lines.append(f"### {term}")
        for span in spans:
            lines.append(
                f"- **{span['class']}** {span['item']} [{span['start']:.2f}–{span['end']:.2f}] "
                f"with: {span['with']!r}"
            )
        lines.append("")
    (out_dir / "HOTWORD-CENSUS.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out_dir/'HOTWORD-CENSUS.md'} and hotword-census.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
