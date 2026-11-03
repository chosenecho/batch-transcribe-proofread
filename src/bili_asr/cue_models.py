"""Source-neutral cue values shared by parsing and quality assessment."""

from typing import NamedTuple


class Cue(NamedTuple):
    start: float
    end: float
    text: str
    confidence: float | None
