"""Public, field-limited evidence rules shared by storage and export readers."""
from __future__ import annotations

from bili_asr.canonical_json import digest
from bili_asr.source_metadata import valid_source_text

from bili_asr.contracts.registry import DESCRIPTION_SUPPLEMENT_POLICY as DESCRIPTION_POLICY, SOURCE_SUPPLEMENT_POLICY as POLICY
KIND = "archive-part-projection"
DESCRIPTION_KIND = "archive-video-details-projection"
FIELDS = {"version", "platform", "externalVideoId", "partIndex", "videoPartId",
          "cid", "field", "value", "sourceKind", "observedAt"}


def validate_supplement(value: object, metadata: dict, part_id: int) -> dict:
    """Verify identity and value binding, not an upstream digital signature.

    The trusted recorder reads the archive itself; caller-authored evidence is
    never accepted by apply. Part-title acquisition time is unknown; description
    time is retained only when matching per-field collection evidence exists.
    Registration time is a separate fact.
    """
    if not isinstance(value, dict) or set(value) != {"supplementId", "evidence"}:
        raise ValueError("source-supplement: invalid public evidence envelope")
    evidence = value["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != FIELDS:
        raise ValueError("source-supplement: unsupported or altered evidence")
    description = evidence["field"] == "description"
    observed = evidence["observedAt"]
    if (type(evidence["version"]) is not int or evidence["version"] != (2 if description else 1)
            or evidence["sourceKind"] != (DESCRIPTION_KIND if description else KIND)
            or (observed is not None and (not description or type(observed) is not int or not 0 <= observed <= 2**53 - 1))
            or evidence["field"] not in {"partTitle", "description"} or evidence["platform"] != "bilibili"
            or value["supplementId"] != digest(evidence)):
        raise ValueError("source-supplement: unsupported or altered evidence")
    for field, minimum in (("partIndex", 0), ("videoPartId", 1), ("cid", 1)):
        if type(evidence[field]) is not int or not minimum <= evidence[field] <= 2**53 - 1:
            raise ValueError("source-supplement: invalid part identity")
    if (any(evidence[field] != metadata[field] for field in ("platform", "externalVideoId", "partIndex"))
            or evidence["videoPartId"] != part_id
            or not valid_source_text(evidence["value"], limit=65536 if description else 512, multiline=description)
            or (not description and not evidence["value"].strip())
            or evidence["value"] != metadata[evidence["field"]]):
        raise ValueError("source-supplement: source or title binding mismatch")
    return evidence
