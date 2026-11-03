"""Registered immutable evidence for a v2 edition backed by a real v1 input."""
from __future__ import annotations
from bili_asr.storage.extension_contracts import require_import_extension, import_objects as extension_objects

import hashlib
import json
import sqlite3

from bili_asr.canonical_json import canonical, digest
from bili_asr.storage.archive_contracts import frozen_version
from bili_asr.contracts.registry import IMPORT_EXTENSION as EXTENSION, LEGACY_FACTS_POLICY as POLICY
from bili_asr.contracts.content_policies import content_policy


def body_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()






def import_origin(connection: sqlite3.Connection, edition_id: str) -> dict | None:
    if not require_import_extension(connection):
        return None
    row = connection.execute("SELECT * FROM publication_import_origins WHERE edition_id=?", (edition_id,)).fetchone()
    return dict(row) if row else None


def read_baseline(connection: sqlite3.Connection, import_id: str) -> dict:
    require_import_extension(connection, required=True)
    row = connection.execute("SELECT * FROM manuscript_import_baselines WHERE import_id=?", (import_id,)).fetchone()
    if row is None:
        raise ValueError("import-integrity: missing baseline")
    result = dict(row)
    baseline = json.loads(result["baseline_json"])
    if canonical(baseline) != result["baseline_json"] or digest(baseline) != import_id:
        raise ValueError("import-integrity: baseline identity mismatch")
    bindings = {"video_part_id": "videoPartId", "revision_id": "revisionId",
                "legacy_edition_id": "legacyEditionId", "body_sha256": "bodySha256", "review_sha256": "reviewSha256"}
    if any(result[left] != baseline[right] for left, right in bindings.items()):
        raise ValueError("import-integrity: baseline row bindings mismatch")
    for column, filename in (("body_path", "body.md"), ("review_path", "review.md")):
        if result[column] != f"documents/imports/{import_id}/{filename}":
            raise ValueError("import-integrity: invalid baseline path")
    result["baseline"] = baseline
    validate_baseline(connection, baseline)
    return result


def frozen_legacy_source(connection, prepared: dict) -> tuple[dict, dict]:
    from bili_asr.storage.sources import SourceRepository
    from bili_asr.source_metadata import SourceMetadataSnapshot
    ref = SourceRepository(connection).part(prepared["snapshot"]["video_part_id"])["content_ref"]
    metadata = prepared["snapshot"]["metadata"]
    if (ref.platform, ref.external_video_id, ref.part_index) != ("bilibili", metadata["bvid"], metadata["page_index"]):
        raise ValueError("import-integrity: legacy source mapping differs from frozen input")
    source = SourceMetadataSnapshot(ref, metadata["title"], None, None, None, None).to_dict()
    policy = content_policy(POLICY)
    evidence = {field: {"kind": policy.field_kind(field),
        "objectId": prepared["input_id"] if policy.field_kind(field) == "legacy-input" else None,
        "observedAt": None, "valueSha256": digest(value)} for field, value in source.items()}
    return source, evidence


def validate_baseline(connection, baseline: dict) -> None:
    from bili_asr.storage.publication import read_revision, read_edition, PublicationRepository
    from bili_asr.publication_models_v2 import normalize_content_v2
    fields = {"policyVersion", "selector", "videoPartId", "revisionId", "inputId", "legacyEditionId",
        "legacyReleaseId", "sourceFingerprint", "bodySha256", "reviewSha256", "content",
        "metadataEvidence", "legacyAttribution", "legacyEditorNote"}
    if not isinstance(baseline, dict) or set(baseline) != fields or baseline["policyVersion"] != POLICY:
        raise ValueError("import-integrity: unknown baseline policy or fields")
    revision, prepared = read_revision(connection, baseline["revisionId"])
    source, evidence = frozen_legacy_source(connection, prepared)
    if (frozen_version(connection, "input", revision["input_id"]) != 1
            or revision["input_id"] != baseline["inputId"] or revision["video_part_id"] != baseline["videoPartId"]
            or baseline["content"] != normalize_content_v2(baseline["content"])
            or baseline["content"]["source"]["videoPartId"] != baseline["videoPartId"]
            or baseline["content"]["source"]["metadata"] != source or baseline["metadataEvidence"] != evidence
            or baseline["sourceFingerprint"] != digest({"ref": source, "videoPartId": baseline["videoPartId"]})
            or body_digest(baseline["content"]["markdown"]) != baseline["bodySha256"]):
        raise ValueError("import-integrity: frozen baseline facts or body mismatch")
    selector = baseline["selector"]
    expected = {"kind": "edition", "id": baseline["legacyEditionId"]} if baseline["legacyEditionId"] else {"kind": "ai-revision", "id": baseline["revisionId"]}
    if selector != expected:
        raise ValueError("import-integrity: baseline selector mismatch")
    if baseline["legacyEditionId"]:
        legacy = read_edition(connection, baseline["legacyEditionId"])
        if (legacy["content_version"] != 1 or legacy["revision_id"] != baseline["revisionId"]
                or legacy["video_part_id"] != baseline["videoPartId"]
                or body_digest(legacy["content"]["markdown"]) != baseline["bodySha256"]):
            raise ValueError("import-integrity: legacy edition binding mismatch")
    from bili_asr.manuscript_templates import AI_RENDERERS, renderer_for
    rendered = renderer_for(AI_RENDERERS, "ai-draft-v1")(
        prepared["snapshot"]["metadata"], prepared, json.loads(revision["blocks_json"]), baseline["revisionId"])
    if (body_digest(rendered["review.md"]) != baseline["reviewSha256"]
            or (baseline["legacyEditionId"] is None and body_digest(rendered["ai-draft.md"]) != baseline["bodySha256"])):
        raise ValueError("import-integrity: baseline differs from fixed legacy renderer")
    if baseline["legacyReleaseId"]:
        release = PublicationRepository(connection).release(baseline["legacyReleaseId"])
        if release["edition_id"] != baseline["legacyEditionId"] or release["video_part_id"] != baseline["videoPartId"]:
            raise ValueError("import-integrity: legacy release binding mismatch")


