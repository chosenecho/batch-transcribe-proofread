"""``bili-asr proofread`` — the merge-v2 proofread method as a package module.

Two routes into one side-by-side table; no route is ground truth.

* Blocks are built from **ASR VAD segments only**: a new block starts when the
  next segment's ``start_ms`` exceeds the current block's ``end_ms`` by more
  than :data:`BLOCK_GAP_MS`.  Subtitles never define block boundaries — the
  2026-09-22 wave's 22.8% drift came from letting caption cues split blocks.
* A block's *target span* runs from its first segment's start to the first
  gap after it — the next block's first segment start, or, for the last
  block, the silence up to the ~12 s plateau when its own VAD run ended
  early.  Caption entries attach to exactly one block by interval overlap
  (largest first, tie broken by earliest span start).  An entry overlapping
  no target span is *unassigned* — a counted route disagreement, never
  dropped silently: 2026-09-22 showed the true gap count is ~0, and every
  unattached bucket in that wave was a counting artifact, not a fact.
* Similarity between the block's ASR text and the concatenation of its caption
  entries is ``difflib.SequenceMatcher(autojunk=False)`` — the autojunk default
  was a documented 2026-09-18 trap that under-reports common-character
  similarity.  ≥0.85 agree / 0.75–0.85 minor / <0.75 review.

Merge markers (the contract ``proofread-merge`` parses) live at the end of a
block heading line, after ``>>``:

* ``>> keep`` — accept the 字幕 column (default for every unmarked block);
* ``>> use-asr`` — accept the ASR column;
* ``>> use-sub`` — accept the 字幕 column, spelled out;
* ``>> custom: <text>`` — replace the block with the payload text.

The marker payload is plain text on the heading line: it survives any markdown
viewer and diffs as a one-line change.

Count-guards (the 2026-09-22 lessons, Task 2 of plan 20260928-proofread-pipeline):

* Guard A — coverage: every subtitle entry and every ASR character appears in
  exactly one block (:func:`guard_a_coverage`); a violation raises
  :class:`GuardViolationError` naming the block.
* Guard B — no fabrication: the banned cue-centering criterion (the one that
  fabricated 23–27% false gaps on 2026-09-22) must not exist.  It is banned
  *in code*: the module never computes a cue center, and
  ``tests/test_proofread.py``
  (``test_guard_b_banned_criterion_absent``) greps this source for
  the banned tokens and fails if they return.
* Guard C — stability: re-running on the same inputs is byte-identical
  (deterministic ordering throughout; no wall clock, no randomness).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Mapping, Sequence

from .archive import bundle_paths_for_stem
from .cues import CueParseError
from .cues import read_route_ms as _read_route_ms

#: Blocks are built from ASR VAD segments alone: a segment whose ``start_ms``
#: exceeds the block's current ``end_ms`` by more than this opens a new block.
BLOCK_GAP_MS = 1500
#: The measured plateau for the merge-v2 method: 12→16 s adds nothing.
BLOCK_TARGET_MS = 12_000
#: Similarity thresholds, ratified by the 2026-09-22 corpus measurement.
AGREE_THRESHOLD = 0.85
MINOR_THRESHOLD = 0.75

_WORK_SUBDIR = os.path.join(".tmp", "proofread-work")

_MARKER_RE = re.compile(r"\s*>>\s*([a-z-]+)(?::\s?(.*))?$")


class ProofreadRouteError(LookupError):
    """A required route (ASR sidecar or caption transcript) is missing."""


class GuardViolationError(AssertionError):
    """A count-guard failed; the message names the offending block."""


class ProofreadMergeError(ValueError):
    """A completed side-by-side定稿 could not be merged."""


# --------------------------------------------------------------------------------------
# Alignment data model.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Cue:
    """One unit on the millisecond timeline: an ASR VAD segment or a caption entry."""

    index: int  # 1-based, input order within its own side
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class Block:
    """One aligned block: a run of ASR VAD segments plus the caption entries inside it."""

    index: int  # 1-based, block order
    start_ms: int
    end_ms: int
    target_end_ms: int  # first gap after the block: next block's start or its own end
    asr_indexes: tuple[int, ...]
    subtitle_indexes: tuple[int, ...]
    asr_text: str
    subtitle_text: str
    similarity: float
    decision: str  # "agree" | "minor" | "review"


@dataclass(frozen=True)
class AlignmentAccounting:
    """Everything that went in, and into which bucket each unit went."""

    asr_segments: int
    asr_chars: int
    subtitle_entries: int
    subtitle_assigned: int
    subtitle_unassigned: int
    unassigned_subtitle_indexes: tuple[int, ...]


@dataclass(frozen=True)
class Alignment:
    """A complete alignment of one part's two routes, with both buckets explicit."""

    work_id: str
    asr_cues: tuple[Cue, ...]
    subtitle_cues: tuple[Cue, ...]
    blocks: tuple[Block, ...]
    unassigned_subtitles: tuple[Cue, ...]
    accounting: AlignmentAccounting


