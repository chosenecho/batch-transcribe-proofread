from __future__ import annotations

import bili_asr.asr.constants as _module_asr_constants
import bili_asr.cli.main as _module_cli_main


import collections
import hashlib
import json
import re
from pathlib import Path

import pytest

from bili_asr import archive, asr, quality
from bili_asr.archive import LOW_CONFIDENCE, write_archive
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.page_identity import page_identity
from bili_asr.quality import (
    CONTENT_REASON_CODES,
    DEFECT_REASON_CODES,
    FRAGMENT_MAX_CHARS,
    FRAGMENT_MAX_SECONDS,
    OVERLONG_CHARS,
    REASON_CODES,
    REFERENCE_AGREEMENT_FLOOR,
    QualityAnalyzer,
    ReferenceUnavailable,
)


def row(**values: object) -> dict[str, object]:
    return {
        "bvid": "BV1demo",
        "work_id": "BV1demo:p0",
        "source": "subtitle",
        "sub_lan": "ai-zh",
        "status": "archived",
        "duration_s": 10,
        **values,
    }


def work_stem(name: str) -> str:
    """The work stem (``BV1demo.p0``) carried by an artifact name.

    Fixtures name which work they mean through the old-style artifact name; only
    the stem is load-bearing now that the four files are fixed names, so this is
    the one place that reads it out.
    """
    match = re.search(r"([A-Za-z0-9]+\.p\d+)", name)
    assert match, f"no work stem in {name!r}"
    return match.group(1)


def write_srt(root: Path, name: str, body: str) -> str:
    """Publish an srt into its work's bundle directory (shape A).

    ``name`` names the work (``BV1demo.p0.srt``); the file inside is always
    ``bundle.srt``.
    """
    path = root / "transcripts" / work_stem(name) / "bundle.srt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path.relative_to(root).as_posix()


def test_valid_monotonic_srt_is_stable_and_read_only(tmp_path: Path) -> None:
    relative = write_srt(
        tmp_path, "BV1demo.p0.srt", "1\n00:00:00,000 --> 00:00:01,000\nhello\n"
    )
    path = tmp_path / relative
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    analyzer = QualityAnalyzer()
    first = analyzer.analyze(row(srt_path=relative), tmp_path)
    second = analyzer.analyze(row(srt_path=relative), tmp_path)
    assert first.to_dict() == second.to_dict()
    assert first.reasons == ()
    assert first.cue_count == 1
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
        second.to_dict(), sort_keys=True
    )


def test_canonical_page_identity_has_no_defect(tmp_path: Path) -> None:
    relative = write_srt(
        tmp_path, "BV1demo.p0.srt", "1\n00:00:00,000 --> 00:00:01,000\nhello\n"
    )
    result = QualityAnalyzer().analyze(
        row(work_id="BV1demo:p0", srt_path=relative), tmp_path
    )
    assert result.reasons == ()
    assert result.cue_count == 1


def test_anomaly_reason_codes(tmp_path: Path) -> None:
    cases = {
        "empty": "",
        "malformed": "1\nnot timing\ntext\n",
        "non_monotonic": (
            "1\n00:00:02,000 --> 00:00:03,000\na\n\n2\n00:00:01,000 -->"
            " 00:00:01,500\nb\n"
        ),
        "overlap": (
            "1\n00:00:00,000 --> 00:00:02,000\na\n\n2\n00:00:01,000 -->"
            " 00:00:03,000\nb\n"
        ),
        "out_of_range": "1\n00:00:09,000 --> 00:00:11,000\na\n",
    }
    for reason, content in cases.items():
        relative = write_srt(tmp_path / reason, "BV1demo.p0.srt", content)
        result = QualityAnalyzer().analyze(
            row(srt_path=relative), tmp_path / reason
        )
        assert reason in result.reasons


def test_json_and_standard_archive_paths_are_supported(tmp_path: Path) -> None:
    raw = tmp_path / "transcripts" / "BV1demo.p0"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "bundle.raw.json").write_text(
        json.dumps({"body": [{"from": 0, "to": 1, "content": "x"}]}),
        encoding="utf-8",
    )
    result = QualityAnalyzer().analyze(row(), tmp_path)
    assert result.reasons == ()
    assert result.artifact_count == 1


def test_write_archive_canonical_outputs_pass_quality_analysis(
    tmp_path: Path,
) -> None:
    ident = page_identity("BV1demo", 0, 99)
    entry = {
        "bvid": ident.bvid,
        "work_id": ident.work_id,
        "page_index": 0,
        "cid": 99,
        "title": "demo",
        "pubdate_str": "2026-01-02",
        "duration_s": 12,
        "source": "asr",
        "status": "archived",
    }
    write_archive(
        tmp_path,
        entry,
        [{"start": 0.0, "end": 1.0, "text": "你好"}],
        source="asr",
    )
    result = QualityAnalyzer().analyze(entry, tmp_path)
    assert result.reasons == ()
    assert result.artifact_count == 4
    assert result.cue_count == 1


