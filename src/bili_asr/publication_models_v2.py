"""Pure universal frozen content v2 codec; no renderer or input registry."""
from __future__ import annotations

from typing import Any

from bili_asr.platform_identity import ContentRef
from bili_asr.source_identity import source_url
from bili_asr.publication_text import CONTENT_KEYS, normalize_text

INPUT_VERSION = CONTENT_VERSION = 2


def normalize_content_v2(content: dict[str, Any]) -> dict[str, Any]:
    from bili_asr.source_metadata import SourceMetadataSnapshot

    if not isinstance(content, dict) or set(content) != CONTENT_KEYS:
        raise ValueError("publication-content-v2: invalid content fields")
    source = content["source"]
    if not isinstance(source, dict) or set(source) != {"platform", "externalVideoId", "partIndex", "videoPartId", "url", "metadata"}:
        raise ValueError("publication-content-v2: invalid source fields")
    ref = ContentRef(source["platform"], source["externalVideoId"], source["partIndex"])
    part_id = source["videoPartId"]
    if type(part_id) is not int or part_id < 1 or source["url"] != source_url(ref):
        raise ValueError("publication-content-v2: invalid source identity")
    metadata = SourceMetadataSnapshot.from_dict(source["metadata"])
    if metadata.ref != ref:
        raise ValueError("publication-content-v2: metadata belongs to a different source")
    tags = content["tags"]
    if not isinstance(tags, list):
        raise ValueError("publication-content-v2: tags must be a list")
    tags = [normalize_text(tag, "tag", nonempty=True, multiline=False) for tag in tags]
    if len(set(tags)) != len(tags):
        raise ValueError("publication-content-v2: duplicate tags")
    return {
        "title": normalize_text(content["title"], "title", nonempty=True, multiline=False),
        "markdown": normalize_text(content["markdown"], "markdown", nonempty=True) + "\n",
        "summary": normalize_text(content["summary"], "summary"), "tags": tags,
        "source": {"platform": ref.platform, "externalVideoId": ref.external_video_id,
                   "partIndex": ref.part_index, "videoPartId": part_id, "url": source_url(ref),
                   "metadata": metadata.to_dict()},
        "attribution": normalize_text(content["attribution"], "attribution", nonempty=True),
        "editorNote": normalize_text(content["editorNote"], "editorNote"),
    }


def content_from_ai_v2(prepared: dict, markdown_text: str) -> dict:
    from bili_asr.source_metadata import SourceMetadataSnapshot
    snapshot = prepared["snapshot"]
    metadata = SourceMetadataSnapshot.from_dict(snapshot["metadata"]["source_metadata"])
    ref = metadata.ref
    return normalize_content_v2({
        "title": metadata.title, "markdown": markdown_text, "summary": "", "tags": [],
        "source": {"platform": ref.platform, "externalVideoId": ref.external_video_id,
                   "partIndex": ref.part_index, "videoPartId": snapshot["video_part_id"],
                   "url": source_url(ref), "metadata": metadata.to_dict()},
        "attribution": "Prepared from the source transcript with AI assistance.", "editorNote": "",
    })
