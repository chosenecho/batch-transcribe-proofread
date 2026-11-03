"""Field evidence and review boundaries for registered content conversions.

These policies adapt historical facts without rewriting their source encoding.
Storage still verifies source identities and commits new editions atomically.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any

from bili_asr.contracts.identities import DESCRIPTION_SUPPLEMENT_POLICY, LEGACY_FACTS_POLICY, SOURCE_SUPPLEMENT_POLICY


@dataclass(frozen=True)
class ContentPolicy:
    identity: str
    source_content: tuple[str, ...]
    target_content: str
    body_rule: str
    review_status: str
    release_rule: str
    field_kinds: tuple[tuple[str, str], ...]
    consumer_profile: str
    converter: str
    sample: str

    def field_kind(self, name: str) -> str:
        try:
            return dict(self.field_kinds)[name]
        except (KeyError, TypeError) as exc:
            raise ValueError("unregistered content evidence field") from exc


_FROZEN = ("version", "platform", "externalVideoId", "partIndex", "title")
_UNKNOWN = ("creatorId", "creatorName", "pubdateUnix", "sourcePublishedAt", "metadataObservedAt",
            "description", "coverUrl", "categoryId", "tags", "aid", "partTitle", "durationMs")
_FIELDS = tuple(sorted((*((name, "legacy-input") for name in _FROZEN),
                        *((name, "unobserved") for name in _UNKNOWN))))
_POLICIES = (
    ContentPolicy(LEGACY_FACTS_POLICY, ("publication-content/v1", "frozen-input/v1"),
        "publication-content/v2", "selected-body-utf8-identical", "pending-review", "preserve-current-release",
        _FIELDS, "universal-origin-v1", "services.preserved_body_import",
        "tests/test_preserved_body_import.py"),
    ContentPolicy(SOURCE_SUPPLEMENT_POLICY, ("publication-content/v2", LEGACY_FACTS_POLICY),
        "publication-content/v2", "parent-body-utf8-identical", "pending-review", "preserve-current-release",
        tuple((name, "archive-part-projection" if name == "partTitle" else kind) for name, kind in _FIELDS),
        "universal-origin-v1", "services.source_supplement",
        "tests/test_source_supplement.py"),
    ContentPolicy(DESCRIPTION_SUPPLEMENT_POLICY, ("publication-content/v2", LEGACY_FACTS_POLICY),
        "publication-content/v2", "parent-body-utf8-identical", "pending-review", "preserve-current-release",
        tuple((name, "archive-video-details-projection" if name == "description" else kind) for name, kind in _FIELDS),
        "universal-origin-v2", "services.source_supplement", "tests/test_description_supplement.py"),
)
CONTENT_POLICIES = MappingProxyType({entry.identity: entry for entry in _POLICIES})


def content_policy(identity: str) -> ContentPolicy:
    try:
        return CONTENT_POLICIES[identity]
    except (KeyError, TypeError) as exc:
        raise ValueError("unsupported content conversion policy") from exc


def policy_catalog() -> list[dict[str, Any]]:
    return [asdict(policy) for policy in _POLICIES]


def missing_value(field: str) -> object:
    # SourceMetadataSnapshot defines an unobserved tag set as an empty array.
    content_policy(LEGACY_FACTS_POLICY).field_kind(field)
    return [] if field == "tags" else None
