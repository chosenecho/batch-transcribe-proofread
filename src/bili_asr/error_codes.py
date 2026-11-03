"""Pure scalar error-code contract shared by source and storage boundaries."""

from __future__ import annotations

import re

_ERROR_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")


def validate_error_code(value: object, field: str = "error_code") -> str | None:
    """Validate a bounded persisted code without importing a repository.

    Codes retain their original case and spelling.  ``None`` means no code;
    arbitrary exception messages, whitespace, paths and URLs are refused.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string or None")
    if not value or len(value) > 64 or _ERROR_CODE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded scalar code")
    return value


__all__ = ["validate_error_code"]
