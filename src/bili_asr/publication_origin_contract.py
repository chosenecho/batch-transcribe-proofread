"""Pure public origin envelope and evidence bindings, independent of import application."""
from __future__ import annotations

import re

from bili_asr.canonical_json import digest
from bili_asr.contracts.registry import ORIGIN_PROFILE as PROFILE
from bili_asr.contracts.registry import ORIGIN_PROFILE_V2
from bili_asr.source_supplements import DESCRIPTION_POLICY, POLICY as SUPPLEMENT_POLICY, validate_supplement
from bili_asr.contracts.content_policies import content_policy, missing_value
from bili_asr.contracts.registry import LEGACY_FACTS_POLICY
from bili_asr.file_ops import FileOperationError as ExportSnapshotError

COMMON = {"kind", "editionId", "aiRevisionId", "videoPartId", "contentSha256",
          "sourceMetadataSha256", "inputVersion", "aiTemplateVersion"}
IMPORTED = {"importId", "policyVersion", "legacyAiRevisionId", "legacyEditionId", "legacyReleaseId",
            "baselineBodySha256", "currentBodySha256", "reviewArtifactSha256", "importedAt",
            "bodyPreserved", "metadataEvidence"}


def validate_origins(value: object, articles: list[dict], manuscript_type: str) -> dict:
    def fail(message):
        raise ExportSnapshotError("origin-profile: " + message)

    def hexadecimal(identity, size=64):
        return isinstance(identity, str) and re.fullmatch(rf"[0-9a-f]{{{size}}}", identity) is not None

    if (not isinstance(value, dict) or set(value) != {"schemaVersion", "manuscriptType", "contractProfile", "entries"}
            or type(value["schemaVersion"]) is not int or value["schemaVersion"] != (2 if value.get("contractProfile") == ORIGIN_PROFILE_V2 else 1)
            or value["manuscriptType"] != manuscript_type or value["contractProfile"] not in {PROFILE, ORIGIN_PROFILE_V2}
            or not isinstance(value["entries"], list)):
        fail("unknown origins envelope")
    indexed = {article["editionId"]: article for article in articles}
    if len(indexed) != len(articles) or len(value["entries"]) != len(articles):
        fail("one origin per article is required")
    seen = []
    for entry in value["entries"]:
        if not isinstance(entry, dict):
            fail("origin must be an object")
        native = entry.get("kind") == "ai-generated-v2"
        policies = {LEGACY_FACTS_POLICY, SUPPLEMENT_POLICY}
        if value["contractProfile"] == ORIGIN_PROFILE_V2:
            policies.add(DESCRIPTION_POLICY)
        supplemented = not native and entry.get("policyVersion") in {SUPPLEMENT_POLICY, DESCRIPTION_POLICY}
        expected_fields = COMMON if native else COMMON | IMPORTED | ({"metadataSupplement"} if supplemented else set())
        if entry.get("kind") not in {"ai-generated-v2", "preserved-legacy-body", "edited-after-preservation"} or set(entry) != expected_fields:
            fail("unknown origin kind or fields")
        if not hexadecimal(entry["editionId"], 32):
            fail("invalid edition ID")
        article = indexed.get(entry["editionId"])
        if article is None or article.get("contentVersion") != 2:
            fail("origin has no universal article")
        seen.append(entry["editionId"])
        for field in ("editionId", "aiRevisionId", "videoPartId", "contentSha256"):
            if type(entry[field]) is not type(article[field]) or entry[field] != article[field]:
                fail("article identity mismatch: " + field)
        if entry["sourceMetadataSha256"] != digest(article["sourceMetadata"]):
            fail("source metadata digest mismatch")
        integers = [entry["videoPartId"], article["partIndex"], article.get("createdAt", article.get("publishedAt"))]
        integers.extend(value for value in article["sourceMetadata"].values() if type(value) is int)
        if any(type(value) is not int or not 0 <= value <= 2**53 - 1 for value in integers):
            fail("public integer outside JavaScript safe range")
        if type(entry["inputVersion"]) is not int or entry["inputVersion"] != (2 if native else 1) or entry["aiTemplateVersion"] != ("ai-draft-v2" if native else "ai-draft-v1"):
            fail("origin input/template mismatch")
        if native:
            continue
        for field in ("importId", "baselineBodySha256", "currentBodySha256", "reviewArtifactSha256"):
            if not hexadecimal(entry[field]):
                fail("invalid digest: " + field)
        for field, size in (("legacyEditionId", 32), ("legacyReleaseId", 64)):
            if entry[field] is not None and not hexadecimal(entry[field], size):
                fail("invalid legacy identity")
        if (entry["legacyAiRevisionId"] != article["aiRevisionId"] or entry["policyVersion"] not in policies
                or entry["reviewArtifactSha256"] != article["reviewArtifactSha256"]
                or type(entry["importedAt"]) is not int or not 0 <= entry["importedAt"] <= 2**53 - 1):
            fail("invalid import binding or time")
        if supplemented:
            try:
                validate_supplement(entry["metadataSupplement"], article["sourceMetadata"], article["videoPartId"])
                if entry["metadataSupplement"]["evidence"]["field"] != ("description" if entry["policyVersion"] == DESCRIPTION_POLICY else "partTitle"):
                    fail("supplement field differs from policy")
            except (ValueError, KeyError, TypeError) as error:
                fail(str(error))
        preserved = entry["kind"] == "preserved-legacy-body"
        if type(entry["bodyPreserved"]) is not bool or entry["bodyPreserved"] != preserved or (entry["baselineBodySha256"] == entry["currentBodySha256"]) != preserved:
            fail("false body preservation claim")
        evidence = entry["metadataEvidence"]
        if not isinstance(evidence, list) or len(evidence) != len(article["sourceMetadata"]):
            fail("incomplete metadata evidence")
        fields = []
        for fact in evidence:
            if not isinstance(fact, dict) or set(fact) != {"field", "kind", "observedAt", "valueSha256"}:
                fail("invalid evidence fields")
            field = fact["field"]
            if not isinstance(field, str) or field not in article["sourceMetadata"]:
                fail("unknown evidence field")
            fields.append(field)
            expected_kind = content_policy(entry["policyVersion"]).field_kind(field)
            observed = (entry["metadataSupplement"]["evidence"]["observedAt"]
                        if entry["policyVersion"] == DESCRIPTION_POLICY and field == "description" else None)
            if (fact["kind"] != expected_kind or type(fact["observedAt"]) is not type(observed) or fact["observedAt"] != observed
                    or fact["valueSha256"] != digest(article["sourceMetadata"][field])):
                fail("metadata evidence differs from frozen-facts policy")
            if expected_kind == "unobserved" and article["sourceMetadata"][field] != missing_value(field):
                fail("unobserved source value is not unknown")
        if fields != sorted(article["sourceMetadata"]) or len(set(fields)) != len(fields):
            fail("metadata evidence is duplicated or unsorted")
    if seen != sorted(indexed) or len(set(seen)) != len(seen):
        fail("origins are duplicated, missing or unsorted")
    return value