def _normalize_cues(raw: Sequence[tuple[int, int, str]], kind: str) -> tuple[Cue, ...]:
    """Wrap ``(start_ms, end_ms, text)`` triples as ordered :class:`Cue` objects.

    Input must already be in time order: a unit starting before its predecessor
    is a producer defect and raises :class:`ValueError` naming the index, never
    silently re-sorted.  Text is kept verbatim.
    """

    cues: list[Cue] = []
    previous_start: int | None = None
    for position, (start_ms, end_ms, text) in enumerate(raw, start=1):
        start, end = int(start_ms), int(end_ms)
        if end <= start:
            raise ValueError(
                f"{kind} index {position} has a degenerate interval: "
                f"start_ms={start} end_ms={end} (end_ms must be greater than start_ms)"
            )
        if previous_start is not None and start < previous_start:
            raise ValueError(
                f"{kind} index {position} is out of time order: start_ms={start} "
                f"precedes the previous start_ms={previous_start}"
            )
        previous_start = start
        cues.append(Cue(index=position, start_ms=start, end_ms=end, text=str(text)))
    return tuple(cues)


def classify_similarity(similarity: float) -> str:
    """Map one similarity score onto the ratified decision bands."""

    if similarity >= AGREE_THRESHOLD:
        return "agree"
    if similarity >= MINOR_THRESHOLD:
        return "minor"
    return "review"


def _build_block_runs(
    asr_cues: Sequence[Cue],
) -> list[tuple[Cue, ...]]:
    """Cluster ASR VAD segments into block runs; subtitles never split a block."""

    runs: list[list[Cue]] = []
    for cue in asr_cues:
        if not runs or cue.start_ms - runs[-1][-1].end_ms > BLOCK_GAP_MS:
            runs.append([cue])
        else:
            runs[-1].append(cue)
    return runs


