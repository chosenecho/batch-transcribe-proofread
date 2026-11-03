"""Public origin profile: exact fields and catalog/evidence cross-file binding."""
from __future__ import annotations

from bili_asr.canonical_json import digest
from bili_asr.publication_origin_contract import validate_origins as validate_origins
from bili_asr.contracts.registry import ORIGIN_PROFILE as PROFILE  # Public compatibility export.
from bili_asr.source_supplements import DESCRIPTION_POLICY, POLICY as SUPPLEMENT_POLICY

__all__ = ["PROFILE", "public_origin", "validate_origins"]


def public_origin(connection, edition: dict, artifact_roots) -> dict:
    from bili_asr.preserved_body_evidence import check_preserved_body_import
    from bili_asr.storage.import_origins import import_origin, read_baseline
    from bili_asr.storage.publication import read_revision
    from bili_asr.storage.archive_contracts import frozen_version

    if edition["content_version"] != 2:
        raise ValueError("origin-profile: legacy content cannot enter universal-origin-v1")
    origin = import_origin(connection, edition["edition_id"])
    result = {"kind": "ai-generated-v2", "editionId": edition["edition_id"],
        "aiRevisionId": edition["revision_id"], "videoPartId": edition["video_part_id"],
        "contentSha256": edition["content_sha256"], "sourceMetadataSha256": digest(edition["content"]["source"]["metadata"]),
        "inputVersion": 2, "aiTemplateVersion": "ai-draft-v2"}
    if origin is None:
        revision, _ = read_revision(connection, edition["revision_id"])
        if frozen_version(connection, "input", revision["input_id"]) != 2:
            raise ValueError("origin-profile: native edition requires v2 input")
        return result
    checked = check_preserved_body_import(connection, edition_id=edition["edition_id"], artifact_roots=artifact_roots)
    stored = read_baseline(connection, origin["import_id"])
    baseline = stored["baseline"]
    result.update({"kind": "preserved-legacy-body" if checked["bodyPreserved"] else "edited-after-preservation",
        "inputVersion": 1, "aiTemplateVersion": "ai-draft-v1", "importId": origin["import_id"],
        "policyVersion": baseline["policyVersion"], "legacyAiRevisionId": baseline["revisionId"],
        "legacyEditionId": baseline["legacyEditionId"], "legacyReleaseId": baseline["legacyReleaseId"],
        "baselineBodySha256": checked["baselineBodySha256"], "currentBodySha256": checked["currentBodySha256"],
        "reviewArtifactSha256": checked["reviewArtifactSha256"], "importedAt": stored["imported_at"],
        "bodyPreserved": checked["bodyPreserved"], "metadataEvidence": [
            {"field": field, "kind": evidence["kind"], "observedAt": evidence["observedAt"], "valueSha256": evidence["valueSha256"]}
            for field, evidence in sorted(baseline["metadataEvidence"].items())]})
    from bili_asr.storage.source_supplements import edition_supplement, stored_supplement
    link = edition_supplement(connection, edition["edition_id"])
    if link:
        supplement = stored_supplement(connection, link, edition["content"]["source"]["metadata"], edition["video_part_id"])
        field = supplement["evidence"]["field"]
        result["policyVersion"] = DESCRIPTION_POLICY if field == "description" else SUPPLEMENT_POLICY
        result["metadataSupplement"] = supplement
        for fact in result["metadataEvidence"]:
            if fact["field"] == field:
                fact.update(kind=supplement["evidence"]["sourceKind"], observedAt=supplement["evidence"]["observedAt"],
                            valueSha256=digest(supplement["evidence"]["value"]))
    return result
