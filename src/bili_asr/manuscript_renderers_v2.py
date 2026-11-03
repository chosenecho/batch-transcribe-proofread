"""Immutable multilingual v2 byte renderers, independent of input preparation."""
from __future__ import annotations

import re

from bili_asr.canonical_json import canonical
from bili_asr.source_identity import source_url
from bili_asr.publication_models_v2 import normalize_content_v2

AI_TEMPLATE_VERSION = "ai-draft-v2"
PUBLISH_TEMPLATE_VERSION = "publish-v2"


def _literal(text: str) -> str:
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#+.!|~-])", r"\\\1", escaped)


def render_publish_v2(content: dict) -> bytes:
    content = normalize_content_v2(content)
    source = content["source"]
    parts = [f"# {_literal(content['title'])}"]
    if content["summary"]:
        parts.append(content["summary"])
    parts += [content["markdown"].rstrip("\n"),
              f"Source: [{_literal(source['platform'])} / {_literal(source['externalVideoId'])} / P{source['partIndex'] + 1}]({source['url']})"]
    published = source["metadata"]["sourcePublishedAt"]
    parts.append(f"Source published at: {published if published is not None else 'unknown'}")
    for field in ("attribution", "editorNote"):
        if content[field]:
            parts.append(content[field])
    if content["tags"]:
        parts.append("Tags: " + ", ".join(_literal(tag) for tag in content["tags"]))
    return ("\n\n".join(parts) + "\n").encode("utf-8")


def render_ai_v2(metadata: dict, prepared: dict, blocks: list[dict], revision_id: str) -> dict[str, str]:
    from bili_asr.source_metadata import SourceMetadataSnapshot
    source = SourceMetadataSnapshot.from_dict(metadata["source_metadata"])
    url = source_url(source.ref)
    draft = "\n\n".join(_literal(block["text"]) for block in blocks) + "\n"
    snapshot = prepared["snapshot"]
    reference = snapshot["reference"]
    review = [f"# {_literal(source.title)}: verification reference", "", "AI draft; human review required.", "",
              f"Revision: `{revision_id}`", "", f"Input: `{prepared['input_id']}`", "",
              f"Model: `{_literal(snapshot['config']['model'])}`; template: `{AI_TEMPLATE_VERSION}`", "",
              f"Base transcript: `{snapshot['base']['transcript_id']}`; digest: `{snapshot['base']['content_sha256']}`", "",
              "Reference transcript: " + ("none" if reference is None else f"`{reference['transcript_id']}`; digest: `{reference['content_sha256']}`"), "",
              f"Prompt digest: `{snapshot['prompt_sha256']}`; source rule: `{snapshot['source_rule_version']}`", "",
              f"Configuration: `{_literal(canonical(snapshot['config']))}`", "",
              f"Source: [{_literal(source.ref.platform)} / {_literal(source.ref.external_video_id)}]({url})", "",
              f"Source published at: {source.to_dict()['sourcePublishedAt'] or 'unknown'}", ""]
    for index, block in enumerate(blocks, 1):
        separator = "&" if "?" in url else "?"
        review.extend([f"## Paragraph {index}: {block['start_ms']}–{block['end_ms']} ms", "",
                       f"[Watch]({url}{separator}t={block['start_ms'] // 1000})", "",
                       "Segments: " + ", ".join(f"`{identity}`" for identity in block["segment_ids"]), "",
                       f"Original: {_literal(block['original_text'])}", "", f"Edited: {_literal(block['text'])}", ""])
        review.extend(f"- {_literal(issue['note'])}; {_literal(issue['candidate'])}; "
                      + ", ".join(issue["evidence_refs"]) for issue in block["issues"])
        if block["issues"]:
            review.append("")
    if snapshot["unassigned_reference_ids"]:
        review.extend(["## Unassigned reference segments", "",
                       "These captions have no overlapping editable segment; verify them independently.", ""])
        ids = set(snapshot["unassigned_reference_ids"])
        for segment in reference["segments"]:
            if segment["segment_id"] in ids:
                review.extend([f"- `{segment['segment_id']}` ({segment['start_ms']}–{segment['end_ms']} ms): {_literal(segment['text'])}", ""])
    return {"ai-draft.md": draft, "review.md": "\n".join(review).rstrip() + "\n"}