def _similarity(left: str, right: str) -> float:
    """Similarity between two texts with ``autojunk=False``.

    The default heuristic junk-disbands characters occurring in more than 1% of
    the text, which under-reports common-character similarity on the short
    block texts this method aligns — a documented 2026-09-18 trap.
    """

    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def align_routes(
    work_id: str,
    asr_segments: Sequence[tuple[int, int, str]],
    subtitle_entries: Sequence[tuple[int, int, str]],
) -> Alignment:
    """Align one part's two routes into blocks with similarity decisions.

    Every unit is accounted for by counting the real tuples: ASR segments
    belong to their block by construction, and a caption entry is either
    assigned to exactly one block (largest overlap with the block's target
    span, ties broken by earliest block start) or reported in
    ``unassigned_subtitles`` and counted there — the count is never produced
    by subtracting one input total from another.
    """

    asr_cues = _normalize_cues(asr_segments, "asr segment")
    subtitle_cues = _normalize_cues(subtitle_entries, "subtitle entry")
    runs = _build_block_runs(asr_cues)

    # Target spans: a block owns [first segment start, first gap after it),
    # where the gap is the next run's first segment start — never that run's
    # segments: a later segment inside the pause belongs to the later run, and
    # the recognizer's own gap (BLOCK_GAP_MS) already marks the speech run as
    # over, so a run with a successor claims no silence past its start.  Only
    # the last block, with no successor, claims the silence up to the measured
    # ~12 s plateau when its own VAD run ended early.
    spans: list[tuple[int, int]] = []
    for position, run in enumerate(runs):
        run_end = run[-1].end_ms
        if position + 1 < len(runs):
            target_end = runs[position + 1][0].start_ms
        else:
            target_end = max(run_end, run[0].start_ms + BLOCK_TARGET_MS)
        spans.append((run[0].start_ms, target_end))

    assigned: list[list[int]] = [[] for _ in runs]
    unassigned: list[int] = []
    for entry in subtitle_cues:
        best_position: int | None = None
        best_rank: tuple[int, int] | None = None  # (overlap, -span start)
        for position, (span_start, span_end) in enumerate(spans):
            overlap = min(entry.end_ms, span_end) - max(entry.start_ms, span_start)
            if overlap <= 0:
                continue
            rank = (overlap, -span_start)
            if best_rank is None or rank > best_rank:
                best_rank = rank
                best_position = position
        if best_position is None:
            unassigned.append(entry.index)
        else:
            assigned[best_position].append(entry.index)

    subtitle_by_index = {cue.index: cue for cue in subtitle_cues}
    blocks: list[Block] = []
    for position, run in enumerate(runs, start=1):
        asr_text = "".join(cue.text for cue in run)
        subtitle_text = "".join(
            subtitle_by_index[index].text for index in assigned[position - 1]
        )
        similarity = _similarity(asr_text, subtitle_text) if subtitle_text else 0.0
        blocks.append(
            Block(
                index=position,
                start_ms=run[0].start_ms,
                end_ms=run[-1].end_ms,
                target_end_ms=spans[position - 1][1],
                asr_indexes=tuple(cue.index for cue in run),
                subtitle_indexes=tuple(assigned[position - 1]),
                asr_text=asr_text,
                subtitle_text=subtitle_text,
                similarity=similarity,
                decision=classify_similarity(similarity),
            )
        )

    accounting = AlignmentAccounting(
        asr_segments=len(asr_cues),
        asr_chars=sum(len(cue.text) for cue in asr_cues),
        subtitle_entries=len(subtitle_cues),
        subtitle_assigned=sum(len(indexes) for indexes in assigned),
        subtitle_unassigned=len(unassigned),
        unassigned_subtitle_indexes=tuple(unassigned),
    )
    return Alignment(
        work_id=work_id,
        asr_cues=asr_cues,
        subtitle_cues=subtitle_cues,
        blocks=tuple(blocks),
        unassigned_subtitles=tuple(subtitle_by_index[index] for index in unassigned),
        accounting=accounting,
    )


# --------------------------------------------------------------------------------------
# Guard A: coverage — every subtitle entry and every ASR character in exactly one block.
# --------------------------------------------------------------------------------------


