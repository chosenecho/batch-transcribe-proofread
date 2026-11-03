"""Versioned annotations beside immutable model issues, never added to paid JSON."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from bili_asr.canonical_json import digest
from bili_asr.contracts.json_schema import validate_json
from bili_asr.editorial_lineage import source_lineage

SCHEMA = "editorial-issue-review/v1"
ISSUE_TYPES = frozenset({"unclassified", "recognition", "name", "number", "date_time",
                        "unit", "unintelligible", "source_support", "omission", "fluency", "other"})
SEVERITIES = frozenset({"unassessed", "low", "medium", "high"})
STATUSES = frozenset({"open", "in_review", "resolved", "dismissed"})


def issue_review(prepared: dict, blocks: list[dict], *, revision_id: str) -> dict:
    """Create explicit unclassified review tasks without guessing issue spans."""
    source_lineage(prepared, blocks, revision_id=revision_id)
    entries = []
    for paragraph_index, block in enumerate(blocks):
        for issue_index, issue in enumerate(block["issues"]):
            identity = {"revision_id": revision_id, "paragraph_index": paragraph_index,
                        "issue_index": issue_index, "model_issue": issue}
            entries.append({"issue_id": digest(identity), "paragraph_index": paragraph_index,
                "issue_index": issue_index, "model_issue": deepcopy(issue),
                "declared_segment_ids": list(block["segment_ids"]),
                "issue_type": "unclassified", "severity": "unassessed", "status": "open",
                "source_spans": [], "span_status": "unlocated", "annotation": None})
    report = {"schema": SCHEMA, "input_id": prepared["input_id"], "revision_id": revision_id,
            "blocks_sha256": digest(blocks), "index_basis": "zero_based_unicode_codepoints",
            "issues": entries}
    validate_json(SCHEMA, report)
    return report


def annotate_issues(prepared: dict, blocks: list[dict], *, revision_id: str,
                    annotations: list[dict]) -> dict:
    """Validate separately attributed review work; original model issues stay exact."""
    if not isinstance(annotations, list):
        raise ValueError("issue annotations must be a list")  # noqa: TRY004 -- invalid serialized review document
    report = issue_review(prepared, blocks, revision_id=revision_id)
    entries = {issue["issue_id"]: issue for issue in report["issues"]}
    sources = {segment["segment_id"]: segment for segment in prepared["snapshot"]["base"]["segments"]}
    reference = prepared["snapshot"]["reference"]
    if reference:
        sources.update((s["segment_id"], s) for s in reference["segments"])
    annotated = set()
    for annotation in annotations:
        expected = {"issue_id", "issue_type", "severity", "status", "source_spans",
                    "reviewer_kind", "reviewer", "reviewed_at", "note"}
        if not isinstance(annotation, dict) or set(annotation) != expected:
            raise ValueError("invalid issue annotation fields")
        identity = annotation["issue_id"]
        if not isinstance(identity, str) or identity not in entries or identity in annotated:
            raise ValueError("unknown or repeated issue annotation")
        if any(not isinstance(annotation[name], str) or annotation[name] not in choices
               for name, choices in (("issue_type", ISSUE_TYPES), ("severity", SEVERITIES), ("status", STATUSES))):
            raise ValueError("unsupported issue classification")
        if annotation["reviewer_kind"] not in ("human", "agent"):
            raise ValueError("issue annotation requires an identified reviewer")
        for name in ("reviewer", "reviewed_at", "note"):
            if not isinstance(annotation[name], str) or not annotation[name].strip():
                raise ValueError("issue annotation attribution is missing")
        if datetime.fromisoformat(annotation["reviewed_at"]).utcoffset() is None:
            raise ValueError("issue annotation timestamp requires a timezone")
        spans = annotation["source_spans"]
        if not isinstance(spans, list):
            raise ValueError("invalid issue source spans")  # noqa: TRY004 -- invalid serialized review document
        entry = entries[identity]
        paragraph = blocks[entry["paragraph_index"]]
        allowed = set(paragraph["segment_ids"]) | set(entry["model_issue"]["evidence_refs"])
        for span in spans:
            if (not isinstance(span, dict)
                    or set(span) != {"segment_id", "start_char", "end_char", "quote"}
                    or not isinstance(span["segment_id"], str)
                    or span["segment_id"] not in allowed or span["segment_id"] not in sources):
                raise ValueError("issue span must cite its own paragraph or issue evidence")
            text = sources[span["segment_id"]]["text"]
            start, end = span["start_char"], span["end_char"]
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                    or text[start:end] != span["quote"]):
                raise ValueError("issue span differs from exact source characters")
        entry.update({name: deepcopy(annotation[name])
                      for name in ("issue_type", "severity", "status", "source_spans")})
        entry["span_status"] = "located" if spans else "unlocated"
        entry["annotation"] = {name: annotation[name]
                               for name in ("reviewer_kind", "reviewer", "reviewed_at", "note")}
        annotated.add(identity)
    validate_json(SCHEMA, report)
    return report
