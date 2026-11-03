"""Immutable archive evidence and its inheritance through imported editions."""
from __future__ import annotations
from bili_asr.storage.extension_contracts import require_supplement_extension, supplement_objects as supplement_objects

import json
from copy import deepcopy

from bili_asr.canonical_json import canonical, digest
from bili_asr.source_supplements import DESCRIPTION_KIND, KIND, POLICY, validate_supplement






def edition_supplement(connection, edition_id: str) -> dict | None:
    if not require_supplement_extension(connection):
        return None
    row = connection.execute("SELECT * FROM publication_source_supplements WHERE edition_id=?", (edition_id,)).fetchone()
    return dict(row) if row else None


def capture_part_title(connection, edition: dict) -> dict:
    """Capture only the stored part title, never a caller's proposed value."""
    from bili_asr.storage.sources import SourceRepository
    part = SourceRepository(connection).part(edition["video_part_id"])
    source = edition["content"]["source"]["metadata"]
    ref = part["content_ref"]
    if (ref.platform != "bilibili" or any(source[field] != value for field, value in (
            ("platform", ref.platform), ("externalVideoId", ref.external_video_id), ("partIndex", ref.part_index)))):
        raise ValueError("source-supplement: current part differs from frozen identity")
    # Old collection allowed a video-title fallback for an empty single-part
    # title. Without raw evidence, an equal title cannot attest a real part title.
    if part["title"] == part["video_title"]:
        raise ValueError("source-supplement: title may be a video-title fallback; require explicit part evidence")
    evidence = {"version": 1, "platform": ref.platform, "externalVideoId": ref.external_video_id,
        "partIndex": ref.part_index, "videoPartId": edition["video_part_id"], "cid": part["cid"],
        "field": "partTitle", "value": part["title"], "sourceKind": KIND, "observedAt": None}
    result = {"supplementId": digest(evidence), "evidence": evidence}
    validate_supplement(result, {**source, "partTitle": part["title"]}, edition["video_part_id"])
    return result


def capture_description(connection, edition: dict) -> dict:
    """Freeze the retained database description and its field observation time.

    A legacy video-details timestamp may have come from another field update,
    so without the per-field receipt the acquisition time remains unknown.
    This reads local facts only and makes no upstream request.
    """
    from bili_asr.storage.sources import SourceRepository
    part = SourceRepository(connection).part(edition["video_part_id"])
    source = edition["content"]["source"]["metadata"]
    ref = part["content_ref"]
    if (ref.platform != "bilibili" or any(source[field] != value for field, value in (
            ("platform", ref.platform), ("externalVideoId", ref.external_video_id), ("partIndex", ref.part_index)))):
        raise ValueError("source-supplement: current part differs from frozen identity")
    row = connection.execute('SELECT "desc" FROM video_details WHERE bvid=?', (ref.external_video_id,)).fetchone()
    if row is None or row[0] is None:
        raise ValueError("source-supplement: description is unknown; collect real source metadata first")
    observed = connection.execute("SELECT last_success_at,value_json FROM source_metadata_observations "
        "WHERE platform='bilibili' AND external_id=? AND operation='details' AND field='desc'",
        (ref.external_video_id,)).fetchone()
    stamp = None
    if observed and observed[0] is not None:
        from bili_asr.metadata_acceptance import unpack_cache
        if observed[1] is None or unpack_cache(observed[1])[0] != row[0]:
            raise ValueError("source-supplement: description disagrees with successful field observation")
        stamp = observed[0]
    evidence = {"version": 2, "platform": ref.platform, "externalVideoId": ref.external_video_id,
        "partIndex": ref.part_index, "videoPartId": edition["video_part_id"], "cid": part["cid"],
        "field": "description", "value": row[0], "sourceKind": DESCRIPTION_KIND, "observedAt": stamp}
    result = {"supplementId": digest(evidence), "evidence": evidence}
    validate_supplement(result, {**source, "description": row[0]}, edition["video_part_id"])
    return result


def capture_field(connection, edition: dict, field: str) -> dict:
    if field == "partTitle":
        return capture_part_title(connection, edition)
    if field == "description":
        return capture_description(connection, edition)
    raise ValueError("source-supplement: unsupported field")


