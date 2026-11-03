"""Task-scoped tests for plan 20260928-proofread-pipeline (T1 align, T2 guards, T3 merge).

Fixtures are synthetic: raw ASR sidecars live under ``tests/fixtures/proofread/``
(copied-into-tree, never read from a live archive root) and the caption route is built
in-test on a real SQLite transcript store, so every ASR/字幕 shape is decided here.

Marker syntax under test (the contract ``proofread-merge`` parses):

* the *original* block line: ``## 1 [00:00:00,000-00:00:03,200] (agree 1.000)``
* the *marked* block line: ``## 1 [00:00:00,000-00:00:03,200] (agree 1.000) >> keep``
  or ``>> use-asr`` / ``>> use-sub`` / ``>> custom: <text>``
* the final text is either the marker payload (``custom``) or the already-printed
  column body (``keep`` = 字幕, ``use-asr`` = ASR, ``use-sub`` = 字幕).
"""

from __future__ import annotations

import bili_asr.cli.main as _module_cli_main


import json
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "proofread"


# --------------------------------------------------------------------------------------
# Route fixtures: synthetic bvids, both routes, one disagreement-heavy part + one clean.
# --------------------------------------------------------------------------------------


def _write_caption_route(
    connection, *, bvid: str, page_index: int, cid: int, segments_ms: list[tuple[int, int, str]]
) -> None:
    """Store one synthetic caption transcript (source_kind subtitle-ai) for a part."""
    from bili_asr.storage import MetadataRepository
    from bili_asr.storage.models import TranscriptSegmentRecord

    fixtures = __import__("importlib").import_module("tests.fixtures.metadata_records")
    metadata = MetadataRepository(connection)
    with metadata.transaction():
        metadata.upsert_user(fixtures.make_user_record())
        metadata.upsert_video(fixtures.make_video_record(bvid, aid=None, title=f"{bvid} 视频"))
        metadata.upsert_part(
            fixtures.make_part_record(
                bvid, page_index=page_index, cid=cid, title=f"{bvid} p{page_index}",
                processing_status="metadata_collected",
            )
        )
    row = connection.execute(
        "SELECT video_part_id FROM video_parts WHERE bvid = ? AND page_index = ?",
        (bvid, page_index),
    ).fetchone()
    part_id = int(row["video_part_id"])

    from bili_asr.storage import TranscriptRepository
    from bili_asr.storage.models import AcquisitionRunRecord

    repository = TranscriptRepository(connection)
    run_id = f"fixture-caption-{bvid}-{page_index}"
    repository.start_acquisition_run(
        AcquisitionRunRecord(
            run_id=run_id,
            kind="subtitle",
            selector_kind="bvid",
            selector_target=bvid,
            requested_limit=None,
            credential_present=False,
            started_at=1_700_000_000,
        )
    )
    repository.record_acquired_transcript(
        run_id=run_id,
        video_part_id=part_id,
        source_kind="subtitle-ai",
        language="ai-zh",
        segments=tuple(
            TranscriptSegmentRecord(start_ms=start, end_ms=end, text=text)
            for start, end, text in segments_ms
        ),
        started_at=1_700_000_000,
        finished_at=1_700_000_000,
        created_at=1_700_000_000,
    )


CLEAN_CAPTIONS_MS = [
    (0, 3200, "大家好，我们今天继续讨论。"),
    (3200, 6000, "我们先看一下基本情况。"),
    (6600, 10000, "这个问题的大背景是这样的。"),
    (12600, 15600, "所以我们先给出结论。"),
    (15600, 18800, "结论就是前面说的那样。"),
    (19600, 22600, "谢谢大家收看。"),
]

HEAVY_CAPTIONS_MS = [
    (0, 4000, "我们聊一聊劳动法的问题。"),
    (4000, 8000, "这里面有很多具体案例。"),
    (12000, 16000, "下面看一个真实例子。"),
    (13000, 15000, "这是一条字幕自己的补充。"),
    (16000, 20000, "这个例子说明了问题所在。"),
    (30000, 34000, "所以我们回到最初的问题上来。"),
]


