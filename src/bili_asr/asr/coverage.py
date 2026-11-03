"""Coverage implementation."""

from __future__ import annotations

from typing import Any
import bili_asr.asr.constants as _dependency_constants


def _coverage_record(
    decoded_seconds: float, cues: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The coverage evidence for one run, or ``None`` when there is nothing to claim.

    ``decoded_seconds`` is ``sum(len(chunk) / SAMPLE_RATE)`` over **that run's** ``_split_audio``
    output — a measured quantity, not a header/container guess, and exactly what
    :class:`AudioDecodeError`'s documented gap says was missing.  The splitter's tiling promise
    makes that sum equal the decoded sample count, so the denominator needs no tolerance band and
    a shortfall stays attributable to the model rather than to the split.

    ``produced_s`` is ``max(cue.end) - min(cue.start)`` — a **span**, not a span-sum, so
    duplicate or overlapping cues cannot inflate coverage.  ``coverage`` is compared **unrounded**.

    ``None`` means "no coverage claim at all" (``decoded_s == 0``): a run that decoded nothing has
    nothing to attest, and inventing a ratio there would be a claim about audio nobody read.
    """

    if decoded_seconds <= 0:
        return None
    if cues:
        produced = max(float(cue["end"]) for cue in cues) - min(
            float(cue["start"]) for cue in cues
        )
    else:
        # A run that produced no cue at all covers 0 of what it decoded — the same rule that makes
        # an empty transcript with a non-zero decode a shortfall rather than a no-speech outcome.
        produced = 0.0
    # Alignment timestamps can overshoot the decoded window.  Keep the derived
    # evidence within the storage contract; published cue timestamps stay intact.
    produced = min(produced, decoded_seconds)
    coverage = produced / decoded_seconds
    return {
        "decoded_s": float(decoded_seconds),
        "produced_s": float(produced),
        "coverage": float(coverage),
        "coverage_min": _dependency_constants.COVERAGE_MIN,
        "coverage_short": bool(coverage < _dependency_constants.COVERAGE_MIN),
    }


def apply_coverage_evidence(entry: dict[str, Any], runner: Any) -> dict[str, Any] | None:
    """Write the last run's coverage evidence onto a manifest row, in place.

    The one writer helper both routes share (the in-process ``asr``/``pilot`` loops and
    :class:`RunCoordinator`): the evidence is a property of the **run**, so it must reach the row
    through every path that ran ASR, and a path that forgot it would leave a measured defect
    looking like an unqualified success.

    The previous run's keys are cleared first, so a re-run that measured nothing cannot leave an
    older run's ``coverage`` standing as if it described this one — an unqualified success cannot
    be laundered and cannot be inherited either (spec rule 6).

    The carrier is the manifest row itself (D11): ``decoded_s`` / ``produced_s`` / ``coverage`` /
    ``coverage_min`` beside the existing outcome vocabulary, plus the boolean marker
    ``coverage_short``.  No new ``outcome`` and no new ``error_code`` value — the existing CHECK
    constraint and the Python guards refuse those on every existing database, and the schema is
    ``CREATE TABLE IF NOT EXISTS`` with no in-place migration.

    Returns the record that was written, or ``None`` when the runner has no measurement to offer
    (it never transcribed, or it decoded nothing).
    """

    for key in _dependency_constants.COVERAGE_KEYS:
        entry.pop(key, None)
    reader = getattr(runner, "transcribed_coverage", None) if runner is not None else None
    record = reader() if callable(reader) else None
    if not record:
        return None
    entry.update(record)
    return record


def coverage_verdict(entry: Any) -> str:
    """One archived row's coverage verdict, read-only, from the archive alone.

    Three values, and the third is the point: a record with no ``coverage`` is
    :data:`COVERAGE_VERDICT_NOT_EVALUABLE` — *not* "covered" and *not* clean.  Reading an absent
    field as "no gap" would call every pre-existing row fine, which is the illusion this plan
    removes; the store persists no decoded duration for those rows, so their coverage is not
    reconstructible and the honest answer is the partition, not a guess.
    """

    if not isinstance(entry, dict):
        return _dependency_constants.COVERAGE_VERDICT_NOT_EVALUABLE
    coverage = entry.get("coverage")
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)):
        return _dependency_constants.COVERAGE_VERDICT_NOT_EVALUABLE
    return (
        _dependency_constants.COVERAGE_VERDICT_SHORT
        if float(coverage) < _dependency_constants.COVERAGE_MIN
        else _dependency_constants.COVERAGE_VERDICT_COVERED
    )


def characters_of(runner: Any) -> dict[str, Any] | None:
    """The character-level record ``runner`` produced for its last transcription, if any.

    Read through ``getattr`` because the record is **optional by design**: a runner double that
    never held aligner output answers with nothing, and so does an ASR boundary from before this
    record existed.  Nothing is fabricated for either — the ``characters`` field is only ever a
    captured fact.
    """

    reader = getattr(runner, "characters", None)
    return reader() if callable(reader) else None


def transcribed_coverage(runner: Any) -> dict[str, Any] | None:
    """The coverage evidence ``runner`` measured for its last transcription, if any.

    The module-level counterpart of :meth:`ASRRunner.transcribed_coverage`, and the same shape as
    :func:`characters_of` beside it: read through ``getattr`` so a runner double without the
    method, or an ASR boundary from before this measurement existed, answers with nothing rather
    than fabricating a clean bill.  ``None`` reaches the bundle as *no attestation*, which is the
    truthful record — the frontmatter simply carries no ``coverage_*`` key.

    This is the bundle-side half of the same measurement :func:`apply_coverage_evidence` writes to
    the manifest row; ``I-000188``'s acceptance requires the signal in both surfaces.
    """

    reader = getattr(runner, "transcribed_coverage", None)
    return reader() if callable(reader) else None