def stored_supplement(connection, link: dict, source: dict, part_id: int) -> dict:
    row = connection.execute("SELECT * FROM source_metadata_supplements WHERE supplement_id=?", (link["supplement_id"],)).fetchone()
    if row is None:
        raise ValueError("source-supplement: evidence is missing")
    evidence = json.loads(row["evidence_json"])
    result = {"supplementId": row["supplement_id"], "evidence": evidence}
    if canonical(evidence) != row["evidence_json"] or type(row["registered_at"]) is not int or row["registered_at"] < 0:
        raise ValueError("source-supplement: invalid stored evidence")
    validate_supplement(result, source, part_id)
    from bili_asr.storage.sources import SourceRepository
    part = SourceRepository(connection).part(part_id)
    if part["cid"] != evidence["cid"]:
        raise ValueError("source-supplement: CID binding mismatch")
    return result


def expected_source(connection, edition: dict, baseline: dict) -> dict:
    """Rebuild each edition's source without consulting mutable title values."""
    source = deepcopy(baseline["content"]["source"])
    link = edition_supplement(connection, edition["edition_id"])
    if link is None:
        return source
    current_metadata = edition["content"]["source"]["metadata"]
    supplement = stored_supplement(connection, link, current_metadata, edition["video_part_id"])
    field = supplement["evidence"]["field"]
    if source["metadata"][field] is not None:
        raise ValueError("source-supplement: cannot overwrite a frozen source value")
    source["metadata"][field] = supplement["evidence"]["value"]
    from bili_asr.storage.import_origins import body_digest, import_origin
    from bili_asr.storage.publication import PublicationRepository
    repository = PublicationRepository(connection)
    root = repository.edition(link["root_edition_id"])
    root_link = edition_supplement(connection, root["edition_id"])
    root_origin = import_origin(connection, root["edition_id"])
    current_origin = import_origin(connection, edition["edition_id"])
    if (root_link is None or root_origin is None or current_origin is None
            or root_link["supplement_id"] != link["supplement_id"]
            or root_link["root_edition_id"] != root["edition_id"]
            or any(root_origin[key] != current_origin[key] for key in ("import_id", "root_edition_id"))
            or root["content_version"] != 2 or root["video_part_id"] != baseline["videoPartId"]
            or root["revision_id"] != baseline["revisionId"] or root["content"]["source"] != source
            or not root["parent_edition_id"]):
        raise ValueError("source-supplement: invalid supplemental root")
    parent = repository.edition(root["parent_edition_id"])
    parent_origin = import_origin(connection, parent["edition_id"])
    if (edition_supplement(connection, parent["edition_id"]) is not None or parent_origin is None
            or any(parent_origin[key] != root_origin[key] for key in ("import_id", "root_edition_id"))
            or parent["content_version"] != 2 or parent["revision_id"] != baseline["revisionId"]
            or parent["video_part_id"] != baseline["videoPartId"]
            or parent["content"]["source"] != baseline["content"]["source"]):
        raise ValueError("source-supplement: invalid supplemental parent")
    expected_content = deepcopy(parent["content"])
    expected_content["source"] = source
    if (root["content"] != expected_content
            or root_origin["expected_markdown_sha256"] != body_digest(root["content"]["markdown"])):
        raise ValueError("source-supplement: supplementation changed existing manuscript content")
    return source


def validate_supplement_inheritance(connection, child: dict, parent: dict) -> None:
    child_link = edition_supplement(connection, child["edition_id"])
    parent_link = edition_supplement(connection, parent["edition_id"])
    if child_link and child_link["root_edition_id"] == child["edition_id"]:
        if parent_link:
            raise ValueError("source-supplement: repeated supplemental root")
        return
    if (child_link is None) != (parent_link is None) or (child_link and any(
            child_link[key] != parent_link[key] for key in ("supplement_id", "root_edition_id"))):
        raise ValueError("source-supplement: ancestor lost or changed evidence")


def inherit_supplement(connection, parent_id: str, edition_id: str) -> None:
    link = edition_supplement(connection, parent_id)
    if link:
        connection.execute("INSERT INTO publication_source_supplements VALUES (?,?,?)",
            (edition_id, link["supplement_id"], link["root_edition_id"]))
