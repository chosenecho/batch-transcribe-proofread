"""Versioned source facts suitable for freezing in a new editorial edition."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import re
import unicodedata
from urllib.parse import urlsplit

from bili_asr.formatting import pubdate_iso
from bili_asr.platform_identity import ContentRef

_FIELDS = frozenset({"version", "platform", "externalVideoId", "partIndex", "title",
    "creatorId", "creatorName", "pubdateUnix", "sourcePublishedAt", "metadataObservedAt",
    "description", "coverUrl", "categoryId", "tags", "aid", "partTitle", "durationMs"})
_COVER_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+hdslb\.com\Z")
_YOUTUBE_COVER_HOSTS = frozenset({"i.ytimg.com", "i1.ytimg.com", "i2.ytimg.com", "i3.ytimg.com", "i4.ytimg.com"})
_BINDING_FIELDS = frozenset({"observationId", "payloadSha256", "envelopeSha256"})


def valid_source_text(value: object, *, limit: int, multiline: bool = False) -> bool:
    """Shared pure boundary rule for source facts and their frozen DTO."""
    return (isinstance(value, str) and len(value) <= limit
            and all(unicodedata.category(character) not in {"Cc", "Cs"}
                    or (multiline and character in "\r\n\t") for character in value))


def public_cover_url(value: str | None, *, platform: str = "bilibili", codec_version: int = 1) -> str | None:
    """Expose the registered public image hosts without capability parameters."""
    if (value is None or not valid_source_text(value, limit=2048)
            or "\\" in value or any(character.isspace() for character in value)):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        allowed = (_COVER_HOST.fullmatch(host) is not None if codec_version == 1 or platform == "bilibili"
                   else codec_version == 2 and platform == "youtube" and host in _YOUTUBE_COVER_HOSTS)
        if (parsed.scheme != "https" or not allowed
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.port not in {None, 443}):
            return None
    except ValueError:
        return None
    return value


@dataclass(frozen=True, slots=True)
class SourceMetadataSnapshot:
    ref: ContentRef
    title: str
    creator_id: str | None
    creator_name: str | None
    pubdate: int | None
    observed_at: int | None
    description: str | None = None
    cover_url: str | None = None
    category_id: int | None = None
    tags: tuple[str, ...] = ()
    aid: int | None = None
    part_title: str | None = None
    duration_ms: int | None = None
    codec_version: int = 1
    observation_bindings: tuple[tuple[str, str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.ref, ContentRef):
            raise ValueError("source metadata requires ContentRef")
        if type(self.codec_version) is not int or self.codec_version not in {1, 2}:
            raise ValueError("unknown source metadata codec version")
        if (not isinstance(self.observation_bindings, tuple) or len(self.observation_bindings) > 256
                or (self.codec_version == 1 and self.observation_bindings)
                or any(not isinstance(binding, tuple) or len(binding) != 3
                    or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", value) is None
                           for value, length in zip(binding, (32, 64, 64), strict=True))
                    for binding in self.observation_bindings)
                or len({binding[0] for binding in self.observation_bindings}) != len(self.observation_bindings)):
            raise ValueError("invalid frozen source observation bindings")
        if (not valid_source_text(self.ref.external_video_id, limit=128)
                or not valid_source_text(self.ref.platform, limit=32)):
            raise ValueError("invalid source metadata identity")
        for name in ("title", "creator_id", "creator_name", "description", "cover_url", "part_title"):
            value = getattr(self, name)
            limit = 65536 if name == "description" else 2048 if name == "cover_url" else 512
            if value is not None and not valid_source_text(value, limit=limit, multiline=name == "description"):
                raise ValueError(f"invalid source metadata {name}")
            if value is not None and name != "description" and not value.strip():
                raise ValueError(f"invalid empty source metadata {name}")
        object.__setattr__(self, "cover_url", public_cover_url(self.cover_url, platform=self.ref.platform,
                                                            codec_version=self.codec_version))
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("source title must not be empty")
        for name in ("pubdate", "observed_at", "category_id", "aid", "duration_ms"):
            value = getattr(self, name)
            minimum = 1 if name in {"category_id", "aid", "duration_ms"} else 0
            if value is not None and (type(value) is not int or not minimum <= value <= 2**63 - 1):
                raise ValueError(f"invalid source metadata {name}")
        if (not isinstance(self.tags, tuple) or len(self.tags) > 256
                or any(not valid_source_text(tag, limit=256) or not tag.strip() for tag in self.tags)):
            raise ValueError("invalid source metadata tags")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("source metadata tags must not repeat")

    def to_dict(self) -> dict[str, Any]:
        result = {
            "version": "source-metadata-v" + str(self.codec_version), "platform": self.ref.platform,
            "externalVideoId": self.ref.external_video_id, "partIndex": self.ref.part_index,
            "title": self.title, "creatorId": self.creator_id, "creatorName": self.creator_name,
            "pubdateUnix": self.pubdate, "sourcePublishedAt": pubdate_iso(self.pubdate),
            "metadataObservedAt": self.observed_at, "description": self.description,
            "coverUrl": self.cover_url, "categoryId": self.category_id,
            "tags": list(self.tags), "aid": self.aid, "partTitle": self.part_title,
            "durationMs": self.duration_ms,
        }
        if self.codec_version == 2:
            result["observationBindings"] = [dict(zip(("observationId", "payloadSha256", "envelopeSha256"),
                                                      binding, strict=True)) for binding in self.observation_bindings]
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SourceMetadataSnapshot:
        if not isinstance(value, dict) or value.get("version") not in {"source-metadata-v1", "source-metadata-v2"}:
            raise ValueError("unknown source metadata contract")
        version = 2 if value["version"] == "source-metadata-v2" else 1
        if set(value) != (_FIELDS | {"observationBindings"} if version == 2 else _FIELDS):
            raise ValueError("unknown source metadata fields")
        bindings = value.get("observationBindings", [])
        if not isinstance(bindings, list) or any(not isinstance(item, dict) or set(item) != _BINDING_FIELDS for item in bindings):
            raise ValueError("invalid frozen source observation bindings")
        if not isinstance(value["tags"], list):
            raise ValueError("source metadata tags must be an array")
        try:
            snapshot = cls(
                ContentRef(value["platform"], value["externalVideoId"], value["partIndex"]),
                value["title"], value["creatorId"], value["creatorName"], value["pubdateUnix"],
                value["metadataObservedAt"], value["description"], value["coverUrl"], value["categoryId"],
                tuple(value["tags"]), value["aid"], value["partTitle"], value["durationMs"],
                version, tuple((item["observationId"], item["payloadSha256"], item["envelopeSha256"]) for item in bindings),
            )
        except (TypeError, ValueError) as error:
            raise ValueError("invalid source metadata shape") from error
        if value != snapshot.to_dict():
            raise ValueError("source metadata fields or derived timestamp disagree")
        return snapshot


__all__ = ["SourceMetadataSnapshot", "public_cover_url", "valid_source_text"]