@pytest.fixture
def proofread_workspace(tmp_path):
    """Archive root + artifact root holding both routes for one clean and one heavy part."""
    from bili_asr.storage import open_database

    archive_root = tmp_path / "archive"
    artifact_root = tmp_path / "artifacts"
    (artifact_root / "transcripts").mkdir(parents=True)
    connection = open_database(os.fspath(archive_root))
    try:
        _write_caption_route(connection, bvid="BV1proofClean", page_index=0, cid=101,
                             segments_ms=CLEAN_CAPTIONS_MS)
        _write_caption_route(connection, bvid="BV1proofHeavy", page_index=0, cid=202,
                             segments_ms=HEAVY_CAPTIONS_MS)
        for raw in ("BV1proofClean.p0.json", "BV1proofHeavy.p0.json"):
            (artifact_root / "transcripts" / raw[: -len(".json")]).mkdir(parents=True, exist_ok=True)
            (artifact_root / "transcripts" / raw[: -len(".json")] / "bundle.raw.json").write_bytes(
                (FIXTURES / raw).read_bytes()
            )
    finally:
        connection.close()
    return archive_root, artifact_root


def _run_proofread(workspace, bvid: str) -> tuple[Path, Path]:
    from bili_asr.proofread import build_sidebyside

    archive_root, artifact_root = workspace
    return build_sidebyside(bvid, 0, archive_root=os.fspath(archive_root),
                            artifact_root=os.fspath(artifact_root))


# --------------------------------------------------------------------------------------
# Task 1: the side-by-side table + alignment jsonl.
# --------------------------------------------------------------------------------------


def test_proofread_writes_sidebyside_and_alignment_jsonl(proofread_workspace):
    sidebyside_path, align_path = _run_proofread(proofread_workspace, "BV1proofClean")

    assert sidebyside_path == proofread_workspace[1] / ".tmp" / "proofread-work" / \
        "inputs" / "BV1proofClean.p0.sidebyside.md"
    assert align_path == proofread_workspace[1] / ".tmp" / "proofread-work" / \
        "align" / "BV1proofClean.p0.alignment.jsonl"

    sidebyside = sidebyside_path.read_text(encoding="utf-8")
    assert "## 1 [00:00:00,000-00:00:12,000] (agree 0.932)" in sidebyside
    assert sidebyside.index("| ASR | 字幕 |") < sidebyside.index("## 1")

    header = json.loads(align_path.read_text(encoding="utf-8").splitlines()[0])
    assert header["kind"] == "header"
    assert header["asr_chars"] > 0 and header["subtitle_entries"] > 0
    records = [json.loads(line) for line in align_path.read_text(encoding="utf-8").splitlines()[1:]]
    blocks = [rec for rec in records if rec["kind"] == "block"]
    assert len(blocks) == header["blocks"]
    # asr per-segment records are not emitted; the header count is the pin
    assert header["asr_segments"] == 6


def test_proofread_block_semantics_agree_minor_review(proofread_workspace):
    """Ratified thresholds: >=0.85 agree, 0.75-0.85 minor, <0.75 review."""
    from bili_asr.proofread import align_routes, classify_similarity

    sidebyside_path, _ = _run_proofread(proofread_workspace, "BV1proofHeavy")
    align = align_routes("BV1proofHeavy:p0",
                         [(0, 4000, "我们来讨论劳动法的相关问题。"),
                          (4000, 8000, "这里涉及很多具体的案例。"),
                          (12000, 16000, "下面看一个实际发生的例子。"),
                          (16000, 20000, "这个例子说明了问题所在。"),
                          (30000, 34000, "所以回到最初的问题上来。")],
                         sorted(HEAVY_CAPTIONS_MS, key=lambda entry: entry[0]))
    decisions = {block.index: block.decision for block in align.blocks}
    # Measured against the landed aligner (autojunk=False, whole-run overlap
    # assignment, plateau spans): b1 0.735 review, b2 0.667 review (the extra
    # subtitle cue lands inside b2 and drags it below 0.75 — the disagreement-
    # heavy shape the method exists to surface), b3 0.923 agree.  The table
    # shows review+agree here; "minor" is exercised by classify pins below.
    assert decisions[1] == "review"        # 0.735 < 0.75
    assert decisions[2] == "review"        # 0.667: extra cue drags the pair
    assert decisions[3] == "agree"         # 0.923

    assert classify_similarity(0.85) == "agree"
    assert classify_similarity(0.8499) == "minor"
    assert classify_similarity(0.75) == "minor"
    assert classify_similarity(0.7499) == "review"

    sidebyside = sidebyside_path.read_text(encoding="utf-8")
    assert "(review" in sidebyside and "(agree" in sidebyside