def test_json_timestamp_anomalies_and_non_finite_values(tmp_path: Path) -> None:
    raw_dir = tmp_path / "transcripts" / "nan"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Non-finite NaN
    (raw_dir / "bundle.raw.json").write_text(
        '{"body": [{"from": "NaN", "to": 1.0, "content": "x"}]}',
        encoding="utf-8",
    )
    res_nan = QualityAnalyzer().analyze(
        row(raw_path="transcripts/nan/bundle.raw.json"), tmp_path
    )
    assert "malformed" in res_nan.reasons

    # Non-finite Infinity
    (tmp_path / "transcripts" / "inf").mkdir(parents=True, exist_ok=True)
    (tmp_path / "transcripts" / "inf" / "bundle.raw.json").write_text(
        '{"body": [{"from": 0.0, "to": "Infinity", "content": "x"}]}',
        encoding="utf-8",
    )
    res_inf = QualityAnalyzer().analyze(
        row(raw_path="transcripts/inf/bundle.raw.json"), tmp_path
    )
    assert "malformed" in res_inf.reasons

    # Negative from timestamp
    (tmp_path / "transcripts" / "neg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "transcripts" / "neg" / "bundle.raw.json").write_text(
        json.dumps({"body": [{"from": -1.0, "to": 1.0, "content": "x"}]}),
        encoding="utf-8",
    )
    res_neg = QualityAnalyzer().analyze(
        row(raw_path="transcripts/neg/bundle.raw.json"), tmp_path
    )
    assert "out_of_range" in res_neg.reasons

    # Non-monotonic
    (tmp_path / "transcripts" / "nonmono").mkdir(parents=True, exist_ok=True)
    (tmp_path / "transcripts" / "nonmono" / "bundle.raw.json").write_text(
        json.dumps(
            {"body": [{"from": 2.0, "to": 3.0}, {"from": 1.0, "to": 1.5}]}
        ),
        encoding="utf-8",
    )
    res_nm = QualityAnalyzer().analyze(
        row(raw_path="transcripts/nonmono/bundle.raw.json"), tmp_path
    )
    assert "non_monotonic" in res_nm.reasons

    # Overlap
    (tmp_path / "transcripts" / "overlap").mkdir(parents=True, exist_ok=True)
    (tmp_path / "transcripts" / "overlap" / "bundle.raw.json").write_text(
        json.dumps(
            {"body": [{"from": 0.0, "to": 2.0}, {"from": 1.0, "to": 3.0}]}
        ),
        encoding="utf-8",
    )
    res_ov = QualityAnalyzer().analyze(
        row(raw_path="transcripts/overlap/bundle.raw.json"), tmp_path
    )
    assert "overlap" in res_ov.reasons

    # Out of range (exceeds duration_s=10)
    (tmp_path / "transcripts" / "oor").mkdir(parents=True, exist_ok=True)
    (tmp_path / "transcripts" / "oor" / "bundle.raw.json").write_text(
        json.dumps({"body": [{"from": 9.0, "to": 12.0}]}),
        encoding="utf-8",
    )
    res_oor = QualityAnalyzer().analyze(
        row(raw_path="transcripts/oor/bundle.raw.json"), tmp_path
    )
    assert "out_of_range" in res_oor.reasons


def test_markdown_frontmatter_identity_checks(tmp_path: Path) -> None:
    md_dir = tmp_path / "transcripts"
    (md_dir / "BV1demo.p0").mkdir(parents=True, exist_ok=True)

    # Matching frontmatter
    md_ok = md_dir / "BV1demo.p0" / "bundle.md"
    md_ok.write_text(
        '---\nbvid: "BV1demo"\nwork_id: "BV1demo:p0"\n---\n\ncontent\n',
        encoding="utf-8",
    )
    res_ok = QualityAnalyzer().analyze(
        row(md_path=md_ok.relative_to(tmp_path).as_posix()), tmp_path
    )
    assert res_ok.reasons == ()
    assert res_ok.artifact_count == 1

    # Mismatched work_id in frontmatter
    md_bad_work = md_dir / "BV1demo.p0" / "bundle.md"
    md_bad_work.write_text(
        '---\nbvid: "BV1demo"\nwork_id: "BV1other:p0"\n---\n\ncontent\n',
        encoding="utf-8",
    )
    res_bad_work = QualityAnalyzer().analyze(
        row(md_path=md_bad_work.relative_to(tmp_path).as_posix()), tmp_path
    )
    assert "identity_mismatch" in res_bad_work.reasons

    # Mismatched bvid in frontmatter
    md_bad_bvid = md_dir / "BV1demo.p0" / "bundle.md"
    md_bad_bvid.write_text(
        '---\nbvid: "BV1other"\nwork_id: "BV1demo:p0"\n---\n\ncontent\n',
        encoding="utf-8",
    )
    res_bad_bvid = QualityAnalyzer().analyze(
        row(md_path=md_bad_bvid.relative_to(tmp_path).as_posix()), tmp_path
    )
    assert "identity_mismatch" in res_bad_bvid.reasons


def test_txt_and_md_single_artifacts_are_not_empty_when_populated(
    tmp_path: Path,
) -> None:
    txt_dir = tmp_path / "transcripts" / "BV1demo.p0"
    txt_dir.mkdir(parents=True, exist_ok=True)
    txt_file = txt_dir / "bundle.txt"
    txt_file.write_text("Hello transcript text\nSecond line\n", encoding="utf-8")

    res = QualityAnalyzer().analyze(
        row(txt_path="transcripts/BV1demo.p0/bundle.txt"), tmp_path
    )
    assert res.reasons == ()
    assert res.artifact_count == 1
    assert res.cue_count == 0

    # Empty txt file
    empty_dir = tmp_path / "transcripts" / "BV1demo.p0_empty"
    empty_dir.mkdir(parents=True, exist_ok=True)
    (empty_dir / "bundle.txt").write_text("", encoding="utf-8")
    res_empty = QualityAnalyzer().analyze(
        row(txt_path="transcripts/BV1demo.p0_empty/bundle.txt"), tmp_path
    )
    assert "empty" in res_empty.reasons


def test_missing_identity_and_traversal_are_redacted(tmp_path: Path) -> None:
    """A declared path that escapes the root is its own reason, and never echoed.

    `../secret.srt` is a *declaration* the archive cannot honour, so per contract
    §2b R3 it is `identity_unconfined` — a defect — rather than the backlog reason
    `artifact_missing` (which means "not there yet").  Either way the operator's
    path text must not reach the report: the code names the problem, the path
    would leak where the file was being pointed at.
    """
    result = QualityAnalyzer().analyze(row(srt_path="../secret.srt"), tmp_path)
    payload = json.dumps(result.to_dict())
    assert result.reasons == ("identity_unconfined",)
    assert "secret" not in payload
    assert "http" not in payload


def test_identity_mismatch_is_reported_without_leaking_path(
    tmp_path: Path,
) -> None:
    relative = write_srt(
        tmp_path, "BV1other.p0.srt", "1\n00:00:00,000 --> 00:00:01,000\nx\n"
    )
    result = QualityAnalyzer().analyze(row(srt_path=relative), tmp_path)
    assert result.reasons == ("identity_mismatch",)


@pytest.mark.parametrize(
    "relative",
    [
        "transcripts/BV1demo.p10/bundle.srt",
        "BV1demo.p1/transcripts/BV1other.p0/bundle.srt",
        "transcripts/srt/BV1demo.p10.srt",
        "BV1demo.p1/transcripts/srt/BV1other.p0.srt",
    ],
)
def test_identity_rejects_page_prefixes_and_matching_ancestors(
    tmp_path: Path, relative: str,
) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")
    result = QualityAnalyzer().analyze(
        row(work_id="BV1demo:p1", srt_path=relative), tmp_path,
    )
    assert result.reasons == ("identity_mismatch",)
    assert result.cue_count == 1


