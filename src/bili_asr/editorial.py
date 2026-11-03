"""Pure input preparation, constrained AI revisions and Markdown rendering.

No database, HTTP client or process-local progress lives in this module.
Token budgeting uses a deterministic UTF-8 byte-to-token estimate; provider
usage remains the source of truth for final cost accounting.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from bili_asr.canonical_json import canonical, digest
from bili_asr.editorial_input_contract import (
    HISTORICAL_SYSTEM_PROMPT,
    RULE_VERSION,
    SYSTEM_PROMPT,
    TOKEN_ESTIMATE_BYTES,
    EditorialConfig,
    estimate_chunk_budget,
    estimate_utf8_tokens,
    local_precheck,
)
from bili_asr.editorial_segments import language_key, segments
from bili_asr.workflow_errors import safe_job_details

__all__ = [
    "ARTIFACT_ROLES",
    "RULE_VERSION",
    "SYSTEM_PROMPT",
    "TEMPLATE_VERSION",
    "TOKEN_ESTIMATE_BYTES",
    "EditorialConfig",
    "RevisionValidationError",
    "canonical",
    "digest",
    "estimate_chunk_budget",
    "estimate_utf8_tokens",
    "language_key",
    "local_precheck",
    "local_revision",
    "prepare_input",
    "render_documents",
    "segments",
    "validate_revision",
]


if TYPE_CHECKING:
    from bili_asr.storage.models import TranscriptRecord


TEMPLATE_VERSION = "ai-draft-v1"
ARTIFACT_ROLES = {"ai-draft.md": "ai-draft", "review.md": "review-reference"}


def local_revision(chunk: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Build a provenance-preserving revision when every change is mechanical.

    Only chunks where every segment has a safe whitespace/punctuation candidate
    are eligible.  Filler words, repetitions, mixed chunks, and untouched
    segments continue through the model so semantic editing is never skipped.
    """
    editable = chunk.get("editable_segments", [])
    checks = {item.get("segment_id"): item for item in chunk.get("local_precheck", [])}
    if not editable or len(checks) != len(editable) or any(not item.get("safe") for item in checks.values()):
        return None
    blocks = []
    for segment in editable:
        check = checks[segment["segment_id"]]
        text = check.get("safe_text")
        if not isinstance(text, str) or not text.strip():
            return None
        blocks.append({
            "segment_ids": [segment["segment_id"]],
            "start_ms": segment["start_ms"],
            "end_ms": segment["end_ms"],
            "original_text": segment["text"],
            "text": text,
            "issues": [],
        })
    return blocks