def test_proofread_blocks_are_built_from_asr_segments_only(proofread_workspace):
    """Subtitles never define block boundaries (the 22.8% drift mistake)."""
    sidebyside_path, _ = _run_proofread(proofread_workspace, "BV1proofHeavy")
    boundaries = [
        line for line in sidebyside_path.read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    ]
    # The extra subtitle-only cue (13000-15000) must not open a block of its own.
    assert boundaries == [
        "## 1 [00:00:00,000-00:00:12,000] (review 0.735)",
        "## 2 [00:00:12,000-00:00:30,000] (review 0.667)",
        "## 3 [00:00:30,000-00:00:42,000] (agree 0.923)",
    ]


def test_proofread_missing_route_fails_clearly(tmp_path):
    from bili_asr.proofread import ProofreadRouteError, build_sidebyside

    (tmp_path / "transcripts" / "raw").mkdir(parents=True)
    with pytest.raises(ProofreadRouteError) as excinfo:
        build_sidebyside("BV1absent", 0, archive_root=os.fspath(tmp_path),
                         artifact_root=os.fspath(tmp_path))
    assert "BV1absent:p0" in str(excinfo.value)


# --------------------------------------------------------------------------------------
# Task 2: count-guards A/B/C.
# --------------------------------------------------------------------------------------


def test_guard_a_every_character_and_entry_in_exactly_one_block(proofread_workspace):
    sidebyside_path, _ = _run_proofread(proofread_workspace, "BV1proofHeavy")
    assert sidebyside_path.exists()  # Guard A held: run did not abort.


def test_guard_a_aborts_and_names_the_block():
    from bili_asr.proofread import GuardViolationError, guard_a_coverage

    segments = [(0, 4000, "完全相同的文本内容。"), (6000, 10000, "第二段的内容。")]
    cues = [(0, 4000, "完全相同的文本内容。"), (6000, 10000, "第二段的内容。")]
    align = guard_a_coverage("test:p0", segments, cues)
    assert [block.index for block in align.blocks] == [1, 2]
    # Hand-built violations: one ASR unit double-counted (running coverage
    # exceeds the input total first at block 2), and one ASR unit dropped
    # (coverage comes short at the last block).
    duplicated = align.accounting.asr_chars + len("完全相同的文本内容。")
    with pytest.raises(GuardViolationError) as excinfo:
        guard_a_coverage("test:p0", segments, cues, asr_chars_override=duplicated)
    # an inflated total reports the shortfall at the LAST block (chars missing
    # relative to the wrong total)
    assert "block 2" in str(excinfo.value)
    dropped = align.accounting.asr_chars - len("完全相同的文本内容。")
    with pytest.raises(GuardViolationError) as excinfo:
        guard_a_coverage("test:p0", segments, cues, asr_chars_override=dropped)
    # a deflated total trips the exceeding branch at the FIRST block
    assert "block 1" in str(excinfo.value)


def test_guard_b_banned_criterion_absent():
    """Guard B: the banned cue-centering criterion must not exist in the module source."""
    import bili_asr.proofread as proofread_module

    source = Path(proofread_module.__file__).read_text(encoding="utf-8")
    assert "midpoint" not in source
    assert "中点" not in source
    assert "outside" not in source


