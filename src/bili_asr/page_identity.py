"""Canonical per-page identity for the archive ledger and filesystem.

`work_id` uses `:` as identity punctuation and must never appear in paths.
Filesystem names use `artifact_stem` only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
import re

from bili_asr.platform_identity import ContentRef

_WORK_ID_RE = re.compile(r"^(?P<bvid>[^:]+):p(?P<page_index>0|[1-9]\d*)$")


@dataclass(frozen=True)
class PageIdentity:
    work_id: str
    bvid: str
    page_index: int
    cid: int
    page_label: str = ""

    @property
    def content_ref(self) -> ContentRef:
        """Runtime source reference; legacy fields and file keys stay unchanged."""

        return ContentRef("bilibili", self.bvid, self.page_index)


def format_work_id(bvid: str, page_index: int) -> str:
    if not bvid:
        raise ValueError("bvid is required")
    if page_index < 0:
        raise ValueError(f"page_index must be >= 0, got {page_index}")
    if ":" in bvid:
        raise ValueError("bvid must not contain ':'")
    return f"{bvid}:p{page_index}"


def parse_work_id(work_id: str) -> tuple[str, int]:
    match = _WORK_ID_RE.match(work_id)
    if not match:
        raise ValueError(f"invalid work_id {work_id!r}")
    return match.group("bvid"), int(match.group("page_index"))


def writeback_identity(entry: Mapping[str, object]) -> tuple[str, int] | None:
    """Resolve a store write from the canonical work id, never a guessed p0.

    A legacy video-level row can still publish its bundle, but cannot attribute
    that transcript to a particular part. Conflicting identities also skip.
    """
    if entry.get("unresolved") or not isinstance(entry.get("work_id"), str):
        return None
    try:
        bvid, page_index = parse_work_id(entry["work_id"])
    except ValueError:
        return None
    if entry.get("bvid") not in (None, "", bvid):
        return None
    return bvid, page_index


def artifact_stem(identity: PageIdentity) -> str:
    stem = f"{identity.bvid}.p{identity.page_index}"
    if ":" in stem:
        raise ValueError("artifact_stem must not contain ':'")
    return stem


def page_query_index(page_index: int) -> int:
    """1-based `?p=` query value for public Bilibili URLs."""
    if page_index < 0:
        raise ValueError(f"page_index must be >= 0, got {page_index}")
    return page_index + 1


def page_identity(
    bvid: str,
    page_index: int,
    cid: int,
    page_label: str = "",
) -> PageIdentity:
    return PageIdentity(
        work_id=format_work_id(bvid, page_index),
        bvid=bvid,
        page_index=page_index,
        cid=cid,
        page_label=page_label,
    )


def apply_identity(entry: dict, identity: PageIdentity) -> dict:
    """Copy canonical page fields onto a ledger row."""
    updated = dict(entry)
    updated["bvid"] = identity.bvid
    updated["work_id"] = identity.work_id
    updated["page_index"] = identity.page_index
    updated["cid"] = identity.cid
    if identity.page_label:
        updated["page_label"] = identity.page_label
    elif "page_label" not in updated:
        updated["page_label"] = ""
    return updated


def identity_from_entry(
    entry: dict, key: str
) -> "PageIdentity | str":
    """Identity for a ledger row: PageIdentity when the row is
    page-resolved, otherwise the bare bvid fallback string."""
    work_id = entry.get("work_id")
    cid = entry.get("cid")
    bvid = str(entry.get("bvid") or key)
    if work_id and cid is not None:
        return page_identity(
            bvid,
            int(entry.get("page_index") or 0),
            int(cid),
            page_label=str(entry.get("page_label") or ""),
        )
    return bvid


def canonical_stem(row: Mapping[str, object]) -> str:
    """The one canonical artifact stem for a ledger row (compass D5).

    The stem derives solely from the row's ``(bvid, page_index)`` identity and
    is parseable back to it via ``parse_work_id`` (``{bvid}.p{page_index}``).
    ``page_label`` is a display concern and never enters the stem. Contract:

    - page-resolved row (``work_id`` present, not ``unresolved``): the stem is
      the parsed identity's stem. A malformed ``work_id`` raises — fail-loud
      rather than silently deriving a display string into a path. ``cid`` is
      not part of the stem: the identity D5 derives from is
      ``(bvid, page_index)``, both carried by ``work_id`` itself.
    - unresolved row: the bare ``bvid`` fallback (the archive's own
      ``archive_stem`` shape for a video-level row).
    - a row missing ``bvid`` raises (``KeyError``), matching ``archive_stem``:
      a stem without an identity is not guessable.

    This is the single definition shared by the archive, integrity, and
    workflow publication paths.
    Callers that probe real archive rows catch ``KeyError``/``ValueError``
    and report the row as identity-invalid rather than crashing the report.
    """
    bvid = str(row["bvid"])
    if row.get("unresolved") or not row.get("work_id"):
        return bvid
    work_id = str(row["work_id"])
    wb, page_index = parse_work_id(work_id)
    return artifact_stem(
        page_identity(
            wb,
            page_index,
            int(row.get("cid") or 0),
            page_label=str(row.get("page_label") or ""),
        )
    )


def display_label(row: Mapping[str, object]) -> str:
    """Display-only label for a ledger row (compass D5: display ≠ stem).

    What a report surface shows a human for a row whose canonical stem cannot
    be derived. ``page_label`` is honoured here, and the raw ``work_id`` may
    surface for an identity-invalid row — but this value is NEVER a path
    component. Filesystem names use ``canonical_stem`` / ``artifact_stem``
    only.
    """
    bvid = str(row.get("bvid") or "").strip()
    work_id = str(row.get("work_id") or "").strip()
    if work_id:
        return work_id
    if bvid:
        return bvid
    return str(row.get("page_label") or "")
