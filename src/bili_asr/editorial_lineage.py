"""Private source/draft evidence; structural IDs never certify semantic support."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy

from bili_asr.canonical_json import digest

SCHEMA = "editorial-source-lineage/v1"


def lineage_summary(report: dict) -> dict:
    """Persist bounded status and identity; full text is reconstructed on export."""
    return {key: report[key] for key in ("schema", "input_id", "revision_id", "structure_status", "semantic_status",
                                         "review_required", "human_review_status")} | {
        "report_sha256": digest(report), "paragraph_count": len(report["paragraphs"])}


def source_lineage(prepared: dict, blocks: list[dict], *, revision_id: str | None = None) -> dict:
    """Bind an explicit unverified comparison to frozen sources and output.

    Neighbouring source segments and paragraphs are comparison candidates,
    including across chunk boundaries. Their presence does not permit using
    them as the paragraph's source. Exact repeated drafts are review signals,
    because emphasis and literal quotations can legitimately repeat.
    """
    snapshot = prepared["snapshot"]
    if digest(snapshot) != prepared["input_id"]:
        raise ValueError("source lineage input identity mismatch")
    if revision_id is not None and digest({"input_id": prepared["input_id"], "blocks": blocks}) != revision_id:
        raise ValueError("source lineage revision identity mismatch")
    from bili_asr.editorial_budget import require_frozen_plan
    require_frozen_plan(prepared)
    source = snapshot["base"]["segments"]
    expected = [segment["segment_id"] for segment in source]
    received = [segment_id for block in blocks for segment_id in block["segment_ids"]]
    if not blocks or received != expected:
        raise ValueError("source lineage requires exact ordered structural coverage")
    chunk_coverage = [segment["segment_id"] for chunk in prepared["chunks"] for segment in chunk["editable_segments"]]
    if chunk_coverage != expected:
        raise ValueError("source lineage chunk coverage differs from the frozen source")
    chunk_for = {segment["segment_id"]: chunk["chunk_id"]
                 for chunk in prepared["chunks"] for segment in chunk["editable_segments"]}
    drafts = defaultdict(list)
    for index, block in enumerate(blocks):
        drafts[digest(block["text"])].append(index)
    entries, cursor = [], 0
    for index, block in enumerate(blocks):
        end = cursor + len(block["segment_ids"])
        declared = source[cursor:end]
        if (block["original_text"] != "".join(segment["text"] for segment in declared)
                or block["start_ms"] != declared[0]["start_ms"]
                or block["end_ms"] != max(segment["end_ms"] for segment in declared)):
            raise ValueError("source lineage block differs from its declared sources")
        neighbours = [other for other in (index - 1, index + 1) if 0 <= other < len(blocks)]
        # A bounded source window makes adjacent borrowing visible even when
        # readonly request context is shorter; it is never support evidence.
        context = source[max(0, cursor - 8):cursor] + source[end:end + 8]
        own_chunks = sorted({chunk_for[segment_id] for segment_id in block["segment_ids"]})
        cross_chunk = any(chunk_for[segment["segment_id"]] not in own_chunks for segment in context)
        repeated = drafts[digest(block["text"])]
        entries.append({
            "paragraph_index": index, "chunk_ids": own_chunks,
            "declared_source_range": {"segment_ids": list(block["segment_ids"]),
                "start_ms": block["start_ms"], "end_ms": block["end_ms"],
                "semantic_support": "unverified"},
            "source_segments": deepcopy(declared), "original_text": block["original_text"],
            "draft_text": block["text"], "source_text_sha256": digest(block["original_text"]),
            "draft_text_sha256": digest(block["text"]),
            "comparison_only_adjacent_source_segments": deepcopy(context),
            "comparison_only_adjacent_paragraph_indices": neighbours,
            "same_draft_group": digest(block["text"]) if len(repeated) > 1 else None,
            "cross_chunk_comparison": cross_chunk,
            "semantic_status": "unverified", "review_required": True,
        })
        cursor = end
    return {"schema": SCHEMA, "input_id": prepared["input_id"], "revision_id": revision_id,
            "snapshot_sha256": digest(snapshot), "blocks_sha256": digest(blocks),
            "structure_status": "passed", "semantic_status": "unverified",
            "review_required": True, "reviewer_kind": None, "reviewer": None,
            "human_review_status": "not_reviewed",
            "time_range_basis": "declared_sources_not_verified_semantic_support",
            "repeated_draft_groups": {sha256: indices for sha256, indices in drafts.items() if len(indices) > 1},
            "paragraphs": entries}