@pytest.mark.parametrize(
    "relative",
    [
        "transcripts/BV1demo.p1/bundle.srt",
        "transcripts/BV1demo.p1.proofread/bundle.srt",
        "transcripts/srt/BV1demo.p1.srt",
        "transcripts/md/2026-10-06_BV1demo.p1_demo.md",
    ],
)
def test_identity_accepts_exact_bundle_and_legacy_names(
    tmp_path: Path, relative: str,
) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")
    result = QualityAnalyzer().analyze(
        row(work_id="BV1demo:p1", srt_path=relative), tmp_path,
    )
    assert "identity_mismatch" not in result.reasons


def test_no_candidate_and_absent_declaration_share_frozen_backlog_payload(
    tmp_path: Path,
) -> None:
    analyzer = QualityAnalyzer()
    no_candidate = analyzer.analyze(row(bvid=None, work_id=None), tmp_path)
    absent_declaration = analyzer.analyze(
        row(srt_path="transcripts/BV1demo.p0/bundle.srt"), tmp_path,
    )
    expected = {
        "source": "subtitle", "language": "ai-zh", "status": "archived",
        "cue_count": 0, "artifact_count": 0,
        "reasons": ["artifact_missing"], "diagnostics": [],
    }
    assert no_candidate.to_dict() == expected
    assert absent_declaration.to_dict() == expected
    assert no_candidate.content_reasons == absent_declaration.content_reasons == ()


def test_reclaimed_audio_does_not_count_as_defect(tmp_path: Path) -> None:
    relative = write_srt(
        tmp_path, "BV1demo.p0.srt", "1\n00:00:00,000 --> 00:00:01,000\nx\n"
    )
    result = QualityAnalyzer().analyze(
        row(srt_path=relative, audio_path="audio/BV1demo.p0.m4a"), tmp_path
    )
    assert result.reasons == ()
    assert result.status == "archived"


# --- content reasons: the second class, advisory by construction ---------------


def test_reason_vocabulary_splits_into_two_classes() -> None:
    assert DEFECT_REASON_CODES == (
        # The seven original codes keep their positions: the frozen output order
        # of every report that already shipped.
        "empty",
        "malformed",
        "non_monotonic",
        "overlap",
        "out_of_range",
        "identity_mismatch",
        "artifact_missing",
        # Appended by contract §2b R3: a declared path that escapes every read
        # base, and a declared identity field that is not schema-valid.  Both are
        # declaration defects rather than "not there yet", so they must not sit
        # under `artifact_missing` — that alone is the backlog reason.
        "identity_unconfined",
        "identity_invalid",
    )
    assert CONTENT_REASON_CODES == (
        "low_confidence",
        "leading_mark",
        "fragment_cue",
        "overlong_cue",
        "duplicate_cue",
        "repeated_ngram",
        "reference_disagreement",
    )
    # the ordered union keeps the seven defect codes at their original indices
    assert REASON_CODES == DEFECT_REASON_CODES + CONTENT_REASON_CODES
    assert REASON_CODES[:7] == (
        "empty",
        "malformed",
        "non_monotonic",
        "overlap",
        "out_of_range",
        "identity_mismatch",
        "artifact_missing",
    )


def test_content_thresholds_track_the_cue_shaper() -> None:
    """A cue the shaper sized is reported only when it could not be merged."""

    assert OVERLONG_CHARS == _module_asr_constants._CUE_MAX_CHARS
    assert FRAGMENT_MAX_CHARS == _module_asr_constants._CUE_MIN_CHARS
    assert FRAGMENT_MAX_SECONDS == _module_asr_constants._CUE_MIN_SECONDS
    # Identity as well as value: the reason and the archived low-confidence
    # count must read the same object, so a local re-declaration in quality.py
    # cannot silently disagree with archive.py.
    assert quality.LOW_CONFIDENCE is archive.LOW_CONFIDENCE
    assert LOW_CONFIDENCE == 0.4


def write_raw(root: Path, name: str, segments: list[dict]) -> str:
    path = root / "transcripts" / "raw" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"segments": segments}), encoding="utf-8")
    return "transcripts/raw/" + name


def test_each_content_reason_fires_on_a_crafted_artefact(tmp_path: Path) -> None:
    """One crafted artefact per content code — and each stays free of defects."""

    overlong = (
        "今天的讨论围绕国际劳工仲裁这个主题展开，涉及多个国家的法律资源分配，"
        "以及普通劳动者在遇到纠纷时能够获得的支持方式与成本问题"
    )
    assert len(overlong) > OVERLONG_CHARS
    cases: dict[str, tuple[str, str]] = {
        "low_confidence": (
            "raw",
            json.dumps(
                {
                    "segments": [
                        {"start": 0.0, "end": 2.0, "text": "正常的一句话", "confidence": 0.25}
                    ]
                }
            ),
        ),
        "leading_mark": (
            "srt",
            "1\n00:00:00,000 --> 00:00:02,000\n，今天天气很好我们出去走走\n",
        ),
        "fragment_cue": ("srt", "1\n00:00:00,000 --> 00:00:00,500\n嗯\n"),
        "overlong_cue": ("srt", f"1\n00:00:00,000 --> 00:00:08,000\n{overlong}\n"),
        "duplicate_cue": (
            "srt",
            "1\n00:00:00,000 --> 00:00:02,000\n重复的一句话\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\n重复的一句话\n",
        ),
        "repeated_ngram": (
            "srt",
            "1\n00:00:00,000 --> 00:00:08,000\n" + "一二三四五六七八" * 4 + "\n",
        ),
    }
    assert set(cases) <= set(CONTENT_REASON_CODES)
    for reason, (kind, body) in cases.items():
        case_root = tmp_path / reason
        if kind == "raw":
            relative = write_raw(case_root, "BV1demo.p0.json", json.loads(body)["segments"])
        else:
            relative = write_srt(case_root, "BV1demo.p0.srt", body)
        result = QualityAnalyzer().analyze(
            row(**{f"{kind}_path": relative}), case_root
        )
        assert reason in result.content_reasons, reason
        # advisory: a content reason never makes the item defective
        assert result.reasons == (), reason
        assert not set(result.content_reasons) & set(DEFECT_REASON_CODES), reason


