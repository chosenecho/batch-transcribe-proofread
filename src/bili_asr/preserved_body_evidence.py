"""Read-only reconstruction and verification of preserved legacy body evidence.

Import planning/application and publication consume the same exact frozen baseline.
"""
from __future__ import annotations

import hashlib
import re

from bili_asr.canonical_json import digest
from bili_asr.services.artifact_readers import read_retained_text
from bili_asr.manuscript_artifacts import get_ai_artifacts
from bili_asr.publication_content import content_from_ai
from bili_asr.publication_models_v2 import normalize_content_v2
from bili_asr.source_identity import source_url
from bili_asr.storage.archive_contracts import frozen_version
from bili_asr.storage.import_origins import body_digest, import_origin, read_baseline, frozen_legacy_source, POLICY
from bili_asr.storage.publication import (PublicationRepository, read_revision,
    read_edition as get_edition, verify_release_identity)
from bili_asr.storage.sources import SourceRepository


def selected_baseline(connection, selector: dict, artifact_roots) -> tuple[dict, bytes, bytes]:
    if (not isinstance(selector, dict) or set(selector) != {"kind", "id"}
            or not isinstance(selector["kind"], str) or selector["kind"] not in {"ai-revision", "edition"}):
        raise ValueError("import-selector: explicitly select ai-revision or edition and ID")
    size = 32 if selector["kind"] == "edition" else 64
    if not isinstance(selector["id"], str) or re.fullmatch(rf"[0-9a-f]{{{size}}}", selector["id"]) is None:
        raise ValueError("import-selector: invalid legacy source ID")
    old_edition = get_edition(connection, selector["id"]) if selector["kind"] == "edition" else None
    revision_id = old_edition["revision_id"] if old_edition else selector["id"]
    revision, prepared = read_revision(connection, revision_id)
    if frozen_version(connection, "input", revision["input_id"]) != 1 or (old_edition and old_edition["content_version"] != 1):
        raise ValueError("import-selector: only verified legacy v1 inputs and editions are eligible")
    artifacts = get_ai_artifacts(connection, revision_id, artifact_roots)
    legacy = old_edition["content"] if old_edition else content_from_ai(prepared, artifacts["ai-draft.md"].decode("utf-8"))
    body = legacy["markdown"].encode("utf-8") if old_edition else artifacts["ai-draft.md"]
    part_id = revision["video_part_id"]
    ref = SourceRepository(connection).part(part_id)["content_ref"]
    metadata = prepared["snapshot"]["metadata"]
    if (ref.platform, ref.external_video_id, ref.part_index) != ("bilibili", metadata["bvid"], metadata["page_index"]):
        raise ValueError("import-integrity: legacy source mapping differs from frozen input")
    # v1 inputs attest only title and identity. Missing facts stay unknown; no
    # current observations or editorial title/tags are presented as source facts.
    source_metadata, evidence = frozen_legacy_source(connection, prepared)
    content = normalize_content_v2({
        **legacy, "markdown": body.decode("utf-8"),
        "source": {"platform": ref.platform, "externalVideoId": ref.external_video_id,
                   "partIndex": ref.part_index, "videoPartId": part_id, "url": source_url(ref), "metadata": source_metadata},
        "attribution": "Body preserved from a legacy AI-assisted manuscript; no new AI inference during import.",
        "editorNote": "Imported edition requires human review. The reference is the historical AI baseline.",
    })
    if content["markdown"].encode("utf-8") != body or body.startswith(b"\xef\xbb\xbf"):
        raise ValueError("body_not_canonical: import would alter the selected UTF-8 body")
    release = PublicationRepository(connection).release_for_edition(old_edition["edition_id"]) if old_edition else None
    if release:
        verify_release_identity(connection, release)
        read_retained_text(connection, release["relative_path"], release["artifact_sha256"], artifact_roots)
    baseline = {
        "policyVersion": POLICY, "selector": selector, "videoPartId": part_id,
        "revisionId": revision_id, "inputId": revision["input_id"],
        "legacyEditionId": old_edition["edition_id"] if old_edition else None,
        "legacyReleaseId": release["release_id"] if release else None,
        "sourceFingerprint": digest({"ref": source_metadata, "videoPartId": part_id}),
        "bodySha256": hashlib.sha256(body).hexdigest(), "reviewSha256": hashlib.sha256(artifacts["review.md"]).hexdigest(),
        "content": content, "metadataEvidence": evidence,
        "legacyAttribution": legacy["attribution"], "legacyEditorNote": legacy["editorNote"],
    }
    return baseline, body, artifacts["review.md"]


def check_preserved_body_import(connection, *, edition_id: str, artifact_roots) -> dict:
    edition = get_edition(connection, edition_id)
    origin = import_origin(connection, edition_id)
    if origin is None:
        raise ValueError("import-check: edition has no preservation origin")
    stored = read_baseline(connection, origin["import_id"])
    baseline, body, review = selected_baseline(connection, stored["baseline"]["selector"], artifact_roots)
    if baseline != stored["baseline"]:
        raise ValueError("import-integrity: legacy evidence differs from import baseline")
    if (read_retained_text(connection, stored["body_path"], stored["body_sha256"], artifact_roots) != body
            or read_retained_text(connection, stored["review_path"], stored["review_sha256"], artifact_roots) != review):
        raise ValueError("import-integrity: baseline files differ from legacy evidence")
    return {"valid": True, "importId": origin["import_id"], "editionId": edition_id,
            "bodyPreserved": origin["relation"] == "preserved", "baselineBodySha256": stored["body_sha256"],
            "currentBodySha256": body_digest(edition["content"]["markdown"]), "reviewArtifactSha256": stored["review_sha256"]}
