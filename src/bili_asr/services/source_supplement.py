"""Plan and atomically supplement one collected source field without inference."""
from __future__ import annotations

import json
import re
import time
from copy import deepcopy
from pathlib import Path

from bili_asr.canonical_json import canonical, digest
from bili_asr.publication_content import normalize_actor
from bili_asr.source_supplements import DESCRIPTION_POLICY, POLICY
from bili_asr.storage.archive_contracts import require_universal_contract
from bili_asr.storage.import_origins import import_origin, require_import_extension
from bili_asr.storage.publication import (
    PublicationConflictError,
    PublicationRepository,
    read_edition,
)
from bili_asr.storage.source_supplements import (
    capture_field,
    edition_supplement,
    require_supplement_extension,
)

MAX_BATCH = 100


def install_source_supplement_extension(archive_root: Path) -> dict:
    from bili_asr.services.preserved_body_import import _install_extension

    def installed(connection):
        require_import_extension(connection, required=True)
        return require_supplement_extension(connection)

    return _install_extension(archive_root, POLICY, "schema-source-supplements.sql", installed)


def check_source_supplement(connection, *, edition_id: str, artifact_roots) -> dict:
    from bili_asr.preserved_body_evidence import check_preserved_body_import
    from bili_asr.storage.source_supplements import stored_supplement
    checked = check_preserved_body_import(connection, edition_id=edition_id, artifact_roots=artifact_roots)
    edition = read_edition(connection, edition_id)
    link = edition_supplement(connection, edition_id)
    if link is None:
        raise ValueError("source-supplement: edition has no supplemental evidence")
    evidence = stored_supplement(connection, link, edition["content"]["source"]["metadata"], edition["video_part_id"])
    field = evidence["evidence"]["field"]
    return {**checked, "supplementId": evidence["supplementId"], field: evidence["evidence"]["value"],
            "observedAt": evidence["evidence"]["observedAt"]}