def guard_a_coverage(
    work_id: str,
    asr_segments: Sequence[tuple[int, int, str]],
    subtitle_entries: Sequence[tuple[int, int, str]],
    *,
    asr_chars_override: int | None = None,
) -> Alignment:
    """Align and assert Guard A; raise :class:`GuardViolationError` naming the block.

    Two independent counts, both obtained from the real tuples:

    * every ASR segment's text must contribute its character count to the
      blocks (a segment missing from every block names the first block whose
      coverage drops);
    * every subtitle entry must be assigned to exactly one block (an entry
      attached twice or to none names the block at fault).
    """

    alignment = align_routes(work_id, asr_segments, subtitle_entries)

    asr_chars = (
        alignment.accounting.asr_chars
        if asr_chars_override is None
        else int(asr_chars_override)
    )
    block_chars = sum(len(block.asr_text) for block in alignment.blocks)
    if block_chars != asr_chars:
        expected_by_block = [
            sum(len(asr_text) for asr_text in (
                alignment.asr_cues[index - 1].text for index in block.asr_indexes
            ))
            for block in alignment.blocks
        ]
        covered = 0
        for block, expected in zip(alignment.blocks, expected_by_block):
            covered += expected
            if covered > asr_chars:
                raise GuardViolationError(
                    f"Guard A: work_id={work_id} block {block.index}: ASR char coverage "
                    f"{covered} exceeds the input total {asr_chars} — an ASR unit is "
                    f"counted in more than one block"
                )
        last = alignment.blocks[-1].index if alignment.blocks else 0
        raise GuardViolationError(
            f"Guard A: work_id={work_id} block {last}: "
            f"{asr_chars - block_chars} ASR character(s) appear in no block"
        )

    seen: set[int] = set()
    for block in alignment.blocks:
        for index in block.subtitle_indexes:
            if index in seen:
                raise GuardViolationError(
                    f"Guard A: work_id={work_id} block {block.index}: subtitle entry "
                    f"{index} appears in more than one block"
                )
            seen.add(index)
    expected = set(cue.index for cue in alignment.subtitle_cues)
    if seen != expected:
        missing = sorted(expected - seen)
        first_missing = missing[0] if missing else 0
        last = alignment.blocks[-1].index if alignment.blocks else 0
        raise GuardViolationError(
            f"Guard A: work_id={work_id} block {last}: {len(missing)} subtitle "
            f"entr(ies) — first is entry {first_missing} — appear in no block"
        )
    return alignment


# --------------------------------------------------------------------------------------
# Rendering: the side-by-side table and the alignment jsonl (Guard C: byte-identical).
# --------------------------------------------------------------------------------------