def test_content_reasons_are_not_defects_and_do_not_leak_into_reasons(
    tmp_path: Path,
) -> None:
    """A healthy transcript with content observations stays a valid work item."""

    relative = write_srt(
        tmp_path,
        "BV1demo.p0.srt",
        "1\n00:00:00,000 --> 00:00:02,000\n，今天天气很好我们出去走走\n",
    )
    result = QualityAnalyzer().analyze(row(srt_path=relative), tmp_path)
    assert result.content_reasons == ("leading_mark",)
    assert result.reasons == ()
    assert "content_reasons" not in result.to_dict()


def test_clean_cue_scores_no_content_reason(tmp_path: Path) -> None:
    relative = write_srt(
        tmp_path, "BV1demo.p0.srt", "1\n00:00:00,000 --> 00:00:02,000\n今天天气很好我们出去走走\n"
    )
    result = QualityAnalyzer().analyze(row(srt_path=relative), tmp_path)
    assert result.content_reasons == ()
    assert result.reasons == ()


def test_ngram_window_is_the_pinned_eight_characters(tmp_path: Path) -> None:
    """``_NGRAM_CHARS`` is pinned from both sides, so it cannot rot silently.

    A body of three repeated 8-character blocks fires ``repeated_ngram`` at the
    declared window and — because nothing longer repeats three times — at no
    longer window.  Changing ``_NGRAM_CHARS`` to 12 leaves the focused suite
    green without this test, which its sibling ``_NGRAM_MIN_REPEATS`` already
    had guarded.
    """

    assert quality._NGRAM_CHARS == 8
    block = "甲乙丙丁戊己庚辛"
    body = block + "今天天气很好" + block + "这个答案不复杂" + block + "好"

    def windows(count: int) -> int:
        return max(
            collections.Counter(
                body[index : index + count]
                for index in range(max(0, len(body) - count))
            ).values(),
            default=0,
        )

    # The declared window repeats; every longer window occurs at most twice.
    assert windows(quality._NGRAM_CHARS) >= quality._NGRAM_MIN_REPEATS
    for longer in range(quality._NGRAM_CHARS + 1, quality._NGRAM_CHARS + 5):
        assert windows(longer) < quality._NGRAM_MIN_REPEATS, longer

    result = QualityAnalyzer().analyze(
        row(srt_path=write_srt(
            tmp_path,
            "BV1demo.p0.srt",
            f"1\n00:00:00,000 --> 00:00:08,000\n{body}\n",
        )),
        tmp_path,
    )
    assert "repeated_ngram" in result.content_reasons
    # The window is a content observation: the row stays free of defects.
    assert result.reasons == ()


def test_srt_only_artefact_reports_no_low_confidence(tmp_path: Path) -> None:
    """No recorded score means not computed — never fabricated."""

    relative = write_srt(
        tmp_path, "BV1demo.p0.srt", "1\n00:00:00,000 --> 00:00:02,000\n今天天气很好我们出去走走\n"
    )
    result = QualityAnalyzer().analyze(row(srt_path=relative), tmp_path)
    assert "low_confidence" not in result.content_reasons


