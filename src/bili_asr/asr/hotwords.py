"""Hotwords implementation."""

from __future__ import annotations

from typing import Iterable
import bili_asr.asr.constants as _dependency_constants


def _extra_hotwords(environment_value: str | None) -> tuple[str, ...]:
    """The operator's extra hotwords, in order, without duplicates."""

    if not environment_value:
        return ()
    terms: list[str] = []
    for raw in environment_value.replace("，", ",").split(","):
        term = raw.strip()
        if term and term not in terms and term not in _dependency_constants.DEFAULT_HOTWORDS:
            terms.append(term)
    return tuple(terms)


def evidence_guard_hotwords(
    candidates: Iterable[str],
    evidence_texts: Iterable[str | None],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Evidence-based hotword guard (2026-09-28 governance ruling, plan
    ``20260928-hotword-injection-governance``).

    A candidate term may reach the decoder prompt only if it occurs in at least
    one of ``evidence_texts`` — the run's own first-pass transcript or the
    paired AI-subtitle text.  Speculative seeding is the insertion-error source
    recorded as residual ``20260922-proofread-wave R1`` (hotword tokens observed
    in output where the audio says something else), so the default path seeds
    nothing: ``DEFAULT_HOTWORDS`` is empty while the per-token keep/drop
    measurement is pending, and operator-declared ``BILI_ASR_HOTWORDS`` terms
    pass through this same guard before they can bias a decode.

    The guard is pure string containment — no model calls, no tokenization.
    CJK terms are matched as-is (a hotword list entry is already the smallest
    meaningful unit, and the corpus text carries no word boundaries to consult);
    Latin-script terms are matched case-insensitively, because the prompt list
    capitalizes them while transcripts do not.  A term occurring only inside a
    longer word *is* matched — that is the deliberate semantics: a term that
    appears anywhere in the evidence is a term this run plausibly needs.

    Returns ``(admitted, dropped)`` in candidate order, de-duplicated.
    """


    def _norm(text: str) -> str:
        return text.casefold()

    seen: set[str] = set()
    ordered: list[str] = []
    for term in candidates:
        term = term.strip()
        if term and term not in seen:
            seen.add(term)
            ordered.append(term)
    folded = [_norm(text) for text in evidence_texts if text]
    admitted = tuple(
        term for term in ordered
        if any(_norm(term) in text for text in folded)
    )
    dropped = tuple(term for term in ordered if term not in admitted)
    return admitted, dropped


def filter_hotwords(
    candidates: Iterable[str],
    *,
    evidence_text: str | None,
    paired_subtitle_text: str | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The two-text call shape of :func:`evidence_guard_hotwords`.

    ``evidence_text`` is the run's own first-pass transcript; ``paired_subtitle_text``
    is the AI-subtitle text harvested for the same part (``None`` when the part has no
    subtitle route).  Both are evidence; a token passing either reaches the prompt.
    """


    return evidence_guard_hotwords(candidates, (evidence_text, paired_subtitle_text))
