"""Pure edition provenance checks shared by live and portable readers."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from bili_asr.canonical_json import canonical, digest


def verify_edition(record, revision_record, input_record, artifacts, parents):
    edition, revision = record["edition"], revision_record["row"]
    prepared = input_record["row"]
    snapshot = json.loads(prepared["prepared_json"])["snapshot"]
    if (edition["revision_id"] != revision["revision_id"] or edition["video_part_id"] != prepared["video_part_id"]
            or digest(edition["content"]) != edition["content_sha256"] or canonical(edition["content"]) != edition["content_json"]
            or edition["review_content_sha256"] != edition["content_sha256"]):
        raise ValueError("provenance: publication edition digest mismatch")
    if (edition["parent_edition_id"] is None and parents) or (edition["parent_edition_id"] is not None
            and (len(parents) != 1 or parents[0]["edition"]["edition_id"] != edition["parent_edition_id"]
                 or parents[0]["edition"]["video_part_id"] != edition["video_part_id"])):
        raise ValueError("provenance: edition parent dependency mismatch")
    from bili_asr.publication_content import normalize_content
    from bili_asr.publication_models_v2 import normalize_content_v2
    version = edition["content_version"]
    if type(version) is not int or version not in (1, 2):
        raise ValueError("provenance: unsupported publication content version")
    if (normalize_content_v2 if version == 2 else normalize_content)(edition["content"]) != edition["content"]:
        raise ValueError("provenance: invalid publication content")
    origin, baseline, supplement = record.get("import_origin"), record.get("import_baseline"), record.get("source_supplement")
    source = edition["content"]["source"]
    if version == input_record["version"] and origin is None and baseline is None and supplement is None:
        metadata = snapshot["metadata"]
        matches = (source["metadata"] == metadata["source_metadata"] if version == 2 else
                   (source["bvid"], source["pageIndex"]) == (metadata["bvid"], metadata["page_index"]))
        if not matches or source["videoPartId"] != edition["video_part_id"]:
            raise ValueError("provenance: edition source differs from frozen input")
    elif version == 2 and input_record["version"] == 1 and origin is not None and baseline is not None:
        value = baseline["baseline"]
        if (digest(value) != baseline["import_id"] or canonical(value) != baseline["baseline_json"]
                or origin["import_id"] != baseline["import_id"] or origin["edition_id"] != edition["edition_id"]
                or value["revisionId"] != revision["revision_id"] or value["inputId"] != revision["input_id"]
                or value["videoPartId"] != edition["video_part_id"]
                or hashlib.sha256(value["content"]["markdown"].encode("utf-8")).hexdigest() != value["bodySha256"]):
            raise ValueError("provenance: imported edition baseline mismatch")
        body = hashlib.sha256(edition["content"]["markdown"].encode("utf-8")).hexdigest()
        if origin["expected_markdown_sha256"] != body or origin["relation"] != ("preserved" if body == value["bodySha256"] else "derived"):
            raise ValueError("provenance: imported edition preservation claim mismatch")
        expected = deepcopy(value["content"]["source"])
        if supplement is not None:
            from bili_asr.source_supplements import validate_supplement
            link, stored = supplement["link"], supplement["row"]
            evidence = json.loads(stored["evidence_json"])
            validate_supplement({"supplementId": stored["supplement_id"], "evidence": evidence}, source["metadata"], edition["video_part_id"])
            if link["edition_id"] != edition["edition_id"] or link["supplement_id"] != stored["supplement_id"]:
                raise ValueError("provenance: edition supplement binding mismatch")
            expected["metadata"][evidence["field"]] = evidence["value"]
        if source != expected:
            raise ValueError("provenance: imported source differs from frozen baseline")
        for column, hash_field in (("body_path", "body_sha256"), ("review_path", "review_sha256")):
            if not any(item["relative_path"] == baseline[column] and item["sha256"] == baseline[hash_field] for item in artifacts):
                raise ValueError("provenance: import baseline artifact dependency mismatch")
    else:
        raise ValueError("provenance: unsupported edition origin relationship")
    review_events = [event for event in record["events"] if event["event_type"] in
                     {"created", "edited", "in-review", "changes-requested", "approved", "rejected"}]
    if not review_events:
        raise ValueError("provenance: edition review audit is missing")
    event = review_events[-1]
    if (event["edition_id"] != edition["edition_id"] or event["to_status"] != edition["review_status"]
            or event["content_sha256"] != edition["content_sha256"] or event["review_id"] != edition["review_id"]
            or event["actor"] != edition["review_actor"]):
        raise ValueError("provenance: edition review differs from audit event")
