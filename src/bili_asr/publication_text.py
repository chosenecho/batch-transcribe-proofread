"""Shared text and field rules for frozen publication content."""
from __future__ import annotations

from typing import Any

CONTENT_KEYS = frozenset({"title", "markdown", "summary", "tags", "source", "attribution", "editorNote"})
EDITABLE_METADATA = CONTENT_KEYS - {"markdown", "source"}


def normalize_text(value: Any, name: str, *, nonempty: bool = False, multiline: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"publication-content: {name} must be text")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"publication-content: {name} must be valid UTF-8 text") from exc
    if "\x00" in value or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError(f"publication-content: {name} contains control characters")
    if not multiline and ("\n" in value or "\t" in value):
        raise ValueError(f"publication-content: {name} must be one line")
    value = value.strip()
    if nonempty and not value:
        raise ValueError(f"publication-content: {name} must not be empty")
    return value


def normalize_actor(actor: str) -> str:
    return normalize_text(actor, "actor", nonempty=True, multiline=False)
