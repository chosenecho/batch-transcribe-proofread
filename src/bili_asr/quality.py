"""Deterministic, read-only subtitle and transcript artifact quality signals."""

from __future__ import annotations

import collections
import difflib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, NamedTuple

from bili_asr.cue_models import Cue

from .archive import LOW_CONFIDENCE, bundle_paths_for_stem
from .artifact_root import ArtifactRoots
from bili_asr.asr.constants import _FORBIDDEN_PROVENANCE as _FORBIDDEN_MARKER
from .cues import CueParseError, read_cues as _read_shared_cues
from .page_identity import canonical_stem

#: Reasons that describe a structural defect: an artifact is missing, unreadable,
#: or internally inconsistent.  Only these make a work item invalid.
#:
#: The two ``identity_*`` codes appended here are the *declaration* half of the
#: identity family — the row's own identity/artifact metadata is unusable, which
#: is a different fact from any file's content:
#:
#: ``identity_unconfined``
#:     the row *declares* an artifact path and that declaration escapes every
#:     read base, so the path is unusable however the file system answers.
#: ``identity_invalid``
#:     a declared identity field is not schema-valid (a ``cid`` that is neither
#:     an int nor null), so the canonical artifact stem cannot be derived and
#:     every path inferred from it is a guess.
#:
#: Both must stay distinct from ``artifact_missing``, which means "the artifact
#: is not there yet": an in-flight row whose declaration is corrupt is not
#: backlog (contract §2b R3).  They are appended rather than absorbed into the
#: seven codes above because those positions are frozen output order (see
#: REASON_CODES), and because a reader must be able to tell *why* the row is
#: invalid — the remediation differs from a genuinely absent file.
DEFECT_REASON_CODES = (
    "empty",
    "malformed",
    "non_monotonic",
    "overlap",
    "out_of_range",
    "identity_mismatch",
    "artifact_missing",
    "identity_unconfined",
    "identity_invalid",
)
#: Reasons that record what the recorded content measures.  They are advisory:
#: they describe the transcript, never that the archive is broken, so they leave
#: the validity count and the exit status alone.
CONTENT_REASON_CODES = (
    "low_confidence",
    "leading_mark",
    "fragment_cue",
    "overlong_cue",
    "duplicate_cue",
    "repeated_ngram",
    "reference_disagreement",
)
#: The ordered vocabulary.  The seven defect codes keep their original positions,
#: so existing output keeps its spelling and its sort order.
REASON_CODES = DEFECT_REASON_CODES + CONTENT_REASON_CODES
#: A cue never opens on one of these marks, and a fragment is measured after
#: stripping them.  This is the retired report's own mark set, not the wider one
#: :mod:`bili_asr.asr` shapes cues with (``!``, ``?``, ``,``, ``;`` and ``:``
#: are absent here), so the two are deliberately not the same value.
LEADING = "，。！？、；："
#: Content thresholds, matching the cue shaping bounds in :mod:`bili_asr.asr`
#: (``_CUE_MAX_CHARS`` / ``_CUE_MIN_CHARS`` / ``_CUE_MIN_SECONDS``) so a cue the
#: shaper sized is reported only when it could not be merged into a neighbour.
OVERLONG_CHARS = 60
FRAGMENT_MAX_CHARS = 6
FRAGMENT_MAX_SECONDS = 1.0
#: Two transcripts of the same audio agreeing below this ratio is the
#: ``reference_disagreement`` signal — the systems contested the audio.
REFERENCE_AGREEMENT_FLOOR = 0.95
#: Marks stripped before two transcripts are compared, so punctuation and
#: spacing differences never read as disagreement.  Kept identical to the set
#: the retired standalone quality report used, so recorded agreement ratios
#: stay comparable across the merge.
_REFERENCE_PUNCT = "。，？！、；：,?!.;:…—·\"'“”‘’（）()《》"
_REFERENCE_STRIP = re.compile(f"[{re.escape(_REFERENCE_PUNCT)}]")
#: The published ``.md`` bundle opens with a YAML frontmatter block carrying the
#: title, the bilibili URL, and the work's identity.  That block is metadata,
#: never transcript text, so it is dropped before a plain-text artifact is
#: flattened for comparison.  A closing ``---`` line is required, so a lone
#: leading ``---`` in a transcript stays where it is.
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
#: Repeated-ngram detection: every ``_NGRAM_CHARS``-character window that
#: occurs ``_NGRAM_MIN_REPEATS`` times or more.  Eight characters is the retired
#: report's window, and both figures are pinned from both sides by
#: ``test_ngram_window_is_the_pinned_eight_characters``.
_NGRAM_CHARS = 8
_NGRAM_MIN_REPEATS = 3
#: Character ceiling on the repeated-ngram scan, in the joined transcript's own
#: characters. Sized well above a real transcript (the archive's longest archived
#: part joins to ~4 k characters) so the exact answer holds for every artifact
#: this corpus produces, while a pathological or adversarial body cannot turn
#: the *reporting* path into an unbounded allocation. See
#: :func:`_has_repeated_ngram` for what the bound gives up above it.
_NGRAM_MAX_CHARS = 200_000
_MAX_BYTES = 8 * 1024 * 1024
_MAX_CUES = 10_000
#: Maximum flattened characters per side for an exact full comparison.
#: Longer transcripts use bounded samples; see _compare_reference.
_MAX_COMPARE_CHARS = 20_000
#: Credential-like words as they can appear in a *file name*.  A marker's
#: trailing ``\b`` cannot end the match after an underscore (``_`` is a word
#: character), so ``token_abc123.srt`` escaped it while ``token-abc123.srt`` did
#: not.  A name may join its words with either separator, so this scan supplies
#: that boundary: the marker opens at the start of the name or after a
#: letter-free separator, and must not run into a following letter — every
#: separator form redacts, while an ordinary word such as ``tokenizer`` is left
#: alone.  ``asr._FORBIDDEN_PROVENANCE`` now carries the *same* separator-aware
#: boundary (plan QC F-001: the old one published ``myorg/token_abc`` verbatim),
#: so for credentials this scan is a redundant second opinion at the site that
#: reads names; it stays because it states the name-level rule where names are
#: read.
_NAME_CREDENTIAL = re.compile(
    r"(?:^|[^A-Za-z])(?:sessdata|cookie|token|password|secret|credential)(?![A-Za-z])",
    re.IGNORECASE,
)
class ReferenceUnavailable(ValueError):
    """A supplied reference transcript cannot be used for comparison.

    ``reason`` is a bounded, redaction-safe scalar so the caller reports a
    diagnostic instead of crashing on the reference.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ReferenceAgreement(NamedTuple):
    """One transcript compared against a second transcript of the same audio.

    ``reference`` is the reference's **basename only** — the operator's path,
    its parents, and any URL- or credential-shaped name never leave here.
    """

    reference: str
    agreement: float
    floor: float
    compared_chars: tuple[int, int]
    method: str = "full"
    total_chars: tuple[int, int] | None = None
    windows: tuple[tuple[int, int, int, int], ...] = ()


@dataclass(frozen=True)
class QualityResult:
    """Stable projection of quality observations for one manifest row.

    ``reasons`` holds defect codes only, so validity counts and exit status read
    from it unchanged.  ``content_reasons`` holds the advisory content codes and
    never affects either.

    ``low_confidence_at`` is the one field that carries a **value** rather than
    a code: the low-confidence cues' start seconds, ascending and rounded to 3
    decimals exactly as the archived frontmatter renders them
    (``asr_low_confidence_at``).  The retirement map promised this location list
    as ``low_confidence``'s replacement output, and a bare reason name cannot
    keep that promise — "a cue scored low" without *where* leaves the operator
    reading ``raw.json`` by hand (residual R1 of
    ``20260912-quality-signal-merge``).

    It is deliberately **not** in :meth:`to_dict`: ``to_dict``'s keys,
    the CSV column tuple and ``schema_version`` are frozen by
    ``specs/03-quality-surface.md``, so this value reaches the operator on the
    human path's stderr instead — the same channel the reference-agreement ratio
    already uses.  Consumed positions are keyword-only and appended last, so no
    existing construction or consumer shifts.
    """

    source: str | None
    language: str | None
    status: str | None
    cue_count: int
    artifact_count: int
    reasons: tuple[str, ...]
    diagnostics: tuple[str, ...]
    content_reasons: tuple[str, ...] = ()
    reference: ReferenceAgreement | None = None
    low_confidence_at: tuple[float, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "language": self.language,
            "status": self.status,
            "cue_count": self.cue_count,
            "artifact_count": self.artifact_count,
            "reasons": list(self.reasons),
            "diagnostics": list(self.diagnostics),
        }


class QualityAnalyzer:
    """Inspect local subtitle/archive artifacts without changing them."""

    def analyze(
        self,
        row: Mapping[str, object],
        archive_root: Path,
        reference: Path | None = None,
        *,
        artifact_roots: ArtifactRoots | None = None,
    ) -> QualityResult:
        """Measure one manifest row's artifacts.

        ``reference`` is an optional second transcript of the same audio.  It is
        compared against the row's own transcript: the best comparable text
        among the row's artifacts, which is the archived SRT when one exists and
        otherwise a cue sidecar or the plain transcript, with the published
        ``.md`` bundle as the last resort.  A reference that cannot be read
        raises :class:`ReferenceUnavailable`; it never degrades into a silent
        "no comparison".

        ``artifact_roots`` carries the bases the row's artifacts are probed under
        (contract §5/§10, D8); ``None`` is the identity case — the archive root
        alone, exactly as before.
        """

        roots = artifact_roots if artifact_roots is not None else ArtifactRoots.of(archive_root)
        # Enumeration repeats the same raw candidates and bases. Share their
        # resolves for this row only; the read guard below remains fresh.
        probe = _ContainmentProbe(roots)
        reasons: set[str] = set()
        diagnostics: set[str] = set()
        content_reasons: set[str] = set()
        low_confidence_at: tuple[float, ...] = ()
        # §2b R3: the row's *declared* identity, validated before any path is
        # derived from it.  `integrity.py` fails the same row on the same value
        # (`structural_input_error`, then `continue`), so a `cid` that is neither
        # an int nor null is corruption on this reader too — not backlog.
        cid = row.get("cid")
        identity_invalid = cid is not None and (
            isinstance(cid, bool) or not isinstance(cid, int)
        )
        artifacts: list[Path] = []
        if identity_invalid:
            # §2b R3: a `cid` that is neither an int nor null (bools included,
            # matching `integrity.py`'s own check) makes the canonical artifact
            # stem underivable, so every path *inferred* from it would be a
            # guess.  A reason about a guessed filename would describe the guess
            # rather than the archive, so the walk stops here — `verify` stops at
            # the same row for the same reason (`structural_input_error`).
            reasons.add("identity_invalid")
        else:
            # Both writer-real raw locations must remain confined even when
            # the row explicitly names a complete bundle. These are identity
            # probes, not extra transcript representations to read or count.
            if _inferred_raw_escapes(row, roots, probe):
                reasons.add("identity_unconfined")
            artifacts = _artifact_paths(row, roots, probe)
            if not artifacts:
                # No declared/derivable candidate and an absent named artifact
                # share the frozen backlog reason (#101); absence provenance is
                # not a new diagnostic or a declaration defect.
                reasons.add("artifact_missing")
        cue_count = 0
        valid_artifacts = 0
        transcript: str | None = None
        # The best comparison source seen so far, as a rank: a cue-bearing
        # artifact (3) outranks a plain transcript (2), and both outrank the
        # published ``.md`` bundle (1), whose comparable text is its body rather
        # than the artifact it publishes.  Only a strictly better artifact
        # replaces the current source, so equal ranks keep the archived SRT.
        transcript_rank = 0
        for path in artifacts:
            if not _contained_at_any_base(path, roots):
                # §2b R3: the row's declaration escapes every read base, so the
                # path is unusable however the file system answers.  This is *not*
                # "not there yet" — collapsing it into `artifact_missing` (the one
                # backlog reason) is the regression R3 names, and the reason must
                # stay distinct rather than the backlog set widening to absorb it.
                reasons.add("identity_unconfined")
                continue
            if not path.is_file():
                # A confined candidate exists as a declaration, but its bytes
                # are absent. Keep the same aggregate backlog code as above.
                reasons.add("artifact_missing")
                continue
            try:
                if path.stat().st_size > _MAX_BYTES:
                    reasons.add("malformed")
                    diagnostics.add("file_too_large")
                    continue
                text = path.read_text(encoding="utf-8")
                cues, malformed, empty = _read_shared_cues(
                    path, text, require_source="asr" if row.get("source") == "asr" else None
                )
            except CueParseError:
                reasons.add("malformed")
                diagnostics.add("source_mismatch")
                continue
            except (OSError, UnicodeError):
                reasons.add("malformed")
                diagnostics.add("unreadable")
                continue
            valid_artifacts += 1
            # SRT and raw are representations of the same transcript. Count
            # cues from the preferred comparable artifact once, not per file.
            rank = _transcript_rank(path, cues)
            if rank > transcript_rank:
                candidate = comparable_text(text, cues)
                if candidate:
                    transcript, transcript_rank = candidate, rank
                    cue_count = len(cues)
            if malformed:
                reasons.add("malformed")
            if empty:
                reasons.add("empty")
            _check_cues(cues, row, reasons)
            # Per-cue scores live in the raw sidecar only, so the locations must
            # survive the artifacts that carry none: this runs once per artifact,
            # and an SRT or `.md` following the sidecar would otherwise clear what
            # the sidecar supplied.  The first non-empty answer wins — the row has
            # exactly one artifact that records scores.
            if not low_confidence_at:
                low_confidence_at = _check_content(cues, content_reasons)
            else:
                _check_content(cues, content_reasons)
            _check_identity(path, text, row, reasons)
        agreement = (
            None
            if reference is None
            else _compare_reference(reference, transcript or "")
        )
        if agreement is not None and agreement.agreement < agreement.floor:
            content_reasons.add("reference_disagreement")
        return QualityResult(
            source=_text_value(row, "source", "subtitle_source"),
            language=_text_value(row, "language", "sub_lan", "subtitle_language"),
            status=_text_value(row, "status"),
            cue_count=cue_count,
            artifact_count=valid_artifacts,
            reasons=tuple(sorted(reasons, key=REASON_CODES.index)),
            diagnostics=tuple(sorted(diagnostics)[:8]),
            content_reasons=tuple(sorted(content_reasons, key=REASON_CODES.index)),
            reference=agreement,
            low_confidence_at=low_confidence_at,
        )


def _text_value(row: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _canonical_stem(row: Mapping[str, object]) -> str | None:
    """The row's canonical stem (compass D5) — delegates to
    ``page_identity.canonical_stem``.

    A row whose identity cannot derive a canonical stem (missing ``bvid``,
    malformed ``work_id``) is identity-invalid: the stem is unguessable, so
    no artifact path may be inferred from one. Returns ``None``; the caller
    then surfaces the row's own ``identity_invalid``/declaration reasons
    rather than guessing paths. ``page_label`` never enters the stem — the
    display helper owns that surface.
    """
    try:
        return canonical_stem(row)
    except (KeyError, TypeError, ValueError):
        return None


def _artifact_paths(
    row: Mapping[str, object], roots: ArtifactRoots, probe: _ContainmentProbe,
) -> list[Path]:
    """The row's artifact candidates, each resolved at the base that holds it.

    A declared value is a root-relative string (D7), so it is probed over
    ``roots.read_bases()`` in order and the first base holding the file wins — which
    is what keeps a legacy row whose only copy sits at the archive root readable
    (D6).  The inferred candidates and the ``transcripts/md`` glob walk the same base
    list, and a candidate that exists at neither base is reported at the first one so
    the caller reads it as missing rather than as absent from the report.

    Every candidate is returned unresolved-at-base, so the caller owns the two
    checks that differ by provenance: containment (a declared value *or* an
    inferred name can escape through a symlink, and both are `identity_unconfined`)
    and existence (`artifact_missing`, which alone is backlog).
    """
    bases = roots.read_bases()
    values: list[object] = []
    for key in (
        "subtitle_path",
        "srt_path",
        "txt_path",
        "md_path",
        "raw_path",
        "artifact_path",
    ):
        if row.get(key) is not None:
            values.append(row[key])
    paths = row.get("artifact_paths")
    if isinstance(paths, (list, tuple)):
        values.extend(paths)
    inferred = not values
    if inferred:
        stem = _canonical_stem(row)
        if stem:
            values.extend(
                (
                    bundle_paths_for_stem("", stem)["srt_path"],
                    bundle_paths_for_stem("", stem)["txt_path"],
                    bundle_paths_for_stem("", stem)["raw_path"],
                    Path("subtitles/raw") / f"{stem}.json",
                )
            )
            # The derived ``.md`` bundle is appended last: its body is the
            # transcript, but a transcript artifact outranks it as the
            # comparison source.  Every base's derived directory is offered, so a
            # bundle that sits at either base is found.
            for base in bases:
                values.extend(_derived_md_candidates(row, stem, base))
    result: list[Path] = []
    for value in values:
        if isinstance(value, (str, Path)) and value:
            path = Path(value)
            candidates = (path,) if path.is_absolute() else tuple(base / path for base in bases)
            chosen = next((item for item in candidates if item.exists()), None)
            if chosen is None:
                # §2f: an inferred candidate must still be offered even when nothing
                # exists — existence is the wrong gate for "where does this path point",
                # and a directory component can be the escaping symlink.  The caller's
                # containment check runs first and reports `identity_unconfined`; a
                # merely absent-and-confined candidate still reads as `artifact_missing`.
                #
                # Corrected against measurement: offering *every* absent candidate made
                # each absent inferred sibling its own `artifact_missing`, but the inferred
                # families are alternatives rather than artifacts that must all exist — six
                # existing tests regressed and a healthy archive flipped to exit 1.  The
                # offer therefore stays gated on existence, except for a candidate that
                # escapes every read base: there, absence is the question being asked.
                chosen = next(
                    (item for item in candidates if _escapes_every_base(item, probe)),
                    None,
                )
                if chosen is None:
                    if inferred:
                        continue
                    chosen = candidates[0]
            result.append(chosen)
    if inferred and not result and values:
        result.append(bases[0] / values[0])
    return list(dict.fromkeys(result))


def _inferred_raw_escapes(
    row: Mapping[str, object], roots: ArtifactRoots, probe: _ContainmentProbe,
) -> bool:
    """Ask verify's two raw containment questions independently of declarations.

    Each family is legal when some read base confines its own candidate. An
    absent but confined caption sidecar contributes no missing-artifact reason;
    it is an alternative route, not part of the declared archive bundle.
    """
    stem = _canonical_stem(row)
    if stem is None:
        return False
    bases = roots.read_bases()
    families = (
        tuple(base / "subtitles" / "raw" / f"{stem}.json" for base in bases),
        tuple(bundle_paths_for_stem(base, stem)["raw_path"] for base in bases),
    )
    for candidates in families:
        try:
            if not any(probe.contained(path, base) for base, path in zip(bases, candidates)):
                return True
        except (OSError, RuntimeError):
            # Match the existing inferred-path probe: an unresolved symlink
            # loop or inaccessible parent does not prove an outside location.
            continue
    return False


def _derived_md_candidates(row: Mapping[str, object], stem: str, base: Path) -> list[Path]:
    """The derived ``.md`` bundle path under one base (shape A: one exact name).

    There is no glob any more.  It existed to find a markdown whose name embedded
    the pubdate and the title -- the one artifact of the four whose name moved
    when either did.  Under shape A every artifact is a fixed name inside the
    work's own directory, so there is exactly one place the markdown can be and
    one name it can have; a miss means the bundle is incomplete, not that a
    differently-named copy might be lying beside it.
    """
    exact_md = bundle_paths_for_stem(base, stem)["md_path"]
    return [exact_md] if exact_md.is_file() else []
class _ContainmentProbe:
    """Memoize enumeration resolves within one row, never across reads or rows."""

    def __init__(self, roots: ArtifactRoots) -> None:
        self.bases = roots.read_bases()
        self._resolved: dict[Path, Path] = {}

    def resolve(self, path: Path) -> Path:
        if path not in self._resolved:
            self._resolved[path] = path.resolve()
        return self._resolved[path]

    def contained(self, path: Path, base: Path) -> bool:
        try:
            self.resolve(path).relative_to(self.resolve(base))
        except ValueError:
            return False
        return True

    def contained_at_any_base(self, path: Path) -> bool:
        return any(self.contained(path, base) for base in self.bases)


def _contained_at_any_base(path: Path, roots: ArtifactRoots) -> bool:
    """`_contained`, asked once per base.

    Contract §5/§6 fix the shape as "shared base list, per-family guard": the
    transcript family keeps its own ``resolve``-based check and only the base list
    is shared with the audio family's descriptor-anchored guard.
    """
    return _ContainmentProbe(roots).contained_at_any_base(path)


def _escapes_every_base(path: Path, probe: _ContainmentProbe) -> bool:
    """True only when the path is *known* to resolve outside every read base.

    Measured: ``resolve()`` raises ``RuntimeError`` on a symlink loop (and ``OSError``
    on some kernels) while ``exists()`` merely answers False.  B-R10 already carries
    that crash on the declared-path surface, so an undeterminable answer here reports
    False and the candidate is dropped exactly as it was before §2f — an inferred
    candidate must not widen the residual's reach.
    """
    try:
        return not probe.contained_at_any_base(path)
    except (OSError, RuntimeError):
        return False


def flatten_reference(text: str) -> str:
    """Strip punctuation and spacing so punctuation never reads as disagreement."""

    return _REFERENCE_STRIP.sub("", text).replace(" ", "").lower()


def _transcript_rank(path: Path, cues: list[Cue]) -> int:
    """How well one artifact serves as the row's transcript for comparison.

    Cue-bearing artifacts rank highest because their cue texts are the
    transcript itself; a plain transcript ranks next; the published ``.md``
    bundle ranks last, since its comparable text is its body rather than the
    artifact it publishes.  An artifact whose text is carried by cues — an SRT
    or a cue sidecar — that yielded none holds no transcript at all: its own
    source is timing lines and JSON structure, so it ranks zero and can never
    displace a real transcript.
    """

    if cues:
        return 3
    if path.suffix.lower() in {".srt", ".json"}:
        return 0
    if path.suffix.lower() == ".md":
        return 1
    return 2


def comparable_text(text: str, cues: list[Cue]) -> str:
    """One transcript's text, flattened for cross-transcript comparison.

    A cue-structured artifact contributes its cue texts through the same cue
    parser as everything else; a plain-text artifact (``.txt``/``.md``) has no
    cue structure, so its non-blank lines are its text — the reading the
    retired standalone quality report used for the same inputs.  A leading YAML
    frontmatter block is metadata rather than text and is dropped first, so a
    published ``.md`` bundle compares by its body rather than by its title and
    URL.
    """

    if cues:
        return flatten_reference("".join(cue.text for cue in cues))
    body = _FRONTMATTER.sub("", text, count=1)
    return flatten_reference(
        "".join(line.strip() for line in body.splitlines() if line.strip())
    )


def reference_basename(path: Path) -> str:
    """The reference's basename, or ``[redacted]`` when even that leaks.

    The operator's directory layout stays out of the report; a name that is
    itself URL- or credential-shaped is replaced too, so no path, URL, or
    credential-like value can reach the output.  The provenance marker alone is
    not enough for a *name*: its trailing ``\\b`` never fires after an
    underscore, which is how ``token_abc123.srt`` was published while
    ``token-abc123.srt`` was redacted, so a name-level scan treats both
    separators alike.
    """

    name = path.name
    if not name or _FORBIDDEN_MARKER.search(name) or _NAME_CREDENTIAL.search(name):
        return "[redacted]"
    return name


def _compare_reference(
    reference: Path, transcript: str
) -> ReferenceAgreement | None:
    """Compare the row's transcript against a second transcript of the same audio.

    Both sides are flattened before a bounded ratio is computed, and the
    reference's own text is never echoed — only its basename and the two
    character counts leave this function.  Returns ``None`` when the row has no
    comparable text: that is the row's own defect (``empty``/``artifact_missing``
    already reports it), not a fault of the supplied reference.

    Short pairs retain the full comparison. Longer pairs sample at most ten
    evenly spaced 2000-character windows per side, recording their offsets and
    the full lengths. The mean window agreement is weighted by the ratio of
    the two full lengths, so a tiny matching prefix cannot attest a long row.
    This is a sampled comparison; it cannot detect changes between windows.
    """

    try:
        if reference.stat().st_size > _MAX_BYTES:
            raise ReferenceUnavailable("reference too large")
        text = reference.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReferenceUnavailable("reference unreadable") from exc
    other, malformed, empty = _read_cues(reference, text)
    if reference.suffix.lower() == ".json" and (malformed or empty):
        # A JSON reference is a cue sidecar: one that does not parse, or that
        # carries no segment, has no transcript to compare.  Comparing its own
        # source text would invent a near-zero ratio — the silent wrong answer
        # this exception exists to prevent — so it is refused like any other
        # unreadable reference.  A blank ``.txt``/``.srt`` is not a sidecar and
        # keeps its own, more precise diagnostic below.
        raise ReferenceUnavailable("reference unreadable")
    theirs = comparable_text(text, other)
    if not theirs:
        raise ReferenceUnavailable("reference has no comparable text")
    if not transcript:
        return None
    lengths = (len(transcript), len(theirs))
    if max(lengths) > _MAX_COMPARE_CHARS:
        width = min(2000, min(lengths))
        count = min(10, max(1, min(lengths) // width))
        windows = []
        scores = []
        for index in range(count):
            position = index / (count - 1) if count > 1 else 0.5
            left = round((len(transcript) - width) * position)
            right = round((len(theirs) - width) * position)
            windows.append((left, left + width, right, right + width))
            scores.append(difflib.SequenceMatcher(
                None, transcript[left:left + width], theirs[right:right + width],
                autojunk=False,
            ).ratio())
        ratio = sum(scores) / count * (2 * min(lengths) / sum(lengths))
        return ReferenceAgreement(
            reference_basename(reference), ratio, REFERENCE_AGREEMENT_FLOOR,
            (count * width, count * width), "windowed", lengths, tuple(windows),
        )
    ratio = difflib.SequenceMatcher(
        None, transcript, theirs, autojunk=False
    ).ratio()
    return ReferenceAgreement(
        reference=reference_basename(reference),
        agreement=ratio,
        floor=REFERENCE_AGREEMENT_FLOOR,
        compared_chars=(len(transcript), len(theirs)),
    )


def _read_cues(path: Path, text: str) -> tuple[list[Cue], bool, bool]:
    """The single cue parser, shared via :mod:`bili_asr.cues` (plan 009).

    This module's reason-code order is a pinned contract, so the parse
    result must not change: the shared reader is called leniently (no source
    guard), exactly as this body behaved before the consolidation.
    """

    return _read_shared_cues(path, text)


def _check_cues(
    cues: list[Cue], row: Mapping[str, object], reasons: set[str]
) -> None:
    duration = row.get("duration_s")
    maximum: float | None = None
    if isinstance(duration, (int, float)):
        try:
            d = float(duration)
            if d >= 0 and math.isfinite(d):
                maximum = d
        except (TypeError, ValueError):
            pass
    previous_start = -1.0
    previous_end = -1.0
    for cue in cues:
        start, end = cue.start, cue.end
        if not (math.isfinite(start) and math.isfinite(end)):
            reasons.add("malformed")
            reasons.add("out_of_range")
            continue
        if start < 0 or end < 0 or end <= start:
            reasons.add("out_of_range")
        if start < previous_start:
            reasons.add("non_monotonic")
        if start < previous_end:
            reasons.add("overlap")
        if maximum is not None and end > maximum + 0.001:
            reasons.add("out_of_range")
        previous_start, previous_end = start, end


def _check_content(
    cues: list[Cue], reasons: set[str]
) -> tuple[float, ...]:
    """Record what the transcript's content measures.  Advisory, never a defect.

    Returns the low-confidence cues' start seconds — the same filtered list the
    ``low_confidence`` code is derived from, so the count and the locations can
    never disagree, and an artifact that records no score yields ``()`` rather
    than a fabricated position.
    """

    low = [
        cue
        for cue in cues
        if cue.confidence is not None and cue.confidence <= LOW_CONFIDENCE
    ]
    if low:
        reasons.add("low_confidence")
    if any(cue.text and cue.text[:1] in LEADING for cue in cues):
        reasons.add("leading_mark")
    if any(
        len(cue.text.strip(LEADING)) < FRAGMENT_MAX_CHARS
        and (cue.end - cue.start) < FRAGMENT_MAX_SECONDS
        for cue in cues
    ):
        reasons.add("fragment_cue")
    if any(len(cue.text) > OVERLONG_CHARS for cue in cues):
        reasons.add("overlong_cue")
    texts = [cue.text for cue in cues]
    if any(bool(text) and text == other for text, other in zip(texts, texts[1:])):
        reasons.add("duplicate_cue")
    if _has_repeated_ngram("".join(texts)):
        reasons.add("repeated_ngram")
    # Ascending and 3-decimal, matching the archived `asr_low_confidence_at`.
    return tuple(round(cue.start, 3) for cue in low)


def _has_repeated_ngram(joined: str) -> bool:
    """Whether any :data:`_NGRAM_CHARS`-window of ``joined`` repeats.

    The scan is a counter over sliding windows, so its work is linear in the
    number of windows — and a long transcript is a long string: the artifact cap
    is ``_MAX_BYTES``, which at one CJK character per 3 bytes is millions of
    windows, all of them stored in one dictionary (residual R2 of
    ``20260912-quality-signal-merge``, inherited from the retired script).

    :data:`_NGRAM_MAX_CHARS` bounds the windows actually counted. At or below it
    the answer is **exact**. Above it the scan covers the first
    ``_NGRAM_MAX_CHARS`` characters only, which can miss a repeat that begins
    past the bound — accepted deliberately: this is an advisory content code
    (it never affects validity or the exit status), a transcript long enough to
    hit the bound has already passed every defect check, and an unbounded scan
    on the *reporting* path is the worse failure. The bound is therefore part of
    the code's contract, not an implementation detail.
    """

    limit = min(len(joined), _NGRAM_MAX_CHARS)
    grams = collections.Counter(
        joined[index : index + _NGRAM_CHARS]
        for index in range(max(0, limit - _NGRAM_CHARS))
    )
    return any(count >= _NGRAM_MIN_REPEATS for count in grams.values())


def _check_identity(
    path: Path, text: str, row: Mapping[str, object], reasons: set[str]
) -> None:
    stem = _canonical_stem(row)
    expected_work_id = _text_value(row, "work_id")
    expected_bvid = _text_value(row, "bvid")

    if stem and path.name:
        if path.suffix.lower() in {".srt", ".json", ".txt", ".md"}:
            # Fixed bundle names carry identity in their immediate parent.
            # A matching ancestor or a page-number prefix cannot identify the
            # bundle. Legacy names carry a bounded stem in the filename.
            if path.name in {
                "bundle.srt", "bundle.txt", "bundle.md", "bundle.raw.json",
            }:
                identity_matches = path.parent.name in {stem, f"{stem}.proofread"}
            else:
                identity_matches = re.search(
                    rf"(?<![A-Za-z0-9]){re.escape(stem)}(?![A-Za-z0-9])",
                    path.name,
                ) is not None
            if not identity_matches:
                reasons.add("identity_mismatch")

    if path.suffix.lower() == ".md":
        work_id_match = re.search(
            r"^work_id:\s*[\"']?([^\"'\n\r]+)[\"']?", text, re.MULTILINE
        )
        if work_id_match:
            md_work_id = work_id_match.group(1).strip()
            if expected_work_id and md_work_id != expected_work_id:
                reasons.add("identity_mismatch")

        bvid_match = re.search(
            r"^bvid:\s*[\"']?([^\"'\n\r]+)[\"']?", text, re.MULTILINE
        )
        if bvid_match:
            md_bvid = bvid_match.group(1).strip()
            if expected_bvid and md_bvid != expected_bvid:
                reasons.add("identity_mismatch")