def prepare_input(base: TranscriptRecord, reference: TranscriptRecord | None,
                  config: EditorialConfig, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Freeze explicit versions and assign each base segment to exactly one chunk."""
    if reference and (base.video_part_id != reference.video_part_id
                      or language_key(base.language) != language_key(reference.language)
                      or reference.source_kind not in {"subtitle-ai", "subtitle-cc"}):
        raise ValueError("reference must be a caption for the same part and language")
    body, refs = segments(base), segments(reference) if reference else []
    if not body:
        raise ValueError("base transcript has no segments")
    prompt = HISTORICAL_SYSTEM_PROMPT if config.budget_policy == "legacy-v1" else SYSTEM_PROMPT
    snapshot = {
        "video_part_id": base.video_part_id,
        "base": {"transcript_id": base.transcript_id, "content_sha256": base.content_sha256,
                 "source_kind": base.source_kind, "language": base.language, "segments": body},
        "reference": None if reference is None else {
            "transcript_id": reference.transcript_id, "content_sha256": reference.content_sha256,
            "source_kind": reference.source_kind, "language": reference.language, "segments": refs},
        "config": config.to_dict(), "system_prompt": prompt, "prompt_sha256": digest(prompt),
    }
    if metadata is not None:
        snapshot["metadata"] = dict(metadata)
    # Match per-segment intervals, not a single broad range across timeline gaps.
    matches = {s["segment_id"]: [r for r in refs if r["start_ms"] < s["end_ms"]
                               and r["end_ms"] > s["start_ms"]] for s in body}
    assigned = {r["segment_id"] for group in matches.values() for r in group}
    snapshot["unassigned_reference_ids"] = [r["segment_id"] for r in refs if r["segment_id"] not in assigned]
    if config.budget_policy != "legacy-v1":
        from bili_asr.editorial_budget import prepare_budgeted_input
        return prepare_budgeted_input(snapshot, config, matches, include_precheck=True)
    input_id = digest(snapshot)

    def make_chunk(start: int, end: int) -> dict[str, Any]:
        context = body[max(0, start - config.context_segments):start] + body[end:end + config.context_segments]
        selected = {r["segment_id"] for s in body[start:end] for r in matches[s["segment_id"]]}
        editable = [dict(s, allowed_issue_refs=[s["segment_id"]] +
                         [r["segment_id"] for r in matches[s["segment_id"]]]) for s in body[start:end]]
        reference = [r for r in refs if r["segment_id"] in selected]
        prechecks = []
        for segment in editable:
            check = local_precheck(segment["text"])
            if check["flags"]:
                prechecks.append(dict(check, segment_id=segment["segment_id"]))
        chunk = {"chunk_id": f"{input_id[:16]}:{start}-{end}",
                 "editable_segments": editable,
                 "reference_segments": reference,
                 "readonly_context": context,
                 "local_precheck": prechecks}
        # Keep these fields as frozen, inspectable planning data.  The request
        # adapter still serializes the complete chunk, while the worker can
        # aggregate actual provider usage against these estimates later.
        chunk["budget_estimate"] = estimate_chunk_budget(chunk, system_prompt=prompt, max_output_tokens=config.max_output_tokens)
        return chunk

    def fits(chunk: dict[str, Any]) -> bool:
        input_limit = min(config.max_input_tokens,
                          config.context_tokens - config.max_output_tokens - config.safety_tokens)
        # Reserve output for reasoning, full paragraphs, IDs and issues.
        # This is conservative, not a guarantee
        # about a model's arbitrary verbosity; length-truncated responses fail.
        # ``request_size`` used to be UTF-8 bytes compared directly against a
        # token budget, causing Chinese text to be split far more often than
        # necessary.  Keep the provider-independent estimate conservative and
        # compare like units instead.
        return (chunk["budget_estimate"]["request_tokens"] <= input_limit
                and chunk["budget_estimate"]["estimated_output_bytes"] <= config.max_output_tokens
                and len(chunk["editable_segments"]) <= config.max_chunk_segments
                and sum(len(s["text"]) for s in chunk["editable_segments"]) <= config.max_chunk_chars)

    chunks, start = [], 0
    while start < len(body):
        low, high, chosen = start + 1, len(body), None
        while low <= high:
            end = (low + high) // 2
            chunk = make_chunk(start, end)
            if fits(chunk):
                chosen, low = chunk, end + 1
            else:
                high = end - 1
        if chosen is None:
            raise ValueError("one segment plus references/context exceeds the configured budget")
        chunks.append(chosen)
        start += len(chosen["editable_segments"])
    return {"input_id": input_id, "snapshot": snapshot, "chunks": chunks}


class RevisionValidationError(ValueError):
    """The model response does not obey the frozen input contract."""

    error_code = "RevisionValidationError"

    def __init__(self, message: str, *, reason_code: str = "invalid_revision", **details: Any):
        self.safe_details = safe_job_details(dict(
            stage="revision_validation", reason_code=reason_code, **details))
        super().__init__(message)


def _fields(value: Any, names: set[str], **details: Any) -> None:
    if not isinstance(value, dict) or set(value) != names:
        raise RevisionValidationError("unknown or missing response fields", reason_code="invalid_fields",
                                      expected_field_count=len(names),
                                      actual_field_count=len(value) if isinstance(value, dict) else None,
                                      **details)


def _string(value: Any, *, maximum: int, empty: bool = True, **details: Any) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise RevisionValidationError("invalid response string", reason_code="invalid_string",
                                      maximum_chars=maximum,
                                      actual_chars=len(value) if isinstance(value, str) else None, **details)
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise RevisionValidationError("control character in response", reason_code="control_character", **details)
    return value


def validate_revision(chunk: dict[str, Any], response: Any) -> list[dict[str, Any]]:
    """Validate ordered source coverage of freely edited reading paragraphs.

    Source coverage is structural provenance, not proof of semantic accuracy.
    Uncertain passages remain reviewable without reverting the whole paragraph.
    """
    _fields(response, {"chunk_id", "paragraphs"}, section="revision")
    if response["chunk_id"] != chunk["chunk_id"]:
        raise RevisionValidationError("wrong chunk identity or paragraphs", reason_code="wrong_chunk_identity")
    if not isinstance(response["paragraphs"], list):
        raise RevisionValidationError("wrong chunk identity or paragraphs", reason_code="invalid_paragraphs")
    source = chunk["editable_segments"]
    expected = [s["segment_id"] for s in source]
    if not response["paragraphs"]:
        raise RevisionValidationError("empty reading paragraphs", reason_code="empty_paragraphs",
                                      expected_segment_count=len(expected), paragraph_count=0)
    cursor, validated = 0, []
    for paragraph_index, paragraph in enumerate(response["paragraphs"]):
        location = {"paragraph_index": paragraph_index}
        _fields(paragraph, {"segment_ids", "text", "issues"}, section="paragraph", **location)
        ids = paragraph["segment_ids"]
        if (not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids)
                or ids != expected[cursor:cursor + len(ids)]):
            raise RevisionValidationError("missing, duplicated, unknown or reordered source segments",
                                          reason_code="source_coverage_order", segment_cursor=cursor,
                                          expected_segment_count=len(expected),
                                          received_segment_count=len(ids) if isinstance(ids, list) else None,
                                          **location)
        originals = source[cursor:cursor + len(ids)]
        original_text = "".join(s["text"] for s in originals)
        corrected = _string(paragraph["text"], maximum=max(4000, len(original_text) * 3), empty=False,
                            field="text", **location).strip()
        if "\n" in corrected or re.match(r"^(?:#{1,6}\s|[-*]\s|\[?\d{2}:\d{2}:\d{2})", corrected):
            raise RevisionValidationError("reading text must be one plain paragraph without headings or timestamps",
                                          reason_code="invalid_paragraph_format", **location)
        if not isinstance(paragraph["issues"], list):
            raise RevisionValidationError("issues must be a list", reason_code="invalid_issues", **location)
        reference_ids = {r["segment_id"] for r in chunk["reference_segments"]
                         if any(r["start_ms"] < s["end_ms"] and r["end_ms"] > s["start_ms"]
                                for s in originals)}
        allowed = reference_ids | set(ids)
        issues = []
        for issue_index, issue in enumerate(paragraph["issues"]):
            issue_location = dict(location, issue_index=issue_index)
            _fields(issue, {"note", "candidate", "evidence_refs"}, section="issue", **issue_location)
            _string(issue["note"], maximum=2000, empty=False, field="note", **issue_location)
            _string(issue["candidate"], maximum=4000, field="candidate", **issue_location)
            refs = issue["evidence_refs"]
            if (not isinstance(refs, list)
                    or any(not isinstance(r, str) or r not in allowed for r in refs)):
                bad = refs if isinstance(refs, list) else []
                # Bounded regeneration accepts only known cross-paragraph
                # IDs. Unknown IDs and readonly context still fail strictly.
                known = {s["segment_id"] for s in source} | {
                    s["segment_id"] for s in chunk["reference_segments"]}
                invalid = [r for r in bad if not isinstance(r, str) or r not in allowed]
                recovery_reason = ("cross_paragraph_reference"
                    if isinstance(refs, list) and invalid and all(isinstance(r, str) and r in known for r in invalid)
                    else "invalid_issue_reference")
                raise RevisionValidationError("unknown or unrelated issue reference", reason_code="invalid_evidence_refs",
                    **issue_location, allowed_count=len(allowed), invalid_count=len(invalid),
                    invalid_ref_hashes=[digest(r) for r in invalid][:8], reason=recovery_reason)
            issues.append(dict(issue))
        validated.append({"segment_ids": list(ids), "start_ms": originals[0]["start_ms"],
                          "end_ms": max(s["end_ms"] for s in originals),
                          "original_text": original_text, "text": corrected, "issues": issues})
        cursor += len(ids)
    if cursor != len(expected):
        raise RevisionValidationError("revision does not cover all source segments", reason_code="incomplete_source_coverage",
                                      expected_segment_count=len(expected), covered_segment_count=cursor)
    return validated


def render_documents(metadata: dict[str, Any], prepared: dict[str, Any],
                     blocks: list[dict[str, Any]], revision_id: str) -> dict[str, str]:
    """Render the current writer template; historical reads dispatch by version."""
    from bili_asr.manuscript_templates import AI_RENDERERS, renderer_for
    return renderer_for(AI_RENDERERS, TEMPLATE_VERSION)(metadata, prepared, blocks, revision_id)
