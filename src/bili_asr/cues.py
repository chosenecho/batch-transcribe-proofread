"""The single home for cue parsing and segment rendering.

Three owners read or write the same two artefact shapes — SRT blocks and JSON
segment lists — and this module is the one place that knows those shapes:

- ``quality.py`` reads cues from SRT / TXT / JSON sidecars for the
  coverage/quality report (no source guard; its reason-code order is a pinned
  contract, so its parse results must not change).
- ``proofread.py`` reads the archived ASR route from the bundle raw sidecar,
  and must refuse any sidecar whose ``source`` is not ``asr`` (a
  caption-derived or merged sidecar would put one route on both sides of the
  adjudication table).
- ``asr.py`` renders segments out to SRT / TXT products.

Plan 009 consolidated the three independent implementations here; residual
C-R3 (deciding which other readers gain a source guard) is the follow-up that
this structure exists to make clean.

``Cue`` is defined in the dependency-free ``cue_models`` module and
re-exported by ``quality`` for existing callers.
"""

from __future__ import annotations

import json
import html
import math
import re
from pathlib import Path
from typing import Any, Mapping

from .cue_models import Cue

#: Cap on the cues one read keeps, unchanged from the pre-consolidation
#: ``quality._read_cues`` (its callers also account against it).
_MAX_CUES = 10_000

_SRT_TIME = re.compile(
    r"^(?P<h>\d{1,3}):(?P<m>[0-5]\d):(?P<s>[0-5]\d)[,.](?P<ms>\d{1,3})$"
)


