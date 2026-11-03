"""Immutable v1 byte renderers; preserve algorithms for historical artifacts."""
from __future__ import annotations

import re
from typing import Any


def _md(text: str) -> str:
    # Treat transcript/model text as literal Markdown text, not HTML or links.
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#+.!|~-])", r"\\\1", text)


def _time(ms: int) -> str:
    seconds = ms // 1000
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def render_ai_v1(metadata: dict[str, Any], prepared: dict[str, Any],
                     blocks: list[dict[str, Any]], revision_id: str) -> dict[str, str]:
    """Render the AI draft and its fixed verification reference as a pair."""
    snapshot = prepared["snapshot"]
    title, bvid, page = _md(metadata["title"]), metadata["bvid"], metadata["page_index"] + 1
    base = f"https://www.bilibili.com/video/{bvid}/?p={page}"
    # No video title, timestamps, headings, footnotes or audit metadata in body.
    draft = "\n\n".join(_md(b["text"]) for b in blocks) + "\n"
    config = snapshot["config"]
    review = [f"# {title}：校验参照稿件", "", "AI 合成稿件，未经人工复核。正文已应用语句整理；疑点在此记录。", "",
              f"修订：`{revision_id}`", "", f"输入快照：`{prepared['input_id']}`", "",
              f"模型：`{config['model']}`；思考：`{config['reasoning_effort']}`；top_p：`{config['top_p']}`", "",
              f"规则：`{config['rule_version']}`；模板：`ai-draft-v1`", "",
              f"基础转录：`{snapshot['base']['transcript_id']}`；参考转录："
              f"`{snapshot['reference']['transcript_id'] if snapshot['reference'] else '无'}`", ""]
    for index, block in enumerate(blocks, 1):
        link = f"{base}&t={block['start_ms'] // 1000}"
        review.extend([f"## 段落 {index}：{_time(block['start_ms'])} — {_time(block['end_ms'])}", "",
                       "来源：" + ", ".join(f"`{s}`" for s in block["segment_ids"]) + f"；[回看]({link})", "",
                       f"原文：{_md(block['original_text'])}", "", f"整理稿：{_md(block['text'])}", ""])
        for issue in block["issues"]:
            review.append(f"- 疑点：{_md(issue['note'])}；候选：{_md(issue['candidate'])}；"
                          f"依据：{', '.join(issue['evidence_refs']) or '无'}")
        if block["issues"]:
            review.append("")
    unassigned = snapshot.get("unassigned_reference_ids", [])
    if unassigned:
        review.extend(["## 未匹配的参考字幕", "", *[f"- `{r}`" for r in unassigned], ""])
    return {"ai-draft.md": draft, "review.md": "\n".join(review).rstrip() + "\n"}


def _markdown_inline(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()<>#+.!|~-])", r"\\\1", text)


def render_publish_v1(content: dict[str, Any]) -> bytes:
    """Render only the approved frozen reader object with a fixed template."""
    parts = [f"# {_markdown_inline(content['title'])}"]
    if content["summary"]:
        parts.append(content["summary"])
    parts.append(content["markdown"].rstrip("\n"))
    source = content["source"]
    parts.append(f"\u6765\u6e90\uff1a[{_markdown_inline(source['bvid'])} / P{source['pageIndex'] + 1}]({source['url']})")
    if content["attribution"]:
        parts.append(content["attribution"])
    if content["editorNote"]:
        parts.append(content["editorNote"])
    if content["tags"]:
        parts.append("\u6807\u7b7e\uff1a" + "\u3001".join(_markdown_inline(tag) for tag in content["tags"]))
    return ("\n\n".join(parts) + "\n").encode("utf-8")
