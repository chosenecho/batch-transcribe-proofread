"""Deterministic acquisition candidate ranking; no storage or network policy."""
from __future__ import annotations

from bili_asr.sources.models import SubtitleTrack
from bili_asr.transcript_selection import LANGUAGE_FAMILY_ORDER, language_family


def rank_candidates(
    tracks: tuple[SubtitleTrack, ...], languages: tuple[str, ...] = ()
) -> tuple[SubtitleTrack, ...]:
    """Rank all eligible tracks, preserving exact requested-language boundaries.

    Default order is language family, CC before AI, then upstream order.
    Explicit preferences rank each requested code before later preferences;
    unrelated languages remain ineligible even when a preferred body is empty.
    """
    preference = {language: index for index, language in reversed(tuple(enumerate(languages)))}

    def key(pair: tuple[int, SubtitleTrack]) -> tuple[int, bool, int]:
        index, track = pair
        if languages:
            rank = preference[track.language]
        else:
            family = language_family(track.language, track.is_ai)
            rank = (LANGUAGE_FAMILY_ORDER.index(family) if family in LANGUAGE_FAMILY_ORDER
                    else len(LANGUAGE_FAMILY_ORDER))
        return rank, track.is_ai, index

    eligible = ((index, track) for index, track in enumerate(tracks)
                if not languages or track.language in preference)
    return tuple(track for _, track in sorted(eligible, key=key))


__all__ = ["rank_candidates"]
