"""Pure language and transcript preference policy shared by every layer.

Preference is source kind, language family, exact language code, then newest
version. Publication state is a separate fact and is never inferred here.

Local ASR leads by project policy; captions supply supporting decode evidence.
Publication falls back to captions when ASR admission is not ``usable``. This
order alone does not assess quality or change an existing publication.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

SOURCE_KIND_RANK = {"asr-local": 0, "subtitle-cc": 1, "subtitle-ai": 2}
LANGUAGE_FAMILY_ORDER = ("zh", "en")


def language_family(language: str, is_ai: bool) -> str:
    """Normalize a primary language subtag, including machine-caption prefixes."""

    code = language.strip().lower()
    if is_ai and code.startswith("ai-"):
        code = code[3:]
    return code.split("-", 1)[0]


def transcript_preference_key(row: Mapping[str, Any]) -> tuple[int, int, str, int]:
    """Return the current deterministic preference, smallest first.

    Unknown source kinds are rejected: the shipped storage contract permits
    exactly three. The unique stored kind/language/version identity makes this
    order total inside one part, without an incidental creation-time or id tie.
    """

    family = language_family(row["language"], row["source_kind"] == "subtitle-ai")
    try:
        family_rank = LANGUAGE_FAMILY_ORDER.index(family)
    except ValueError:
        family_rank = len(LANGUAGE_FAMILY_ORDER)
    return (SOURCE_KIND_RANK[row["source_kind"]], family_rank, row["language"], -row["version"])


def choose_transcript(rows: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Choose a preferred stored version, leaving publication identity separate."""

    return min(rows, key=transcript_preference_key, default=None)


__all__ = [
    "SOURCE_KIND_RANK", "LANGUAGE_FAMILY_ORDER", "language_family",
    "transcript_preference_key", "choose_transcript",
]