def test_guard_c_same_inputs_byte_identical(proofread_workspace):
    sidebyside_path, align_path = _run_proofread(proofread_workspace, "BV1proofHeavy")
    first_sidebyside = sidebyside_path.read_bytes()
    first_align = align_path.read_bytes()
    _run_proofread(proofread_workspace, "BV1proofHeavy")
    assert sidebyside_path.read_bytes() == first_sidebyside
    assert align_path.read_bytes() == first_align


# --------------------------------------------------------------------------------------
# Task 3: proofread-merge round-trip.
# --------------------------------------------------------------------------------------


def _marked_copy(sidebyside_path: Path) -> Path:
    marked = sidebyside_path.with_name(sidebyside_path.name + ".定稿")
    marked.write_bytes(sidebyside_path.read_bytes())
    return marked


def test_proofread_merge_roundtrip_with_corrections_accounting(proofread_workspace):
    from bili_asr.proofread import merge_sidebyside

    sidebyside_path, _ = _run_proofread(proofread_workspace, "BV1proofHeavy")
    marked = _marked_copy(sidebyside_path)
    text = marked.read_text(encoding="utf-8")
    text = text.replace("## 1 [00:00:00,000-00:00:12,000] (review 0.735)",
                        "## 1 [00:00:00,000-00:00:12,000] (review 0.735) >> use-asr")
    text = text.replace("## 2 [00:00:12,000-00:00:30,000] (review 0.667)",
                        "## 2 [00:00:12,000-00:00:30,000] (review 0.667) >> custom: 修正后的第二段。")
    text = text.replace("## 3 [00:00:30,000-00:00:34,000] (agree 0.923)",
                        "## 3 [00:00:30,000-00:00:34,000] (agree 0.923) >> keep")
    marked.write_text(text, encoding="utf-8")

    transcript_path, accounting_path = merge_sidebyside(
        marked, bvid="BV1proofHeavy", part=0,
        artifact_root=proofread_workspace[1])

    # Shape A: the proofread bundle is its own work directory, named for the
    # .proofread stem it writes under (not the source part's directory).
    assert transcript_path == (
        proofread_workspace[1] / "transcripts" / "BV1proofHeavy.p0.proofread" / "bundle.txt"
    )
    assert transcript_path.read_text(encoding="utf-8").splitlines() == [
        "我们来讨论劳动法的相关问题。这里涉及很多具体的案例。",
        "修正后的第二段。",
        "所以我们回到最初的问题上来。",
    ]

    accounting = json.loads(accounting_path.read_text(encoding="utf-8"))
    assert accounting["decisions"] == {"use-asr": 1, "custom": 1, "keep": 1}
    assert accounting["blocks"] == 3
    assert accounting["subtitles_asr"] == 1 and accounting["subtitles_sub"] == 2
    srt = (proofread_workspace[1] / "transcripts" / "BV1proofHeavy.p0.proofread" / "bundle.srt")
    assert srt.exists()
    assert "00:00:00,000 --> 00:00:12,000" in srt.read_text(encoding="utf-8")
    raw = json.loads((proofread_workspace[1] / "transcripts" / "BV1proofHeavy.p0.proofread" /
                      "bundle.raw.json").read_text(encoding="utf-8"))
    assert raw["source"] == "proofread" and raw["segments"][1]["text"] == "修正后的第二段。"


def test_proofread_merge_defaults_every_block_to_keep(proofread_workspace):
    from bili_asr.proofread import merge_sidebyside

    sidebyside_path, _ = _run_proofread(proofread_workspace, "BV1proofClean")
    transcript_path, accounting_path = merge_sidebyside(
        _marked_copy(sidebyside_path), bvid="BV1proofClean", part=0,
        artifact_root=proofread_workspace[1])
    # keep defaults take the subtitle column: the first three captions land in
    # block 1 (run break after 10.0 s), the last three in block 2.
    assert transcript_path.read_text(encoding="utf-8").splitlines() == [
        "大家好，我们今天继续讨论。我们先看一下基本情况。这个问题的大背景是这样的。",
        "所以我们先给出结论。结论就是前面说的那样。谢谢大家收看。",
    ]
    accounting = json.loads(accounting_path.read_text(encoding="utf-8"))
    assert accounting["decisions"] == {"keep": 2}