@pytest.fixture(scope="module")
def recorded_cues() -> list[dict]:
    """Synthetic cues preserving duration, confidence and length edge cases.

    The raw token timestamps the shaper consumed stay beside this file as the evidence; the cues
    themselves are the product shape the quality surface reads, and the archive still holds
    transcripts that carry them — so these assertions stay about the quality surface (and about
    reading a transcript the current engine no longer produces), not about a shaper that is gone.
    """

    path = (
        Path(__file__).parent
        / "fixtures"
        / "asr-cues"
        / "synthetic.p0.cues.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _recorded_entry() -> dict[str, object]:
    ident = page_identity("BVsynthetic", 0, 12345)
    return {
        "bvid": ident.bvid,
        "work_id": ident.work_id,
        "page_index": 0,
        "cid": 12345,
        "title": "recorded probe",
        "pubdate_str": "2026-01-02",
        "duration_s": 448.0,
        "source": "asr",
        "status": "archived",
    }


def test_recorded_cue_fixture_yields_the_two_advisory_reasons(
    tmp_path: Path, recorded_cues: list[dict]
) -> None:
    """A3: on the recorded 95-cue transcript the new vocabulary fires exactly
    one under-confident cue and two over-long cues, and nothing else."""

    assert len(recorded_cues) == 95
    segments = [
        {
            "start": cue["start"],
            "end": cue["end"],
            "text": cue["text"],
            "confidence": cue["confidence"],
        }
        for cue in recorded_cues
    ]
    entry = _recorded_entry()
    write_archive(tmp_path, entry, segments, source="asr")

    result = QualityAnalyzer().analyze(entry, tmp_path)
    assert result.reasons == (), "no structural defect on a healthy transcript"
    assert result.content_reasons == ("low_confidence", "overlong_cue")
    # the 95-cue stream is counted once per cue-bearing artifact (srt + raw)
    assert result.artifact_count == 4
    assert result.cue_count == len(recorded_cues)
    assert sum(1 for cue in recorded_cues if cue["confidence"] <= LOW_CONFIDENCE) == 1
    assert sum(1 for cue in recorded_cues if len(cue["text"]) > OVERLONG_CHARS) == 2
    # the shape layer is untouched: the cue fixture keeps the shaper's invariants
    assert all(cue["text"][0] not in "，。！？、；：" for cue in recorded_cues)
    assert not any(
        len(cue["text"].strip("，。！？、；：")) < FRAGMENT_MAX_CHARS
        and (cue["end"] - cue["start"]) < FRAGMENT_MAX_SECONDS
        for cue in recorded_cues
    )


@pytest.mark.skip(reason="legacy manifest-backed quality CLI was replaced by workflow quality evidence")
def test_recorded_cue_fixture_still_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], recorded_cues: list[dict]
) -> None:
    """A3: advisory content reasons must not flip a healthy archive to exit 1."""

    from bili_asr import cli
    from bili_asr.manifest import ManifestStore

    entry = _recorded_entry()
    segments = [
        {
            "start": cue["start"],
            "end": cue["end"],
            "text": cue["text"],
            "confidence": cue["confidence"],
        }
        for cue in recorded_cues
    ]
    write_archive(tmp_path, entry, segments, source="asr")
    ManifestStore(root=str(tmp_path)).upsert(dict(entry))

    exit_code = _module_cli_main.main(
        ["coverage", "--archive-root", str(tmp_path), "--quality", "--format", "json"]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["valid_work_items"] == 1
    assert payload["summary"]["total_cues"] == len(recorded_cues)
    # The row's projected reason list now carries the two advisory content
    # reasons — asserted per class, so a defect leaking into the projection
    # (or a content reason going missing) fails here rather than passing on an
    # accidentally empty list.
    row_reasons = payload["rows"][0]["reasons"]
    assert [r for r in row_reasons if r in DEFECT_REASON_CODES] == []
    assert [r for r in row_reasons if r in CONTENT_REASON_CODES] == [
        "low_confidence",
        "overlong_cue",
    ]
    assert sorted(row_reasons) == ["low_confidence", "overlong_cue"]
    # The content counts are surfaced in the summary too, and the defect codes
    # stay at zero.
    assert payload["summary"]["low_confidence"] == 1
    assert payload["summary"]["overlong_cue"] == 1
    for code in DEFECT_REASON_CODES:
        assert payload["summary"][code] == 0


def _transcript(root: Path, text: str, name: str = "BV1demo.p0.srt") -> str:
    """An SRT named after the ``row()`` helper's canonical stem, so that the
    only reasons in play are the ones a test is about."""

    body = f"1\n00:00:00,000 --> 00:00:04,000\n{text}\n"
    return write_srt(root, name, body)


def test_reference_agreement_compares_flattened_text(tmp_path: Path) -> None:
    """Punctuation and spacing never read as disagreement; the floor holds."""

    relative = _transcript(tmp_path, "你好世界")
    reference = tmp_path / "second.txt"
    reference.write_text("你好，世界！\n", encoding="utf-8")

    result = QualityAnalyzer().analyze(
        row(srt_path=relative), tmp_path, reference
    )
    assert result.reference is not None
    assert result.reference.agreement == 1.0
    assert result.reference.floor == 0.95
    assert result.reference.compared_chars == (4, 4)
    assert "reference_disagreement" not in result.content_reasons


def test_reference_disagreement_is_advisory(tmp_path: Path) -> None:
    """A contested reference adds a content reason and no defect."""

    relative = _transcript(tmp_path, "今天的讨论围绕国际劳工仲裁展开")
    reference = tmp_path / "second.txt"
    reference.write_text("完全无关的另一段录音内容\n", encoding="utf-8")

    result = QualityAnalyzer().analyze(
        row(srt_path=relative), tmp_path, reference
    )
    assert result.reasons == (), "a contested reference is never a defect"
    assert result.content_reasons == ("reference_disagreement",)
    assert result.reference is not None
    assert result.reference.agreement < result.reference.floor
    assert result.reference.reference == "second.txt"


def test_reference_reports_basename_only(tmp_path: Path) -> None:
    """The block never carries the operator's directory layout."""

    relative = _transcript(tmp_path, "hello world")
    nested = tmp_path / "private" / "runs"
    nested.mkdir(parents=True)
    reference = nested / "second.txt"
    reference.write_text("hello world\n", encoding="utf-8")

    result = QualityAnalyzer().analyze(
        row(srt_path=relative), tmp_path, reference
    )
    assert result.reference is not None
    assert result.reference.reference == "second.txt"
    assert "private" not in repr(result.reference)


@pytest.mark.parametrize(
    "name",
    [
        # Both separators: the provenance marker's trailing ``\b`` never fires
        # after ``_`` (it is a word character), so the hyphenated form alone was
        # false assurance — these names were published as-is.
        "sessdata-backup.txt",
        "cookie-jar.txt",
        "token-file.txt",
        "my-secret.txt",
        "credential-v2.txt",
        "sessdata_backup.txt",
        "token_abc123.srt",
        "cookie_jar.srt",
        "my_secret.srt",
        "credential_v2.txt",
        "SESSDATA_abc123.txt",
        "token.abc123.txt",
    ],
)
def test_reference_credential_like_name_is_redacted(tmp_path: Path, name: str) -> None:
    """A name that is itself credential-shaped is replaced entirely."""

    relative = _transcript(tmp_path, "hello world")
    reference = tmp_path / name
    reference.write_text("hello world\n", encoding="utf-8")

    result = QualityAnalyzer().analyze(row(srt_path=relative), tmp_path, reference)
    assert result.reference is not None
    assert result.reference.reference == "[redacted]"


@pytest.mark.parametrize(
    "name", ["second.txt", "transcript-backup.txt", "tokenizer-notes.txt"]
)
def test_ordinary_reference_name_is_not_redacted(tmp_path: Path, name: str) -> None:
    """The name-level scan redacts credential-like names, not words that merely
    begin with one: ``tokenizer-notes.txt`` is an ordinary transcript name."""

    relative = _transcript(tmp_path, "hello world")
    reference = tmp_path / name
    reference.write_text("hello world\n", encoding="utf-8")

    result = QualityAnalyzer().analyze(row(srt_path=relative), tmp_path, reference)
    assert result.reference is not None
    assert result.reference.reference == name


@pytest.mark.parametrize("name,body", [("empty.txt", ""), ("blank.txt", "   \n\n")])
def test_reference_without_comparable_text_is_refused(
    tmp_path: Path, name: str, body: str
) -> None:
    """A reference with nothing to compare is a diagnostic, not a silent pass."""

    relative = _transcript(tmp_path, "hello world")
    reference = tmp_path / name
    reference.write_text(body, encoding="utf-8")

    with pytest.raises(ReferenceUnavailable) as exc:
        QualityAnalyzer().analyze(row(srt_path=relative), tmp_path, reference)
    assert exc.value.reason == "reference has no comparable text"


def test_reference_unreadable_is_refused(tmp_path: Path) -> None:
    """A missing reference raises rather than reporting a fabricated ratio."""

    relative = _transcript(tmp_path, "hello world")
    with pytest.raises(ReferenceUnavailable) as exc:
        QualityAnalyzer().analyze(
            row(srt_path=relative), tmp_path, tmp_path / "absent.txt"
        )
    assert exc.value.reason == "reference unreadable"


def test_reference_oversized_is_refused(tmp_path: Path) -> None:
    """``_MAX_BYTES`` bounds the reference exactly as it bounds an artifact."""

    relative = _transcript(tmp_path, "hello world")
    reference = tmp_path / "huge.txt"
    reference.write_text("x" * (quality._MAX_BYTES + 1), encoding="utf-8")

    with pytest.raises(ReferenceUnavailable) as exc:
        QualityAnalyzer().analyze(row(srt_path=relative), tmp_path, reference)
    assert exc.value.reason == "reference too large"


def test_reference_beyond_the_full_comparison_bound_is_sampled(tmp_path: Path) -> None:
    """``_MAX_BYTES`` bounds bytes, not work: the flattened pair is bounded too.

    With ``autojunk`` off this pair costs super-linear time — at the measured
    rate a would-be 8 MiB reference is hours of CPU with no output — so it is
    refused instead of compared.  The refusal is the reference path's own
    diagnostic, and the row's own text is never the reason here.
    """

    relative = _transcript(tmp_path, "hello world")
    # Flattened length, not file size: the comparison reads the flattened text.
    body = "甲乙丙丁戊己庚辛壬癸" * ((quality._MAX_COMPARE_CHARS // 10) + 1)
    assert len(quality.flatten_reference(body)) > quality._MAX_COMPARE_CHARS
    reference = tmp_path / "second.txt"
    reference.write_text(body, encoding="utf-8")

    result = QualityAnalyzer().analyze(row(srt_path=relative), tmp_path, reference)
    assert result.reference.method == "windowed"
    assert result.reference.total_chars[1] == len(quality.flatten_reference(body))
    assert result.reference.compared_chars[1] <= quality._MAX_COMPARE_CHARS


def test_reference_comparison_at_the_bound_is_still_measured(tmp_path: Path) -> None:
    """The bound is inclusive: a pair at ``_MAX_COMPARE_CHARS`` is compared.

    Pinning the boundary from both sides is what keeps the refusal from
    silently swallowing ordinary transcripts — a bound tightened to zero would
    still pass the over-bound test above.
    """

    size = quality._MAX_COMPARE_CHARS
    # Distinct characters, so the at-bound comparison is itself fast.
    body = "".join(chr(0x4E00 + (index % 0x2000)) for index in range(size))
    relative = write_srt(
        tmp_path,
        "BV1demo.p0.srt",
        f"1\n00:00:00,000 --> 00:00:08,000\n{body}\n",
    )
    reference = tmp_path / "second.txt"
    reference.write_text(body + "\n", encoding="utf-8")

    result = QualityAnalyzer().analyze(row(srt_path=relative), tmp_path, reference)
    assert result.reference is not None
    assert result.reference.compared_chars == (size, size)
    assert result.reference.agreement == 1.0


def test_reference_is_ignored_when_the_row_has_no_transcript_text(tmp_path: Path) -> None:
    """A row with no comparable text keeps its own defect; no ratio is invented."""

    reference = tmp_path / "second.txt"
    reference.write_text("hello world\n", encoding="utf-8")

    result = QualityAnalyzer().analyze(
        row(srt_path="transcripts/missing.p0/bundle.srt"), tmp_path, reference
    )
    assert result.reasons == ("artifact_missing",)
    assert result.reference is None
    assert result.content_reasons == ()


def test_reference_comparison_uses_the_transcript_not_the_md_bundle(
    tmp_path: Path,
) -> None:
    """The published ``.md`` bundle never stands in for the row's transcript.

    Its body is the transcript, but the bundle also carries the archive's own
    YAML frontmatter — title, bilibili URL, identity.  Comparing that metadata
    would fabricate a disagreeing reference against a transcript that matches.
    """

    relative = write_raw(
        tmp_path, "BV1demo.p0.json", [{"start": 0.0, "end": 4.0, "text": "你好世界"}]
    )
    # A bundle whose body is exactly the reference, frontmatter and all.
    md = tmp_path / "transcripts" / "BV1demo.p0" / "bundle.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(
        '---\nbvid: "BV1demo"\ntitle: "demo"\n'
        'url: "https://www.bilibili.com/video/BV1demo"\n---\n\n你好世界\n',
        encoding="utf-8",
    )
    reference = tmp_path / "second.txt"
    reference.write_text("你好世界\n", encoding="utf-8")

    result = QualityAnalyzer().analyze(
        row(raw_path=relative, md_path=md.relative_to(tmp_path).as_posix()),
        tmp_path,
        reference,
    )
    assert result.reasons == ()
    assert result.reference is not None
    assert result.reference.agreement == 1.0
    assert result.reference.compared_chars == (4, 4)
    assert "reference_disagreement" not in result.content_reasons


def test_md_bundle_is_compared_by_its_body_when_it_is_the_only_source(
    tmp_path: Path,
) -> None:
    """The bundle stays usable as a transcript — without its frontmatter.

    With no SRT, TXT or cue sidecar in the row, the bundle is the last resort,
    so its comparison text must be the body it publishes and not its title,
    URL, or identity keys.
    """

    md = tmp_path / "transcripts" / "BV1demo.p0" / "bundle.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(
        '---\nbvid: "BV1demo"\ntitle: "demo"\n'
        'url: "https://www.bilibili.com/video/BV1demo"\n---\n\n你好世界\n',
        encoding="utf-8",
    )
    reference = tmp_path / "second.txt"
    reference.write_text("你好世界\n", encoding="utf-8")

    result = QualityAnalyzer().analyze(
        row(md_path=md.relative_to(tmp_path).as_posix()), tmp_path, reference
    )
    assert result.reasons == ()
    assert result.reference is not None
    assert result.reference.agreement == 1.0
    assert "reference_disagreement" not in result.content_reasons


def test_md_bundle_never_marks_a_defect_free_row_as_disagreeing(
    tmp_path: Path,
) -> None:
    """A defect-free row whose SRT yields no text keeps its clean reason list.

    The row's SRT has one timed cue with empty text — a structural defect this
    module does not report — and the bundle beside it holds the reference text
    in its body.  Comparing the bundle's frontmatter would invent a
    ``reference_disagreement`` against a transcript the row does not contest.
    """

    relative = _transcript(tmp_path, "")
    md = tmp_path / "transcripts" / "BV1demo.p0" / "bundle.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(
        '---\nbvid: "BV1demo"\ntitle: "demo"\n'
        'url: "https://www.bilibili.com/video/BV1demo"\n---\n\n你好世界\n',
        encoding="utf-8",
    )
    reference = tmp_path / "second.txt"
    reference.write_text("你好世界\n", encoding="utf-8")

    result = QualityAnalyzer().analyze(
        row(srt_path=relative, md_path=md.relative_to(tmp_path).as_posix()),
        tmp_path,
        reference,
    )
    assert result.reasons == ()
    assert result.reference is not None
    assert result.reference.agreement == 1.0
    assert result.content_reasons == ()