def _fmt_srt_time_ms(milliseconds: int) -> str:
    hours, remainder = divmod(max(0, int(milliseconds)), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def render_sidebyside(alignment: Alignment) -> str:
    """Render the side-by-side table: ``time | ASR | 字幕`` per block.

    Pure mechanical alignment — the table shows both columns and the decision,
    it resolves nothing.  The format is a contract: ``proofread-merge`` parses
    the block headings (``## <n> [<start>-<end>] (<decision> <score>)``) and
    the ``>>`` marker suffix, and reads the column bodies for ``keep`` /
    ``use-sub`` / ``use-asr``.
    """

    lines = [
        f"# proofread side-by-side: {alignment.work_id}",
        "",
        "columns: time | ASR | 字幕 — mechanical alignment, no adjudication.",
        "decision markers: `>> keep` (default), `>> use-asr`, `>> use-sub`, "
        "`>> custom: <text>` appended to a block heading.",
        "",
        "| time | ASR | 字幕 |",
        "| --- | --- | --- |",
    ]
    for block in alignment.blocks:
        time_cell = (
            f"{_fmt_srt_time_ms(block.start_ms)}-{_fmt_srt_time_ms(block.target_end_ms)}"
        )
        lines.append(
            f"## {block.index} [{time_cell}] ({block.decision} {block.similarity:.3f})"
        )
        lines.append(f"| {time_cell} | {block.asr_text} | {block.subtitle_text} |")
    for cue in alignment.unassigned_subtitles:
        time_cell = f"{_fmt_srt_time_ms(cue.start_ms)}-{_fmt_srt_time_ms(cue.end_ms)}"
        lines.append(f"| {time_cell} |  | ⚠️ {cue.text} |")
    return "\n".join(lines) + "\n"


def render_alignment_jsonl(alignment: Alignment) -> str:
    """Render the alignment jsonl: one header object, then one record per block."""

    accounting = alignment.accounting
    lines = [
        json.dumps(
            {
                "kind": "header",
                "work_id": alignment.work_id,
                "blocks": len(alignment.blocks),
                "asr_segments": accounting.asr_segments,
                "asr_chars": accounting.asr_chars,
                "subtitle_entries": accounting.subtitle_entries,
                "subtitle_assigned": accounting.subtitle_assigned,
                "subtitle_unassigned": accounting.subtitle_unassigned,
                "block_gap_ms": BLOCK_GAP_MS,
                "block_target_ms": BLOCK_TARGET_MS,
                "agree_threshold": AGREE_THRESHOLD,
                "minor_threshold": MINOR_THRESHOLD,
                "sequence_matcher_autojunk": False,
            },
            ensure_ascii=False,
        )
    ]
    for block in alignment.blocks:
        lines.append(
            json.dumps(
                {
                    "kind": "block",
                    "index": block.index,
                    "start_ms": block.start_ms,
                    "end_ms": block.target_end_ms,
                    "segments": block.asr_indexes,
                    "subtitles": block.subtitle_indexes,
                    "decision": block.decision,
                    "similarity": round(block.similarity, 6),
                    "unassigned_subtitles": list(accounting.unassigned_subtitle_indexes),
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------
# Route readers (the shipped readers are reused, never reimplemented).
# --------------------------------------------------------------------------------------


def read_asr_route_ms(artifact_root: Path, bvid: str, part: int) -> list[tuple[int, int, str]]:
    """Read the archived ASR transcript for one part: its ``raw`` sidecar.

    The sidecar is the bundle ``write_archive`` published
    (``transcripts/<bvid>.p<N>/bundle.raw.json``); seconds in the file convert to
    milliseconds exactly (the SRT renderer rounds ``seconds * 1000`` back, so
    no millisecond is lost — the same rule ``writer_segments`` pins).

    The sidecar's own ``source`` is checked, and only ``asr`` is this route.  A
    caption-derived sidecar (``subtitle-ai`` / ``subtitle``) or a merged one
    (``proofread``) holds the *other* route's text, so reading it here would put
    one route on both sides of the table: every block agrees with itself, the
    table adjudicates nothing, and — worse — it reports success.  The refusal
    therefore names the source actually found, so the operator can see which
    file was picked up rather than having to infer it.
    """

    raw_path = bundle_paths_for_stem(artifact_root, f"{bvid}.p{part}")["raw_path"]
    try:
        return _read_route_ms(raw_path, require_source="asr", label=f"{bvid}:p{part}")
    except CueParseError as exc:
        raise ProofreadRouteError(exc.detail) from exc


def read_subtitle_route_ms(
    archive_root: Path, artifact_root: Path, bvid: str, part: int
) -> list[tuple[int, int, str]]:
    """Read the archived AI/CC subtitles for one part from the transcript store.

    The winner follows the publish projection's own rule — ``cc`` before
    ``ai``, the zh family before the rest, then language, then newest version
    (``services.transcript_projection.ordered_candidates``) — and the body is
    the store's own read (``TranscriptRepository.read_transcript``), never a
    re-parse of a loose file.
    """

    import sqlite3

    from .storage import SchemaContractError, TranscriptRepository
    from .storage.models import ALLOWED_CAPTION_SOURCE_KINDS

    db_path = archive_root / "archive.db"
    if not db_path.is_file():
        raise ProofreadRouteError(f"{bvid}:p{part}: missing caption route (no {db_path})")
    connection = None
    try:
        from .artifact_root import ArtifactRoots

        resolved = ArtifactRoots.of(archive_root).archive_root / "archive.db"
        connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA schema_version").fetchone()
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            connection.close()
        raise ProofreadRouteError(
            f"{bvid}:p{part}: unreadable caption route ({type(exc).__name__})"
        ) from exc
    try:
        repository = TranscriptRepository(connection)
        rows = repository.list_stored_transcripts(bvid, part)
        winners: dict[Any, Any] = {}
        for row in rows:
            if row["source_kind"] not in ALLOWED_CAPTION_SOURCE_KINDS:
                continue
            key = (row["source_kind"], row["language"], -int(row["version"]))
            if key not in winners:
                winners[key] = row
        if not winners:
            raise ProofreadRouteError(
                f"{bvid}:p{part}: missing caption route (no subtitle-ai/subtitle-cc transcript)"
            )
        candidates = []
        for row in winners.values():
            record = repository.read_transcript(
                int(row["video_part_id"]), str(row["source_kind"]),
                str(row["language"]), int(row["version"]),
            )
            if record is not None:
                candidates.append(record)
        if not candidates:
            raise ProofreadRouteError(f"{bvid}:p{part}: missing caption route (no readable body)")

        rank = {"subtitle-cc": 0, "subtitle-ai": 1}

        def family_rank(record: Any) -> int:
            language = record.language.strip().lower()
            if record.source_kind == "subtitle-ai" and language.startswith("ai-"):
                language = language[3:]
            try:
                return ("zh", "en").index(language.split("-", 1)[0])
            except ValueError:
                return 2

        winner = min(
            candidates,
            key=lambda record: (
                rank[record.source_kind],
                family_rank(record),
                record.language,
                -record.version,
            ),
        )
        return [
            (segment.start_ms, segment.end_ms, segment.text)
            for segment in winner.segments
        ]
    except (sqlite3.Error, SchemaContractError, ValueError, TypeError, OverflowError) as exc:
        raise ProofreadRouteError(
            f"{bvid}:p{part}: unreadable caption route ({type(exc).__name__})"
        ) from exc
    finally:
        connection.close()


# --------------------------------------------------------------------------------------
# Task 1: the proofread command.
# --------------------------------------------------------------------------------------


def build_sidebyside(
    bvid: str,
    part: int,
    *,
    archive_root: str | os.PathLike[str],
    artifact_root: str | os.PathLike[str],
    asr_root: str | os.PathLike[str] | None = None,
    caption_root: str | os.PathLike[str] | None = None,
) -> tuple[Path, Path]:
    """Build the side-by-side table and the alignment jsonl for one part.

    Writes land only under ``<artifact-root>/.tmp/proofread-work/`` (inputs +
    align); nothing is written under ``transcripts/{srt,txt,md,raw}`` — those
    families belong to ``publish-transcripts`` and to ``proofread-merge``.
    Guard A runs before anything is written: a coverage violation aborts with
    :class:`GuardViolationError` naming the block and no artifact appears.

    The two routes are read from two roots, and the roots are separable because
    the routes really are separable: an end-to-end run that exhausts the caption
    route on one root leaves the ASR sidecar on another, and demanding one root
    for both refuses a pair of inputs that exist and are perfectly readable.
    ``asr_root`` (the ASR sidecar) and ``caption_root`` (the transcript store)
    each default to what they have always been — ``artifact_root`` and
    ``archive_root`` respectively — so an invocation that names neither reads
    exactly what it read before.  Writes follow ``artifact_root`` alone: the new
    parameters *move reads*, they do not create a second output location.
    """

    for label, value in (("asr-root", asr_root), ("caption-root", caption_root)):
        if value is not None and not os.fspath(value).strip():
            raise ProofreadRouteError(f"{label} must name a non-empty directory")
    archive_root_path = Path(archive_root)
    artifact_root_path = Path(artifact_root)
    work_id = f"{bvid}:p{part}"
    asr_route = read_asr_route_ms(
        artifact_root_path if asr_root is None else Path(asr_root), bvid, part
    )
    subtitle_route = read_subtitle_route_ms(
        archive_root_path if caption_root is None else Path(caption_root),
        artifact_root_path,
        bvid,
        part,
    )
    alignment = guard_a_coverage(work_id, asr_route, subtitle_route)

    work_dir = artifact_root_path / _WORK_SUBDIR
    inputs_dir = work_dir / "inputs"
    align_dir = work_dir / "align"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    align_dir.mkdir(parents=True, exist_ok=True)
    sidebyside_path = inputs_dir / f"{bvid}.p{part}.sidebyside.md"
    align_path = align_dir / f"{bvid}.p{part}.alignment.jsonl"
    sidebyside_path.write_text(render_sidebyside(alignment), encoding="utf-8")
    align_path.write_text(render_alignment_jsonl(alignment), encoding="utf-8")
    return sidebyside_path, align_path


# --------------------------------------------------------------------------------------
# Task 3: proofread-merge — a completed side-by-side定稿 back into a transcript artifact.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MergedBlock:
    """One block's adjudicated result: its final text, its source decision."""

    index: int
    start_ms: int
    end_ms: int
    decision: str
    text: str


@dataclass(frozen=True)
class MergeResult:
    """The merged part: final blocks, the published paths, the corrections accounting."""

    blocks: tuple[MergedBlock, ...]
    transcript_path: Path
    srt_path: Path
    raw_path: Path
    corrections_path: Path
    accounting: Mapping[str, Any]


_DEFAULT_DECISION = "keep"
_VALID_DECISIONS = ("keep", "use-asr", "use-sub", "custom")


def _parse_marker(heading: str, block_index: int) -> tuple[str, str | None]:
    """Parse the ``>>`` marker on one block heading; defaults to ``keep``."""

    marker = _MARKER_RE.search(heading)
    if marker is None:
        return _DEFAULT_DECISION, None
    decision = marker.group(1)
    payload = marker.group(2)
    if decision not in _VALID_DECISIONS:
        raise ProofreadMergeError(
            f"block {block_index}: unknown decision marker {decision!r} "
            f"(expected one of {', '.join(_VALID_DECISIONS)})"
        )
    if decision == "custom" and not (payload or "").strip():
        raise ProofreadMergeError(f"block {block_index}: custom decision carries no text")
    return decision, payload


def parse_sidebyside_marked(text: str) -> tuple[MergedBlock, ...]:
    """Parse a completed定稿: block headings with markers + their table rows.

    The block body is the table row directly under its heading; the final text
    is the marker payload (``custom``) or the printed column body (``keep`` /
    ``use-sub`` = 字幕 column, ``use-asr`` = ASR column).  Every unmarked block
    defaults to ``keep``.
    """

    lines = text.splitlines()
    blocks: list[MergedBlock] = []
    position = 0
    while position < len(lines):
        line = lines[position]
        if line.startswith("## "):
            heading = line[3:]
            heading_match = re.match(r"(\d+) \[(\d\d:\d\d:\d\d,\d\d\d)-(\d\d:\d\d:\d\d,\d\d\d)\]", heading)
            if heading_match is None:
                raise ProofreadMergeError(f"unparseable block heading: {line!r}")
            block_index = int(heading_match.group(1))
            start_ms = _parse_srt_time_ms(heading_match.group(2))
            end_ms = _parse_srt_time_ms(heading_match.group(3))
            decision, payload = _parse_marker(heading, block_index)
            body = ""
            if position + 1 < len(lines) and lines[position + 1].startswith("| "):
                cells = lines[position + 1].split("|")
                if len(cells) < 4:
                    raise ProofreadMergeError(
                        f"block {block_index}: side-by-side row has fewer than three columns"
                    )
                asr_text = cells[2].strip()
                subtitle_text = cells[3].strip()
                if decision in ("keep", "use-sub"):
                    body = subtitle_text
                elif decision == "use-asr":
                    body = asr_text
                else:
                    body = (payload or "").strip()
                position += 1
            else:
                # A marker without its adjacent table row is not a valid
                # completed proofread block.  Treating it as an empty body
                # silently drops the block from the published transcript.
                raise ProofreadMergeError(
                    f"block {block_index}: missing side-by-side row"
                )
            blocks.append(
                MergedBlock(
                    index=block_index, start_ms=start_ms, end_ms=end_ms,
                    decision=decision, text=body,
                )
            )
        position += 1
    if not blocks:
        raise ProofreadMergeError("no block found in the completed side-by-side")
    expected = list(range(1, len(blocks) + 1))
    if [block.index for block in blocks] != expected:
        raise ProofreadMergeError(
            "block indexes are not the contiguous 1..N of a proofread table"
        )
    return tuple(blocks)


def _parse_srt_time_ms(value: str) -> int:
    hours, minutes, rest = value.split(":")
    secs, millis = rest.split(",")
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(secs) * 1_000
        + int(millis)
    )


def merge_sidebyside(
    marked_path: str | os.PathLike[str],
    *,
    bvid: str,
    part: int,
    artifact_root: str | os.PathLike[str],
) -> tuple[Path, Path]:
    """Merge a completed side-by-side定稿 into the final transcript artifact.

    Writes the ``.proofread`` variants of the published transcript families
    (``srt``/``txt``/``raw`` under ``<artifact-root>/transcripts/``) plus the
    corrections accounting beside the alignment jsonl.  Blocks are concatenated
    with a newline between them; an empty block contributes no line.  Returns
    ``(txt_path, corrections_path)``.
    """

    from .cues import segments_to_srt, segments_to_txt

    marked = Path(marked_path)
    try:
        text = marked.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProofreadMergeError(f"cannot read completed side-by-side: {exc}") from exc
    blocks = parse_sidebyside_marked(text)

    artifact_root_path = Path(artifact_root)
    bundle_dir = artifact_root_path / "transcripts"
    segments = [
        {"start": block.start_ms / 1000, "end": block.end_ms / 1000, "text": block.text}
        for block in blocks
        if block.text
    ]
    if not segments:
        raise ProofreadMergeError("every block is empty; refusing to publish an empty transcript")

    stem = f"{bvid}.p{part}.proofread"
    # Shape A: the proofread bundle owns its own directory, with the four
    # artifacts as fixed names inside it -- the same rule write_archive uses.
    proofread_paths = bundle_paths_for_stem(artifact_root_path, stem)
    transcript_path = proofread_paths["txt_path"]
    srt_path = proofread_paths["srt_path"]
    raw_path = proofread_paths["raw_path"]
    for directory in (transcript_path.parent, srt_path.parent, raw_path.parent):
        directory.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(segments_to_txt(segments) + "\n", encoding="utf-8")
    srt_path.write_text(segments_to_srt(segments), encoding="utf-8")
    raw_document = {
        "segments": segments,
        "source": "proofread",
        "bvid": bvid,
        "part": part,
        "decisions": {block.index: block.decision for block in blocks},
    }
    raw_path.write_text(
        json.dumps(raw_document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    decisions: dict[str, int] = {}
    for block in blocks:
        decisions[block.decision] = decisions.get(block.decision, 0) + 1
    accounting = {
        "bvid": bvid,
        "part": part,
        "blocks": len(blocks),
        "decisions": decisions,
        "subtitles_asr": sum(1 for block in blocks if block.decision == "use-asr"),
        "subtitles_sub": sum(
            1 for block in blocks if block.decision in ("keep", "use-sub", "custom")
        ),
    }
    corrections_path = (
        artifact_root_path / _WORK_SUBDIR / "align" / f"{bvid}.p{part}.corrections.json"
    )
    corrections_path.parent.mkdir(parents=True, exist_ok=True)
    corrections_path.write_text(
        json.dumps(accounting, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return transcript_path, corrections_path
