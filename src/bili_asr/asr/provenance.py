"""Pure provenance rules; runner construction belongs to the public facade."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import bili_asr.asr.constants as _dependency_constants


def provenance_language(provenance: Any) -> str:
    """Return the persisted language with one shared implicit fallback."""
    if isinstance(provenance, Mapping):
        value = provenance.get("language")
        if value:
            return str(value)
    return _dependency_constants.DEFAULT_TRANSCRIPT_LANGUAGE


def apply_provenance_evidence(entry: dict[str, Any], runner: Any) -> None:
    """Carry the ASR branch's provenance on its resumable manifest row."""
    try:
        provenance = runner.provenance() or {}
    # Provenance is optional evidence from an injected runner.  Its failure
    # must retain the established implicit-language record, not abort a run.
    except Exception:  # noqa: BLE001
        provenance = {}
    entry["source"] = "asr"
    entry["language"] = provenance_language(provenance)


def _redact(value: str) -> str:
    """``[redacted]`` for a value that would publish a path, URL or credential."""

    return "[redacted]" if value and _dependency_constants._FORBIDDEN_PROVENANCE.search(value) else value