def test_frontmatter_free_plain_text_keeps_its_leading_line(tmp_path: Path) -> None:
    """Stripping frontmatter never eats transcript text that is not frontmatter.

    A leading ``---`` with no closing delimiter is text, not a metadata block.
    """

    txt_dir = tmp_path / "transcripts" / "BV1demo.p0"
    txt_dir.mkdir(parents=True, exist_ok=True)
    (txt_dir / "bundle.txt").write_text(
        "---\n正文从这一行开始\n", encoding="utf-8"
    )
    reference = tmp_path / "second.txt"
    reference.write_text("---\n正文从这一行开始\n", encoding="utf-8")

    result = QualityAnalyzer().analyze(
        row(txt_path="transcripts/BV1demo.p0/bundle.txt"), tmp_path, reference
    )
    assert result.reference is not None
    assert result.reference.agreement == 1.0


@pytest.mark.parametrize(
    "name,body",
    [
        ("bad.json", '{"segments": ['),
        ("empty_segments.json", '{"segments": []}'),
        ("bare_list.json", "[]"),
        ("blank.json", ""),
    ],
)
def test_reference_json_that_carries_no_transcript_is_refused(
    tmp_path: Path, name: str, body: str
) -> None:
    """A cue sidecar that does not parse, or has no segment, is unreadable.

    Comparing it as its own source text would report a fabricated near-zero
    ratio for a reference that never held a transcript.
    """

    relative = _transcript(tmp_path, "你好世界")
    reference = tmp_path / name
    reference.write_text(body, encoding="utf-8")

    with pytest.raises(ReferenceUnavailable) as exc:
        QualityAnalyzer().analyze(row(srt_path=relative), tmp_path, reference)
    assert exc.value.reason == "reference unreadable"


