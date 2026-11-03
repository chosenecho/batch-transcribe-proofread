"""Platform-independent source identity, separate from storage and file keys.

An external id is opaque and case-sensitive.  This type never invents a
provider id, generates a filename, or changes a frozen source object.  The
internal ``video_part_id`` remains the workflow/repository identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PLATFORM = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


@dataclass(frozen=True, slots=True)
class ContentRef:
    """One provider video part, using a zero-based part index.

    ``platform`` is an explicit lowercase adapter key; the external id is
    preserved verbatim rather than lowercased or stripped.  A reference may
    represent a platform the installed application does not support.  Adapter
    selection must reject that reference before making any provider request.
    """

    platform: str
    external_video_id: str
    part_index: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.platform, str):
            raise TypeError("platform must be a string")
        if _PLATFORM.fullmatch(self.platform) is None:
            raise ValueError("platform must be a lowercase adapter key")
        if not isinstance(self.external_video_id, str):
            raise TypeError("external_video_id must be a string")
        if not self.external_video_id.strip():
            raise ValueError("external_video_id must not be empty")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.external_video_id):
            raise ValueError("external_video_id contains control characters")
        try:
            self.external_video_id.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("external_video_id must be valid UTF-8 text") from exc
        if type(self.part_index) is not int:
            raise TypeError("part_index must be an integer")
        if self.part_index < 0:
            raise ValueError("part_index must be at least 0")


def require_platform(ref: ContentRef, platform: str) -> None:
    """Reject a mismatched adapter without interpreting the external id."""

    if not isinstance(ref, ContentRef):
        raise TypeError("source identity must be a ContentRef")
    if ref.platform != platform:
        raise ValueError(f"source-identity: adapter only supports {platform}")


__all__ = ["ContentRef", "require_platform"]
