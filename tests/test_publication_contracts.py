"""Storage and applications share one pure frozen publication identity contract."""
from __future__ import annotations

from copy import deepcopy
import hashlib

import pytest

from bili_asr.canonical_json import canonical, digest
from bili_asr.editorial import canonical as old_canonical, digest as old_digest
from bili_asr.publication_content import normalize_content, render_publication
from bili_asr.publication_identity import validate_release_identity
from bili_asr.sources.bilibili_identity import legacy_bilibili_source_ref


def _approved_release():
    content = normalize_content({
        "title": "标题", "markdown": "正文", "summary": "", "tags": ["标签"],
        "source": {"bvid": "BVtest", "pageIndex": 0, "videoPartId": 1,
                   "url": "https://www.bilibili.com/video/BVtest/?p=1"},
        "attribution": "原视频转录", "editorNote": "",
    })
    content_sha = digest(content)
    release_id = digest({"edition_id": "edition", "content_sha256": content_sha, "template_version": "publish-v1"})
    edition = {"edition_id": "edition", "video_part_id": 1, "review_id": "review", "review_status": "approved",
               "content": content, "content_sha256": content_sha, "current_release_id": release_id}
    release = {"release_id": release_id, "edition_id": "edition", "video_part_id": 1, "review_id": "review",
               "content_sha256": content_sha, "template_version": "publish-v1", "status": "published",
               "relative_path": f"publications/part-1/{release_id}/publish-v1/publish.md",
               "artifact_sha256": hashlib.sha256(render_publication(content)).hexdigest(), "published_by": "publisher"}
    event = {"event_type": "published", "from_status": None, "to_status": "published", "video_part_id": 1,
             "edition_id": "edition", "review_id": "review", "content_sha256": content_sha, "actor": "publisher"}
    return release, edition, [event]


def test_public_contract_reuses_frozen_canonical_json_and_bilibili_identity_without_mutation():
    _, edition, _ = _approved_release()
    source = edition["content"]["source"]
    before = deepcopy(source)
    ref = legacy_bilibili_source_ref(source)
    assert ref.platform == "bilibili" and ref.external_video_id == "BVtest" and ref.part_index == 0
    assert source == before
    assert normalize_content(edition["content"]) == edition["content"]
    assert canonical(edition["content"]) == old_canonical(edition["content"])
    assert digest(edition["content"]) == old_digest(edition["content"])
    with pytest.raises(ValueError):
        canonical({"value": float("nan")})


def test_pure_release_identity_accepts_exact_approved_identity_and_audit_sequence():
    release, edition, events = _approved_release()
    validate_release_identity(release, edition, iter(events))
    release["status"] = "withdrawn"
    edition["current_release_id"] = None
    events.append({**events[0], "event_type": "withdrawn", "from_status": "published", "to_status": "withdrawn", "actor": "reviewer"})
    validate_release_identity(release, edition, events)


@pytest.mark.parametrize("field,value", [
    ("release_id", "tampered"), ("review_id", "other"), ("video_part_id", 2),
    ("content_sha256", "0" * 64), ("relative_path", "publications/other/publish.md"),
    ("artifact_sha256", "0" * 64), ("published_by", "other"),
])
def test_pure_release_identity_rejects_tampered_content_approval_path_and_actor(field, value):
    release, edition, events = _approved_release()
    release[field] = value
    with pytest.raises(ValueError, match="publication-integrity"):
        validate_release_identity(release, edition, events)


def test_pure_release_identity_refuses_missing_events_and_disagreed_head():
    release, edition, events = _approved_release()
    with pytest.raises(ValueError, match="audit events"):
        validate_release_identity(release, edition, [])
    edition["current_release_id"] = None
    with pytest.raises(ValueError, match="effective head"):
        validate_release_identity(release, edition, events)
