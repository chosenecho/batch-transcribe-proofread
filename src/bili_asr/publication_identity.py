"""Pure release identity and audit transition validation shared by all readers."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from bili_asr.canonical_json import digest
from bili_asr.manuscript_templates import PUBLISH_RENDERERS, renderer_for


def validate_release_identity(release: Mapping[str, Any], edition: Mapping[str, Any],
                              events: Iterable[Mapping[str, Any]]) -> None:
    version = release["template_version"]
    render = renderer_for(PUBLISH_RENDERERS, version)
    expected_id = digest({"edition_id": edition["edition_id"], "content_sha256": edition["content_sha256"],
                          "template_version": version})
    expected_path = f"publications/part-{edition['video_part_id']}/{expected_id}/{version}/publish.md"
    if (release["release_id"] != expected_id or release["video_part_id"] != edition["video_part_id"]
            or release["review_id"] != edition["review_id"] or edition["review_status"] != "approved"
            or release["content_sha256"] != edition["content_sha256"]
            or release["relative_path"] != expected_path
            or release["artifact_sha256"] != hashlib.sha256(render(edition["content"])).hexdigest()):
        raise ValueError("publication-integrity: release is not bound to the exact approved edition")
    if (release["status"] == "published") != (edition["current_release_id"] == release["release_id"]):
        raise ValueError("publication-integrity: release state disagrees with effective head")
    previous = None
    transitions = {None: {"published"}, "published": {"superseded", "withdrawn"},
                   "superseded": {"withdrawn"}, "withdrawn": set()}
    for event in events:
        status = event["event_type"]
        if (status not in transitions.get(previous, set()) or event["from_status"] != previous
                or event["to_status"] != status or event["video_part_id"] != edition["video_part_id"]
                or event["edition_id"] != edition["edition_id"] or event["review_id"] != edition["review_id"]
                or event["content_sha256"] != edition["content_sha256"]
                or (status == "published" and event["actor"] != release["published_by"])):
            raise ValueError("publication-integrity: release audit event identity or transition mismatch")
        previous = status
    if previous != release["status"]:
        raise ValueError("publication-integrity: release state disagrees with audit events")
    return None