def test_proofread_merge_rejects_unknown_marker(proofread_workspace):
    from bili_asr.proofread import ProofreadMergeError, merge_sidebyside

    sidebyside_path, _ = _run_proofread(proofread_workspace, "BV1proofClean")
    marked = _marked_copy(sidebyside_path)
    text = marked.read_text(encoding="utf-8").replace("(agree 1.000)", "(agree 1.000) >> vote", 1)
    marked.write_text(text, encoding="utf-8")
    with pytest.raises(ProofreadMergeError):
        merge_sidebyside(marked, bvid="BV1proofClean", part=0,
                         artifact_root=proofread_workspace[1])


def test_proofread_merge_rejects_marked_block_without_side_by_side_row(tmp_path):
    from bili_asr.proofread import ProofreadMergeError, parse_sidebyside_marked

    marked = (
        "## 1 [00:00:00,000-00:00:01,000] (agree 1.000) >> keep\n"
    )
    with pytest.raises(ProofreadMergeError, match="missing side-by-side row"):
        parse_sidebyside_marked(marked)


# --------------------------------------------------------------------------------------
# CLI wiring (invocation through ``bili_asr.cli.main``).
# --------------------------------------------------------------------------------------


def _cli_env(workspace, monkeypatch):
    archive_root, artifact_root = workspace
    monkeypatch.setenv("BILI_ARTIFACT_ROOT", os.fspath(artifact_root))
    return ["--archive-root", os.fspath(archive_root)]


@pytest.mark.skip(reason="standalone proofread CLI was replaced by workflow proofread jobs")
def test_cli_proofread_writes_under_proofread_work(proofread_workspace, monkeypatch):
    from bili_asr import cli

    assert _module_cli_main.main(["proofread", "--bvid", "BV1proofClean"] + _cli_env(proofread_workspace, monkeypatch)) == 0
    expected = proofread_workspace[1] / ".tmp" / "proofread-work" / "inputs" / \
        "BV1proofClean.p0.sidebyside.md"
    assert expected.exists()
    assert _module_cli_main.main(["proofread", "--bvid", "BV1absent:p0"] +
                    _cli_env(proofread_workspace, monkeypatch)) == 1


@pytest.mark.skip(reason="standalone proofread merge CLI was replaced by workflow rendering")
def test_cli_proofread_merge_publishes_and_counts(proofread_workspace, monkeypatch):
    from bili_asr import cli

    assert _module_cli_main.main(["proofread", "--bvid", "BV1proofHeavy"] + _cli_env(proofread_workspace, monkeypatch)) == 0
    inputs_dir = proofread_workspace[1] / ".tmp" / "proofread-work" / "inputs"
    (inputs_dir / "BV1proofHeavy.p0.sidebyside.md.定稿").write_bytes(
        (inputs_dir / "BV1proofHeavy.p0.sidebyside.md").read_bytes())
    assert _module_cli_main.main(["proofread-merge", "--bvid", "BV1proofHeavy"] +
                    _cli_env(proofread_workspace, monkeypatch)) == 0
    accounting_path = proofread_workspace[1] / ".tmp" / "proofread-work" / \
        "align" / "BV1proofHeavy.p0.corrections.json"
    accounting = json.loads(accounting_path.read_text(encoding="utf-8"))
    assert accounting["decisions"] == {"keep": 3}
    assert (proofread_workspace[1] / "transcripts" /

            "BV1proofHeavy.p0.proofread" / "bundle.txt").exists()


def test_bili_asr_resolves_inside_worktree():
    import bili_asr

    worktree = Path(__file__).resolve().parents[1]
    assert Path(bili_asr.__file__).resolve().is_relative_to(worktree.resolve())