def test_reference_json_with_segments_still_compares(tmp_path: Path) -> None:
    """Refusing unreadable sidecars does not narrow the JSON arm."""

    relative = _transcript(tmp_path, "你好世界")
    reference = tmp_path / "second.json"
    reference.write_text(
        json.dumps({"segments": [{"start": 0.0, "end": 4.0, "text": "你好世界"}]}),
        encoding="utf-8",
    )

    result = QualityAnalyzer().analyze(row(srt_path=relative), tmp_path, reference)
    assert result.reference is not None
    assert result.reference.agreement == 1.0


def test_reference_agreement_exactly_at_the_floor_is_not_a_disagreement(
    tmp_path: Path,
) -> None:
    """D3.7: the reason fires *below* the floor, so equality does not fire it.

    The ratio is exact rather than approximate: 19 of 20 flattened characters
    shared, which ``SequenceMatcher`` reports as exactly ``0.95``.
    """

    relative = _transcript(tmp_path, "abcdefghijklmnopqrst")
    reference = tmp_path / "second.txt"
    reference.write_text("abcdefghijklmnopqrsX\n", encoding="utf-8")

    result = QualityAnalyzer().analyze(row(srt_path=relative), tmp_path, reference)
    assert result.reference is not None
    assert result.reference.agreement == REFERENCE_AGREEMENT_FLOOR == 0.95
    assert "reference_disagreement" not in result.content_reasons


def test_reference_agreement_floor_is_the_retired_scripts_figure() -> None:
    """The threshold is the retired script's ``~0.95``, declared in one place."""

    assert quality.REFERENCE_AGREEMENT_FLOOR == 0.95


def write_bundle(root: Path, body: str, name: str = "2026-01-02_BV1demo.p0_demo.md") -> str:
    """A published ``.md`` bundle carrying ``body`` as its transcript text.

    Shape A: the markdown is ``transcripts/{stem}/bundle.md``, so the ``name``
    parameter survives only as the carrier of the work's identity — the stem is
    read out of it (``2026-01-02_BV1demo.p0_demo.md`` → ``BV1demo.p0``) and the
    file itself is always ``bundle.md``.  The composite name is kept in the
    signature so existing callers keep naming which work they mean.
    """
    path = root / "transcripts" / work_stem(name) / "bundle.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '---\nbvid: "BV1demo"\ntitle: "demo"\n'
        'url: "https://www.bilibili.com/video/BV1demo"\n---\n\n' + body + "\n",
        encoding="utf-8",
    )
    return path.relative_to(root).as_posix()


def test_transcript_rank_beats_artifact_order_for_a_stale_bundle(
    tmp_path: Path,
) -> None:
    """The row's real transcript wins even when the bundle is named first.

    The bundle is listed before the cue sidecar — the order the row happens to
    carry — and its body is stale, republished from different text.  The
    sidecar is the row's actual transcript and matches the reference, so a
    first-wins selection would compare the stale body and fabricate a
    ``reference_disagreement``.  Comparing the sidecar instead is what the
    artefact rank exists for, and no frontmatter strip can supply it.
    """

    stale = write_bundle(tmp_path, "完全不同的一段旧文本内容")
    relative = write_raw(
        tmp_path, "BV1demo.p0.json", [{"start": 0.0, "end": 4.0, "text": "你好世界"}]
    )
    reference = tmp_path / "second.txt"
    reference.write_text("你好世界\n", encoding="utf-8")

    result = QualityAnalyzer().analyze(
        row(md_path=stale, raw_path=relative), tmp_path, reference
    )
    assert result.reasons == ()
    assert result.reference is not None
    assert result.reference.agreement == 1.0
    # (4, 4) is the sidecar's cue text; the stale bundle body would be (12, 4).
    assert result.reference.compared_chars == (4, 4)
    assert result.content_reasons == ()


def test_cue_less_sidecar_never_becomes_the_comparison_source(
    tmp_path: Path,
) -> None:
    """A cue sidecar with no cue holds no transcript, so it never displaces one.

    ``raw.json`` ranks by the text it yields, not by its suffix: an ASR run
    that produced no segment leaves a sidecar whose own source is JSON
    structure.  Comparing that structure against a reference would invent a
    near-zero ratio, so the bundle beside it — the row's only real transcript,
    and matching the reference exactly — stays the comparison source.  The row
    keeps its own ``empty`` defect either way.
    """

    bundle = write_bundle(tmp_path, "你好世界")
    relative = write_raw(tmp_path, "BV1demo.p0.json", [])
    reference = tmp_path / "second.txt"
    reference.write_text("你好世界\n", encoding="utf-8")

    result = QualityAnalyzer().analyze(
        row(md_path=bundle, raw_path=relative), tmp_path, reference
    )
    assert result.reasons == ("empty",)
    assert result.reference is not None
    assert result.reference.agreement == 1.0
    # (4, 4) is the bundle's body; the cue-less sidecar's source text is (21, 4).
    assert result.reference.compared_chars == (4, 4)
    assert result.content_reasons == ()