class CueParseError(ValueError):
    """A JSON cue sidecar was readable but is not a usable cue document.

    ``kind`` is one of ``"malformed"`` (unparseable, or not a cue document at
    all) or ``"empty"`` (parsed, but holds no usable segment) — the two
    refusal branches the callers have always distinguished.  ``detail`` is
    the caller-rendered refusal text.
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def read_cues(
    path: Path, text: str, *, require_source: str | None = None
) -> "tuple[list[Cue], bool, bool]":
    """Parse one transcript artefact into cues.

    ``path`` selects the shape by suffix — ``.srt`` is SRT blocks, ``.json``
    is a cue sidecar (``{"body": [...]}``, ``{"segments": [...]}``, or a bare
    list of items), anything else falls through to "no comparable cues".

    When ``require_source`` is set, a JSON sidecar whose ``source`` field
    differs is refused with :class:`CueParseError` (``kind="malformed"``);
    when ``None``, any parseable cue document is accepted — the lenient arm
    the coverage/quality report has always used.  ``(cues, malformed, empty)``
    is returned exactly as the pre-consolidation ``quality._read_cues``
    returned it, so the report's frozen reason-code order cannot move.
    """

    if not text.strip():
        return [], False, True
    if path.suffix.lower() == ".srt":
        cues: list["Cue"] = []
        malformed = False
        blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
        for block in blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if not lines:
                continue
            timing = next((line for line in lines if "-->" in line), None)
            if timing is None:
                malformed = True
                continue
            parts = [part.strip() for part in timing.split("-->", 1)]
            if len(parts) != 2:
                malformed = True
                continue
            start, end = _parse_time(parts[0]), _parse_time(parts[1])
            if start is None or end is None:
                malformed = True
            else:
                cues.append(Cue(start, end, _cue_text(lines, timing), None))
        return cues[:_MAX_CUES], malformed, not cues
    if path.suffix.lower() == ".json":
        try:
            document = json.loads(text)
        except (ValueError, TypeError):
            return [], True, False
        if require_source is not None and not isinstance(document, dict):
            raise CueParseError("malformed", f"expected source={require_source} in {path}")
        if isinstance(document, dict):
            if require_source is not None and document.get("source") != require_source:
                raise CueParseError(
                    "malformed",
                    f"expected source={require_source}, found source="
                    f"{_render_source(document.get('source'))} in {path}",
                )
            if "body" in document and isinstance(document["body"], list):
                items = document["body"]
            elif "segments" in document and isinstance(document["segments"], list):
                items = document["segments"]
            else:
                return [], True, False
        elif isinstance(document, list):
            items = document
        else:
            return [], True, False
        cues = []
        malformed = False
        for item in items[:_MAX_CUES]:
            if not isinstance(item, dict):
                malformed = True
                continue
            start_raw = item.get("from") if "from" in item else item.get("start")
            end_raw = item.get("to") if "to" in item else item.get("end")
            if start_raw is None or end_raw is None:
                malformed = True
                continue
            try:
                start, end = float(start_raw), float(end_raw)
            except (TypeError, ValueError):
                malformed = True
                continue
            if not (math.isfinite(start) and math.isfinite(end)):
                malformed = True
                continue
            cues.append(Cue(start, end, _text_of(item), _confidence_of(item)))
        return cues, malformed, not cues
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return [], False, not lines


def read_route_ms(
    path: Path, *, require_source: str, label: str
) -> list[tuple[int, int, str]]:
    """Read ``path`` as the guarded ASR route: ``(start_ms, end_ms, text)``.

    The sidecar's ``source`` must equal ``require_source`` and its
    ``segments`` list must be non-empty; every segment needs finite-numeric
    ``start`` / ``end`` (seconds, converted to milliseconds exactly — the SRT
    renderer rounds ``seconds * 1000`` back, so no millisecond is lost) and a
    renderable ``text``.  ``label`` is the route prefix the refusal names
    (``"<bvid>:p<part>"`` for the proofread table); ``path`` is named as
    written so the operator sees which file was picked up.  Malformations are
    reported per segment, in order, as :class:`CueParseError` — the same
    failure modes (unreadable, wrong source, empty, malformed segment) the
    route reader has always distinguished.
    """

    if not path.is_file():
        raise CueParseError("malformed", f"{label}: missing ASR route (no {path})")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CueParseError("malformed", f"{label}: unreadable ASR route ({exc})") from exc
    if not isinstance(document, dict):
        raise CueParseError("malformed", f"{label}: ASR route is not an object in {path}")
    if require_source is not None and document.get("source") != require_source:
        raise CueParseError(
            "malformed",
            f"{label}: expected source={require_source}, found source="
            f"{_render_source(document.get('source'))} in {path}",
        )
    segments = document.get("segments")
    if not isinstance(segments, list) or not segments:
        raise CueParseError("malformed", f"{label}: ASR route holds no segment")
    triples: list[tuple[int, int, str]] = []
    for position, segment in enumerate(segments, start=1):
        try:
            if not isinstance(segment, dict):
                raise ValueError("segment is not an object")
            if isinstance(segment["start"], bool) or isinstance(segment["end"], bool):
                raise ValueError("timings must be numbers")
            start, end = float(segment["start"]), float(segment["end"])
            if not (math.isfinite(start) and math.isfinite(end)):
                raise ValueError("timings must be finite")
            if start < 0 or end < start:
                raise ValueError("timings must be non-negative and ordered")
            text = segment["text"]
            if not isinstance(text, str):
                raise ValueError("text must be a string")
            start_ms, end_ms = round(start * 1000), round(end * 1000)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise CueParseError(
                "malformed", f"{label}: ASR segment {position} is malformed ({exc})"
            ) from exc
        triples.append((start_ms, end_ms, text))
    return triples


def _render_source(source: Any) -> str:
    """Render a sidecar's ``source`` for a refusal: verbatim, bounded.

    A textual source is the value the operator will recognise; anything else
    is a shape the archive never writes, so it is shown as JSON rather than
    assumed to be printable.  Every branch is bounded, including the JSON
    one: an unbounded fallback would let a hostile or merely odd sidecar put
    an arbitrary string into the refusal line.
    """

    if source is None:
        return "<missing>"
    text = source if isinstance(source, str) else json.dumps(source, ensure_ascii=False)
    return text if len(text) <= 32 else text[:32] + "…"


def _cue_text(lines: list[str], timing: str) -> str:
    """The cue's text: every line after the timing line, joined.

    Taking the lines *after* the timing line — rather than dropping digit-only
    lines — keeps a cue whose text is itself a number.
    """

    index = lines.index(timing)
    return " ".join(lines[index + 1 :])


def _text_of(item: Mapping[str, object]) -> str:
    value = item.get("text")
    if value is None:
        value = item.get("content")
    return "" if value is None else str(value)


def _confidence_of(item: Mapping[str, object]) -> float | None:
    value = item.get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    return score if math.isfinite(score) else None


def _parse_time(value: str) -> float | None:
    match = _SRT_TIME.match(value)
    if not match:
        return None
    return (
        int(match["h"]) * 3600
        + int(match["m"]) * 60
        + int(match["s"])
        + int(match["ms"].ljust(3, "0")) / 1000
    )


def _fmt_srt_time(seconds: float) -> str:
    milliseconds = max(0, round(float(seconds) * 1000))
    return _fmt_time_ms(milliseconds, separator=",")


def _fmt_time_ms(milliseconds: int, *, separator: str) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def segments_to_srt(segments: list[dict[str, Any]]) -> str:
    blocks = []
    for index, segment in enumerate(segments, start=1):
        blocks.append(
            f"{index}\n{_fmt_srt_time(segment['start'])} --> {_fmt_srt_time(segment['end'])}\n"
            f"{segment['text']}\n"
        )
    return "\n".join(blocks)


def segments_to_vtt(segments: list[dict[str, Any]]) -> str:
    """Render stored cue order and plain text as a UTF-8 WebVTT document.

    Timings use the same rounded millisecond timeline as SRT. Overlapping cues
    are valid, but decreasing starts, nonpositive rounded durations and blank
    payload lines cannot be represented faithfully as WebVTT and are refused.
    No cue is sorted, dropped or silently rewritten. Ordinary CR/LF line endings
    are normalized to LF and plain-text markup characters are escaped.
    """
    blocks: list[str] = []
    previous_start = -1
    for index, segment in enumerate(segments, start=1):
        try:
            start, end = segment["start"], segment["end"]
            if any(isinstance(value, bool) or not isinstance(value, (int, float))
                   for value in (start, end)):
                raise ValueError("timings must be numbers")
            if not (math.isfinite(start) and math.isfinite(end)) or start < 0:
                raise ValueError("timings must be finite and non-negative")
            start_ms, end_ms = round(start * 1000), round(end * 1000)
            if end_ms <= start_ms:
                raise ValueError("duration must be positive on the millisecond timeline")
            if start_ms < previous_start:
                raise ValueError("cue starts must not decrease")
            text = segment["text"]
            if not isinstance(text, str):
                raise ValueError("text must be a string")
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if "\x00" in text or any(not line.strip() for line in text.split("\n")):
                raise ValueError("cue text must not contain NUL or blank lines")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"WebVTT cue {index} is unrepresentable: {exc}") from exc
        previous_start = start_ms
        blocks.append(
            f"{index}\n{_fmt_time_ms(start_ms, separator='.')} --> "
            f"{_fmt_time_ms(end_ms, separator='.')}\n"
            f"{html.escape(text, quote=False)}\n\n"
        )
    return "WEBVTT\n\n" + "".join(blocks)


def segments_to_txt(segments: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(segment.get("text", "")).strip()
        for segment in segments
        if str(segment.get("text", "")).strip()
    )


#: ``Cue`` resolves to ``quality.Cue`` under ``from __future__ import
#: annotations``; nothing imports this binding at runtime.
__all__ = [
    "CueParseError",
    "read_cues",
    "read_route_ms",
    "segments_to_srt",
    "segments_to_vtt",
    "segments_to_txt",
]