def plan_source_supplement(connection, *, edition_ids: list[str], artifact_roots, field: str = "partTitle") -> dict:
    from bili_asr.preserved_body_evidence import check_preserved_body_import
    require_universal_contract(connection)
    if field not in {"partTitle", "description"}:
        raise ValueError("source-supplement: unsupported field")
    if (not isinstance(edition_ids, list) or not 1 <= len(edition_ids) <= MAX_BATCH
            or any(not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{32}", identity) is None for identity in edition_ids)
            or len(set(edition_ids)) != len(edition_ids)):
        raise ValueError("source-supplement: select 1..100 unique edition IDs")
    entries, skipped, blocked, seen = [], [], [], set()
    stats = {"field": field, "selected": len(edition_ids), "knownBefore": 0,
             "unknownBefore": 0, "eligible": 0, "excludedNative": 0}
    owned = not connection.in_transaction
    if owned:
        connection.execute("BEGIN")
    try:
        for identity in edition_ids:
            try:
                edition = read_edition(connection, identity)
                if edition["video_part_id"] in seen:
                    raise ValueError("source-supplement: multiple editions for one part")
                seen.add(edition["video_part_id"])
                if edition["current_edition_id"] != identity:
                    raise PublicationConflictError("source-supplement: selected edition is not the current draft head")
                if edition["content_version"] != 2 or import_origin(connection, identity) is None:
                    stats["excludedNative"] += 1
                    skipped.append({"editionId": identity, "reason": "not-preserved-legacy"})
                    continue
                metadata = edition["content"]["source"]["metadata"]
                if metadata[field] is not None:
                    stats["knownBefore"] += 1
                    skipped.append({"editionId": identity, "reason": "already-known"})
                    continue
                stats["unknownBefore"] += 1
                if edition_supplement(connection, identity) is not None:
                    raise ValueError("source-supplement: existing supplement evidence must be retained; multiple-field supplementation is unsupported")
                check_preserved_body_import(connection, edition_id=identity, artifact_roots=artifact_roots)
                evidence = capture_field(connection, edition, field)
                entries.append({"editionId": identity, "videoPartId": edition["video_part_id"],
                    "expectedContentSha256": edition["content_sha256"],
                    "expectedReleaseId": edition["current_release_id"], "supplement": evidence})
                stats["eligible"] += 1
            except (ValueError, OSError) as error:
                blocked.append({"editionId": identity, "reason": str(error)})
        plan = {"schemaVersion": 1, "policyVersion": DESCRIPTION_POLICY if field == "description" else POLICY, "entries": entries,
                "skipped": skipped, "blocked": blocked, "fieldStats": stats}
        return {**plan, "planDigest": digest(plan)}
    finally:
        if owned:
            connection.rollback()


def _validate_plan(plan: dict) -> None:
    fields = {"schemaVersion", "policyVersion", "entries", "skipped", "blocked", "fieldStats", "planDigest"}
    if not isinstance(plan, dict) or set(plan) != fields:
        raise ValueError("source-supplement: unknown plan fields")
    field = "description" if plan.get("policyVersion") == DESCRIPTION_POLICY else "partTitle"
    if (type(plan["schemaVersion"]) is not int or plan["schemaVersion"] != 1 or plan["policyVersion"] not in {POLICY, DESCRIPTION_POLICY}
            or plan["planDigest"] != digest({key: value for key, value in plan.items() if key != "planDigest"})
            or not isinstance(plan["entries"], list) or not 1 <= len(plan["entries"]) <= MAX_BATCH
            or not isinstance(plan["skipped"], list) or not isinstance(plan["blocked"], list) or plan["blocked"]):
        raise ValueError("source-supplement: altered, empty, blocked or unsupported plan")
    expected = {"editionId", "videoPartId", "expectedContentSha256", "expectedReleaseId", "supplement"}
    for entry in plan["entries"]:
        if (not isinstance(entry, dict) or set(entry) != expected or type(entry["videoPartId"]) is not int
                or not isinstance(entry["editionId"], str) or re.fullmatch(r"[0-9a-f]{32}", entry["editionId"]) is None):
            raise ValueError("source-supplement: invalid plan entry")
    if len({entry["videoPartId"] for entry in plan["entries"]}) != len(plan["entries"]):
        raise ValueError("source-supplement: duplicate selected parts")
    for skipped in plan["skipped"]:
        if (not isinstance(skipped, dict) or set(skipped) != {"editionId", "reason"}
                or not isinstance(skipped["editionId"], str) or re.fullmatch(r"[0-9a-f]{32}", skipped["editionId"]) is None
                or skipped["reason"] not in {"already-known", "not-preserved-legacy"}):
            raise ValueError("source-supplement: invalid skipped selection")
    selected = [entry["editionId"] for entry in plan["entries"]] + [entry["editionId"] for entry in plan["skipped"]]
    expected_stats = {"field": field, "selected": len(selected), "eligible": len(plan["entries"]),
        "unknownBefore": len(plan["entries"]), "knownBefore": sum(item["reason"] == "already-known" for item in plan["skipped"]),
        "excludedNative": sum(item["reason"] == "not-preserved-legacy" for item in plan["skipped"])}
    if (len(set(selected)) != len(selected) or len(selected) > MAX_BATCH or plan["fieldStats"] != expected_stats
            or any(type(value) is not int for key, value in plan["fieldStats"].items() if key != "field")):
        raise ValueError("source-supplement: invalid field statistics or duplicate selection")


def _applied_stats(plan: dict) -> dict:
    count = len(plan["entries"])
    return {**plan["fieldStats"], "supplemented": count,
            "knownAfter": plan["fieldStats"]["knownBefore"] + count,
            "unknownAfter": plan["fieldStats"]["unknownBefore"] - count}


def _receipt(connection, plan, artifact_roots) -> dict | None:
    row = connection.execute("SELECT * FROM source_supplement_batches WHERE plan_digest=?", (plan["planDigest"],)).fetchone()
    if row is None:
        return None
    result = json.loads(row["result_json"])
    if (row["plan_json"] != canonical(plan) or canonical(result) != row["result_json"]
            or set(result) != {"planDigest", "editions", "idempotent", "fieldStats"}
            or result["planDigest"] != plan["planDigest"] or result["idempotent"] is not False
            or result["fieldStats"] != _applied_stats(plan)
            or not isinstance(result["editions"], list) or len(result["editions"]) != len(plan["entries"])):
        raise ValueError("source-supplement: receipt mismatch")
    for entry, item in zip(plan["entries"], result["editions"], strict=True):
        if not isinstance(item, dict) or set(item) != {"editionId", "supplementId", "contentSha256"}:
            raise ValueError("source-supplement: invalid receipt edition")
        checked = check_source_supplement(connection, edition_id=item["editionId"], artifact_roots=artifact_roots)
        edition = read_edition(connection, item["editionId"])
        link = edition_supplement(connection, item["editionId"])
        if (checked["supplementId"] != entry["supplement"]["supplementId"]
                or item["supplementId"] != checked["supplementId"]
                or item["contentSha256"] != edition["content_sha256"]
                or edition["parent_edition_id"] != entry["editionId"]
                or link["root_edition_id"] != item["editionId"]):
            raise ValueError("source-supplement: receipt identity mismatch")
    return {**result, "idempotent": True}


def apply_source_supplement(connection, *, plan: dict, artifact_roots, actor: str) -> dict:
    from bili_asr.preserved_body_evidence import check_preserved_body_import
    _validate_plan(plan)
    actor = normalize_actor(actor)
    require_universal_contract(connection)
    require_supplement_extension(connection, required=True)
    field = plan["fieldStats"]["field"]
    repository = PublicationRepository(connection)
    with repository.transaction():
        receipt = _receipt(connection, plan, artifact_roots)
        if receipt:
            return receipt
        prepared = []
        # Verify every entry before creating anything. Live evidence is read
        # under the same write transaction as the compare-and-swap on heads.
        for entry in plan["entries"]:
            parent = read_edition(connection, entry["editionId"])
            if (parent["current_edition_id"] != entry["editionId"]
                    or parent["current_release_id"] != entry["expectedReleaseId"]):
                raise PublicationConflictError("source-supplement: draft or release head changed; create a new plan")
            if (parent["video_part_id"] != entry["videoPartId"]
                    or parent["content_sha256"] != entry["expectedContentSha256"]
                    or parent["content_version"] != 2 or import_origin(connection, parent["edition_id"]) is None
                    or parent["content"]["source"]["metadata"][field] is not None
                    or edition_supplement(connection, parent["edition_id"]) is not None):
                raise ValueError("source-supplement: selected frozen content differs from plan")
            check_preserved_body_import(connection, edition_id=parent["edition_id"], artifact_roots=artifact_roots)
            if capture_field(connection, parent, field) != entry["supplement"]:
                raise ValueError("source-supplement: collected source evidence changed or was invented")
            content = deepcopy(parent["content"])
            content["source"]["metadata"][field] = entry["supplement"]["evidence"]["value"]
            prepared.append((entry, parent, content))
        results = []
        for entry, parent, content in prepared:
            supplement = entry["supplement"]
            raw = canonical(supplement["evidence"])
            existing = connection.execute("SELECT evidence_json FROM source_metadata_supplements WHERE supplement_id=?",
                                          (supplement["supplementId"],)).fetchone()
            if existing and existing[0] != raw:
                raise ValueError("source-supplement: registered evidence mismatch")
            if not existing:
                connection.execute("INSERT INTO source_metadata_supplements VALUES (?,?,?)",
                                   (supplement["supplementId"], raw, int(time.time())))
            edition = repository.insert_edition(part_id=parent["video_part_id"], revision_id=parent["revision_id"],
                content=content, parent_edition_id=parent["edition_id"], actor=actor,
                note=f"Supplement collected {field} {supplement['supplementId']}", event_type="edited", content_version=2)
            connection.execute("INSERT INTO publication_source_supplements VALUES (?,?,?)",
                               (edition["edition_id"], supplement["supplementId"], edition["edition_id"]))
            read_edition(connection, edition["edition_id"])
            results.append({"editionId": edition["edition_id"], "supplementId": supplement["supplementId"],
                            "contentSha256": edition["content_sha256"]})
        stats = _applied_stats(plan)
        result = {"planDigest": plan["planDigest"], "editions": results, "idempotent": False, "fieldStats": stats}
        connection.execute("INSERT INTO source_supplement_batches VALUES (?,?,?,?,?)",
                           (plan["planDigest"], canonical(plan), canonical(result), actor, int(time.time())))
    return result
