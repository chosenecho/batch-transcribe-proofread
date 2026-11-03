"""Shared scalar input rules; preserve caption text until the storage boundary."""

def integer(
    value: object,
    field: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field} must be at most {maximum}")
    return value


def text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not value.strip():
        raise ValueError(f"{field} must not be empty")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError(f"{field} contains invalid control characters")
    return value


def caption_text(value: object, field: str = "text") -> str:
    """Validate caption text: a string non-empty after stripping.

    Unlike :func:`text`, control characters inside the string are kept — the
    stored caption is verbatim apart from trimming.  Trimming is the storage
    boundary's job, not this validator's: ``TranscriptRepository`` stores and
    hashes the stripped form, so a caption's content identity never depends on
    the whitespace a caller happens to carry.
    """
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not value.strip():
        raise ValueError(f"{field} must not be empty")
    return value
