"""Fixture-shaped test for the hotword A/B measurement harness.

Builds two synthetic arm roots (with-evidence-hotwords vs no-hotwords) and a
paired subtitle document, runs ``scripts/measure_hotwords.py`` over them, and
pins the per-token census: a token the prompt recovered splits R, a token the
prompt fabricated splits I, and the corpus ratio carries its stated basis.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _srt(*cues: tuple[str, str, str]) -> str:
    blocks = []
    for i, (start, end, text) in enumerate(cues, 1):
        blocks.append(f"{i}\n{start} --> {end}\n{text}\n")
    return "\n".join(blocks)


def test_the_measurement_harness_scores_the_two_arms(tmp_path: Path) -> None:
    ab = tmp_path / "ab"
    # One item; two cues.  The subtitle (the evidence route) carries 扬弃 at
    # the first cue and 定在 at the second; it never carries 攻势.
    subtitle_body = [
        {"from": 0.0, "to": 3.0, "content": "我们扬弃这个定在"},
        {"from": 3.0, "to": 6.0, "content": "国际的定在"},
    ]
    _write(ab / "subtitle", "subtitles/raw/BV1test.p0.json",
           json.dumps({"body": subtitle_body}, ensure_ascii=False))

    # with-arm: 扬弃 recovered (subtitle carries it), 攻势 inserted (subtitle
    # does not).  without-arm: neither term appears.
    _write(ab / "with", "transcripts/BV1test.p0/bundle.txt", "我们扬弃这个攻势定在\n")
    _write(ab / "with", "transcripts/BV1test.p0/bundle.srt",
           _srt(("00:00:00,000", "00:00:03,000", "我们扬弃这个攻势"),
                ("00:00:03,000", "00:00:06,000", "定在")))
    _write(ab / "without", "transcripts/BV1test.p0/bundle.txt", "我们阳气这个公式定在\n")
    _write(ab / "without", "transcripts/BV1test.p0/bundle.srt",
           _srt(("00:00:00,000", "00:00:03,000", "我们阳气这个公式"),
                ("00:00:03,000", "00:00:06,000", "定在")))

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "measure_hotwords.py"),
         "--ab-root", str(ab), "--terms", "扬弃,攻势,定在"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    census = json.loads((ab / "hotword-census.json").read_text(encoding="utf-8"))
    terms = census["terms"]
    assert terms["扬弃"]["R"] == 1 and terms["扬弃"]["I"] == 0, "subtitle-carried term is a recovery"
    assert terms["攻势"]["R"] == 0 and terms["攻势"]["I"] == 1, "subtitle-absent term is an insertion"
    assert terms["定在"]["R"] == 0 and terms["定在"]["I"] == 0, "both arms render 定在: not a recovery"
    assert 0.0 < census["corpus_ratio_with_vs_without"] < 1.0
    assert (ab / "HOTWORD-CENSUS.md").exists()


def test_the_harness_refuses_a_missing_arm(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "measure_hotwords.py"),
         "--ab-root", str(tmp_path / "nowhere")],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
