"""Publication date rendering shared by stored and published records."""

import time
from datetime import datetime, timezone


def pubdate_utc(pubdate: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(pubdate))


def pubdate_iso(pubdate: int | None) -> str | None:
    """Render known source time in UTC, independently of the host timezone.

    Legacy zero values remain available as raw facts, but never become a
    fabricated 1970 publication time. Unknown and unrepresentable dates render
    as unknown; collection/release timestamps are never used as a fallback.
    """
    if pubdate is None:
        return None
    if type(pubdate) is not int:
        raise TypeError("pubdate must be an integer or None")
    if pubdate <= 0:
        return None
    try:
        return datetime.fromtimestamp(pubdate, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def duration_s_from_ms(duration_ms: int | None) -> int:
    """Convert stored milliseconds to whole seconds with a one-second floor."""
    if duration_ms is None:
        return 1
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
        raise TypeError("duration_ms must be an integer or None")
    return max(1, duration_ms // 1000)
