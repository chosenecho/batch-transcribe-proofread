"""Choose a transcript version and shape its publication record.

This module is pure: it opens no connection and writes no file. The workflow
runtime reads stored versions, chooses one candidate per part, and publishes
the selected body through the archive writer. Publication paths and transcript
identity are then recorded in SQLite.

The order is applied in order: the kind rank (ASR, then uploader captions,
then machine captions), the language **family** rank (``zh``, then
``en``, then every other family), the language code ascending, and the version
**descending**.  It is total because the store's
``UNIQUE (video_part_id, source_kind, language, version)``
(``schema-transcripts.sql:25``) makes ``(source_kind, language)`` an identity and
``version`` unique inside it, so keys 1–4 separate every row a shipped writer can
store and this module adds no ``transcript_id`` tiebreak.  Two consequences are
deliberate rather than accidental: a part holding both an uploader caption and a
machine caption publishes the uploader one, and a part holding ``ai-zh`` and
``ai-en`` publishes the Chinese one — the family rank is what stops the code
order from preferring English there.

Language-family normalization and version preference live in the pure
``transcript_selection`` module. Producers, consumers and storage planners
share those rules without importing acquisition or persistence implementations.

The shared rules that are imported keep one home each:
:func:`~bili_asr.formatting.duration_s_from_ms` for the milliseconds-to-seconds
conversion, and
:func:`~bili_asr.page_identity.format_work_id` for the row's identity (§2.1,
where the read deliberately leaves ``work_id`` to Python).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from bili_asr.page_identity import format_work_id
from bili_asr.formatting import pubdate_utc
from bili_asr.artifacts import REQUIRED_ARTIFACT_KEYS as _PRODUCT_PATH_KEYS
from bili_asr.formatting import duration_s_from_ms
from bili_asr.transcript_selection import (
    LANGUAGE_FAMILY_ORDER, SOURCE_KIND_RANK, choose_transcript, language_family,
    transcript_preference_key,
)

if TYPE_CHECKING:  # types only: this module never builds or checks one (§8).
    from bili_asr.storage.models import TranscriptSegmentRecord

#: The state assigned once the archive bundle is complete.
ARCHIVED_STATUS = "archived"
#: §2.1's part context: what a candidate carries out of the read, and no more.
#: ``pubdate`` and ``video_title`` are the video's own columns; they are here
#: beside the part's because the read joins ``videos`` for them, and a caller
#: holding only a candidate cannot recover them.  The part's ``part_title`` and
#: the video's ``video_title`` are independent facts — measured, 10 of 63 stored
#: parts diverge — so both travel and neither stands in for the other (§3.1, D5).
_PART_KEYS = (
    "video_part_id",
    "bvid",
    "page_index",
    "cid",
    "part_title",
    "duration_ms",
    "pubdate",
    "video_title",
)
#: §2.1's identity columns for one stored version.
_TRANSCRIPT_KEYS = (
    "transcript_id",
    "source_kind",
    "language",
    "model_id",
    "version",
    "content_sha256",
    "created_at",
)
#: Product paths, root-relative to the write base — the five keys the
#: caller's mapping contributes to the row, and the only ones it may.


@dataclass(frozen=True)
class Candidate:
    """One part to publish, with the transcript identity that won it.

    ``part`` carries §2.1's part columns, plus the two the read joins ``videos``
    for (``pubdate``, ``video_title``), and ``transcript`` that version's
    identity columns alone, both copied out of the read's row, so a caller can
    hand ``part``/``transcript`` straight to :func:`projection_row` and read the
    winner off the candidate without the row's other columns travelling with it.
    ``work_id`` is built here by :func:`~bili_asr.page_identity.format_work_id`:
    the SQL read does not select it, and the storage writer validates the same
    Python identity when it persists a publication.
    """

    work_id: str
    part: Mapping[str, Any]
    transcript: Mapping[str, Any]


def _language_family(row: Mapping[str, Any]) -> str:
    """Compatibility wrapper for callers of the original row-shaped helper."""

    return language_family(row["language"], row["source_kind"] == "subtitle-ai")


def ordered_candidates(
    rows: Iterable[Mapping[str, Any]], limit: int | None = None
) -> tuple[Candidate, ...]:
    """Return one candidate per part, in the order the parts first appear.

    ``rows`` is §2.1's read handed over as mappings: one row per stored
    transcript version, in the repository's locked order (``bvid``,
    ``page_index``, then the identity keys), which is why the parts come back in
    that same order — the order rule has one home, in the read (§2.1), and this
    function only keeps the order it is given.  The winner inside a part is
    §3.2's minimum, so it does not depend on the row order at all.

    ``limit`` bounds the run in **parts**: the first ``limit`` candidates, never
    the first ``limit`` rows, because a part is a candidate once whatever number
    of versions it holds.  The validation is the shipped
    ``list_pending_subtitle_parts`` discipline — ``TypeError`` for a non-integer,
    ``ValueError`` for a non-positive one — which is also the shape the command's
    own ``--limit-parts`` exit ``1`` rests on (§2.2).
    """

    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer or None")
        if limit < 1:
            raise ValueError("limit must be a positive integer")

    winners: dict[Any, Mapping[str, Any]] = {}
    for row in rows:
        part_id = row["video_part_id"]
        held = winners.get(part_id)
        if held is None or transcript_preference_key(row) < transcript_preference_key(held):
            winners[part_id] = row

    candidates = tuple(
        Candidate(
            work_id=(format_work_id(row["bvid"], row["page_index"])
                     if row["bvid"] is not None else str(row["work_id"])),
            part={key: row[key] for key in _PART_KEYS},
            transcript={key: row[key] for key in _TRANSCRIPT_KEYS},
        )
        for row in winners.values()
    )
    return candidates if limit is None else candidates[:limit]


def writer_segments(
    segments: Sequence[TranscriptSegmentRecord],
) -> list[dict[str, Any]]:
    """Return one transcript's stored segments in the archive writer's shape (§3.3).

    The store holds ``(start_ms, end_ms, text)`` and the writer consumes
    ``{"start": <seconds>, "end": <seconds>, "text": <verbatim>}``, in the
    ordinal order the read already returned.  ``/1000`` exactly: the SRT
    :func:`cues.segments_to_srt` rounds ``seconds * 1000`` back to an integer, so a
    cue's printed times are the store's milliseconds, with no lost millisecond
    and no accumulated drift.

    An empty sequence is refused here rather than published: the writer would
    emit an empty ``srt``/``txt`` and a sidecar with no cue, and §7 has the
    command name this ``ValueError`` as its ``empty_transcript`` reason.  A
    stored transcript is never empty (``storage/models.py:382-383``), so the
    branch is this module's refusal of a shape the store cannot deliver rather
    than a live path.
    """

    if not segments:
        raise ValueError("a stored transcript carries no segment to publish")
    return [
        {
            "start": segment.start_ms / 1000,
            "end": segment.end_ms / 1000,
            "text": segment.text,
        }
        for segment in segments
    ]


def projection_row(
    part: Mapping[str, Any],
    transcript: Mapping[str, Any],
    paths: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the SQLite publication projection for one selected candidate.

    Exactly sixteen keys: the nine store-derived fields the
    stored part contributes with this publication's state and five product paths copied from
    ``paths`` root-relative to the write base, and the winning identity in the
    readers' vocabulary — ``source`` and ``language``.

    Nothing else is added and nothing is guessed: no ``audio_path`` (a caption
    has no audio), nor ASR execution details that are not stored. ``duration_s``
    and ``pubdate_str`` are derived renderings: a floored duration and the
    video's **UTC** calendar date. Only the five product keys are read out of
    ``paths``, so a caller's mapping may hold more (the marker's path, say)
    without any of it reaching the manifest.
    """

    bvid = part["bvid"]
    page_index = part["page_index"]
    pubdate = part["pubdate"]
    row = {
        "work_id": format_work_id(bvid, page_index),
        "bvid": bvid,
        "page_index": page_index,
        "cid": part["cid"],
        "title": part["part_title"],
        "duration_s": duration_s_from_ms(part["duration_ms"]),
        "pubdate": pubdate,
        "pubdate_str": pubdate_utc(pubdate),
        "status": ARCHIVED_STATUS,
        "source": transcript["source_kind"],
        "language": transcript["language"],
    }
    for key in _PRODUCT_PATH_KEYS:
        row[key] = paths[key]
    return row


__all__ = [
    "ARCHIVED_STATUS",
    "Candidate",
    "LANGUAGE_FAMILY_ORDER",
    "SOURCE_KIND_RANK",
    "ordered_candidates",
    "choose_transcript",
    "transcript_preference_key",
    "language_family",
    "projection_row",
    "writer_segments",
]