def test_cue_less_sidecar_alone_invents_no_ratio(tmp_path: Path) -> None:
    """With no real transcript anywhere in the row, no comparison is reported."""

    relative = write_raw(tmp_path, "BV1demo.p0.json", [])
    reference = tmp_path / "second.txt"
    reference.write_text("你好世界\n", encoding="utf-8")

    result = QualityAnalyzer().analyze(row(raw_path=relative), tmp_path, reference)
    assert result.reasons == ("empty",)
    assert result.reference is None
    assert result.content_reasons == ()


def test_the_ngram_scan_is_exact_at_the_bound(tmp_path: Path) -> None:
    """R2 (this plan): the bound is part of the contract, pinned from both sides.

    At or below `_NGRAM_MAX_CHARS` the scan must answer exactly — a three-times
    repeat anywhere in the body still fires, which is the property the retry of
    this scan exists to keep.  The body here is built so the repeated window sits
    *after* the first half, so a bound that silently truncated at some smaller
    size would drop it.
    """

    assert quality._NGRAM_MAX_CHARS > quality._NGRAM_CHARS
    block = "甲乙丙丁戊己庚辛"
    filler = "这是一个用来把重复推到后面的填充句子"
    body = filler + block + filler + block + filler + block
    assert len(body) <= quality._NGRAM_MAX_CHARS, "fixture must fit inside the bound"

    result = QualityAnalyzer().analyze(
        row(srt_path=write_srt(
            tmp_path,
            "BV1demo.p0.srt",
            f"1\n00:00:00,000 --> 00:00:08,000\n{body}\n",
        )),
        tmp_path,
    )
    assert "repeated_ngram" in result.content_reasons
    assert result.reasons == ()



def _repeat_free_filler(length: int) -> str:
    """Deterministic text whose every 8-character window is nearly unique.

    Built by concatenating sha256 hex digests, so no window repeats three times —
    which is what lets a test attribute a `repeated_ngram` hit to the stretch it
    inserted rather than to the filler.
    """

    out: list[str] = []
    total = 0
    counter = 0
    while total < length:
        digest = hashlib.sha256(f"bili-asr-{counter}".encode()).hexdigest()
        out.append(digest)
        total += len(digest)
        counter += 1
    return "".join(out)[:length]

def test_the_ngram_scan_bounds_its_own_work_above_the_bound() -> None:
    """Above the bound the scan covers a bounded prefix, and the difference shows.

    The bound is a deliberate trade on an *advisory* code, so the two halves are
    asserted together: a body whose repeat lies inside the bound fires, and the
    same repeat pushed past it is allowed to be missed.  Asserting both directions
    is what keeps the bound documented rather than discovered.
    """

    block = "甲乙丙丁戊己庚辛"
    # Three *aligned* occurrences of the window: the blocks start at 0, 8 and 16,
    # so one 8-character key occurs three times.  A bare `block * 3` does not
    # (it yields only two aligned windows of that key), so it is not the fixture
    # this test needs.
    repeated = block * 4
    assert quality._has_repeated_ngram(repeated) is True

    # A filler that is itself *free* of repeats: a run of one character (or
    # zero-padded counters) would fire `repeated_ngram` on its own, which says
    # nothing about the bound.  Deterministic hex digits keep every window
    # distinct, and the assertion below proves it rather than assuming it.
    filler = _repeat_free_filler(quality._NGRAM_MAX_CHARS)
    assert quality._has_repeated_ngram(filler) is False, "filler must be repeat-free"

    # The repeated stretch pushed past the bound: the prefix scan cannot see it.
    # This is the accepted miss, stated as the code's own contract.
    past = filler + repeated
    assert len(past) > quality._NGRAM_MAX_CHARS
    assert quality._has_repeated_ngram(past) is False

    # And the same stretch inside the bound is still found — at the very start,
    # and ending exactly at the bound, so a bound that truncated earlier fails.
    assert quality._has_repeated_ngram(repeated + filler) is True
    assert quality._has_repeated_ngram(
        filler[: quality._NGRAM_MAX_CHARS - len(repeated)] + repeated
    ) is True


def test_the_ngram_bound_does_not_change_a_real_transcripts_answer(tmp_path: Path) -> None:
    """Synthetic regression text stays below the comparison bound.

    The longest archived part joins to a few thousand characters; the bound sits
    far above that, so this change is invisible to every real artifact.
    """

    from pathlib import Path as _Path

    fixture = _Path(__file__).resolve().parent / "fixtures" / "asr-cues" / "synthetic.p0.tokens.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    joined = "".join(str(item.get("text", "")) for item in payload)
    assert 0 < len(joined) < quality._NGRAM_MAX_CHARS


def test_the_quality_analyzer_reads_the_artifact_root(tmp_path: Path) -> None:
    """`analyze` resolves the row's artifacts over the ordered bases (contract §10).

    The analyzer is reachable only through `coverage --quality`, so a single-base
    read here is the quality half of "reports a live archive as broken".
    """
    archive = tmp_path / "state"
    artifact = tmp_path / "artifacts"
    relative = write_srt(
        artifact, "BV1demo.p0.srt", "1\n00:00:00,000 --> 00:00:01,000\nhello\n"
    )
    analyzer = QualityAnalyzer()
    roots = ArtifactRoots.of(archive, artifact)

    result = analyzer.analyze(row(srt_path=relative), archive, artifact_roots=roots)

    assert result.reasons == ()
    assert result.artifact_count == 1
    assert result.cue_count == 1

    # The inferred branch (no path metadata) probes the bases too: the row names no
    # artifact, so only the on-disk candidates at the configured root can answer.
    inferred_txt = artifact / "transcripts" / "BV2inferred" / "bundle.txt"
    inferred_txt.parent.mkdir(parents=True, exist_ok=True)
    inferred_txt.write_text("inferred transcript body\n", encoding="utf-8")
    inferred_result = analyzer.analyze(
        row(bvid="BV2inferred", work_id=None, cid=101), archive, artifact_roots=roots
    )
    assert "artifact_missing" not in inferred_result.reasons
    assert inferred_result.artifact_count == 1

    # Control: with the roots omitted the archive root alone is the base, which is
    # today's behaviour — the artifact is simply not there.
    assert "artifact_missing" in analyzer.analyze(row(srt_path=relative), archive).reasons