def validate_edition_origin(connection: sqlite3.Connection, edition: dict, revision: dict) -> dict:
    origin = import_origin(connection, edition["edition_id"])
    if origin is None:
        raise ValueError("import-integrity: v2/v1 edition requires immutable import origin")
    baseline = read_baseline(connection, origin["import_id"])["baseline"]
    from bili_asr.storage.source_supplements import expected_source, validate_supplement_inheritance
    if (edition["content_version"] != 2 or frozen_version(connection, "input", revision["input_id"]) != 1
            or revision["input_id"] != baseline["inputId"] or edition["revision_id"] != baseline["revisionId"]
            or edition["video_part_id"] != baseline["videoPartId"]
            or edition["content"]["source"] != expected_source(connection, edition, baseline)):
        raise ValueError("import-integrity: edition differs from frozen import identity")
    current = body_digest(edition["content"]["markdown"])
    expected_relation = "preserved" if current == baseline["bodySha256"] else "derived"
    if current != origin["expected_markdown_sha256"] or origin["relation"] != expected_relation:
        raise ValueError("import-integrity: body or preservation claim mismatch")
    from bili_asr.storage.publication import PublicationRepository
    from bili_asr.publication_models_v2 import normalize_content_v2
    repository = PublicationRepository(connection)
    root = repository.edition(origin["root_edition_id"])
    root_origin = import_origin(connection, root["edition_id"])
    if (root_origin is None or root_origin["import_id"] != origin["import_id"]
            or root_origin["root_edition_id"] != root["edition_id"] or root_origin["relation"] != "preserved"
            or root["content"] != baseline["content"] or root["revision_id"] != baseline["revisionId"]
            or root["video_part_id"] != baseline["videoPartId"] or root["content_version"] != 2):
        raise ValueError("import-integrity: invalid root edition")
    child, seen = edition, {edition["edition_id"]}
    while child["edition_id"] != root["edition_id"]:
        parent_id = child["parent_edition_id"]
        if parent_id is None or parent_id in seen:
            raise ValueError("import-integrity: broken or cyclic origin ancestry")
        seen.add(parent_id)
        parent_origin = import_origin(connection, parent_id)
        if parent_origin is None or any(parent_origin[key] != origin[key] for key in ("import_id", "root_edition_id")):
            raise ValueError("import-integrity: derived edition lost its parent evidence")
        parent = repository.edition(parent_id)
        parent_hash = body_digest(parent["content"]["markdown"])
        if (parent["content_version"] != 2 or parent["revision_id"] != baseline["revisionId"]
                or parent["video_part_id"] != baseline["videoPartId"]
                or normalize_content_v2(parent["content"]) != parent["content"]
                or parent["content"]["source"] != expected_source(connection, parent, baseline)
                or parent_origin["expected_markdown_sha256"] != parent_hash
                or parent_origin["relation"] != ("preserved" if parent_hash == baseline["bodySha256"] else "derived")):
            raise ValueError("import-integrity: invalid ancestor edition")
        validate_supplement_inheritance(connection, child, parent)
        child = parent
    return baseline


def inherit_import_origin(connection: sqlite3.Connection, parent_id: str, edition: dict) -> None:
    origin = import_origin(connection, parent_id)
    if origin is None:
        return
    baseline = read_baseline(connection, origin["import_id"])["baseline"]
    current = body_digest(edition["content"]["markdown"])
    connection.execute("INSERT INTO publication_import_origins VALUES (?,?,?,?,?)", (
        edition["edition_id"], origin["import_id"], origin["root_edition_id"],
        "preserved" if current == baseline["bodySha256"] else "derived", current))
    from bili_asr.storage.source_supplements import inherit_supplement
    inherit_supplement(connection, parent_id, edition["edition_id"])
