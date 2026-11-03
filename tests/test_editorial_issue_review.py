"""Issue annotations bind exact original text without rewriting paid JSON."""
from copy import deepcopy

import pytest

from bili_asr.canonical_json import digest
from bili_asr.editorial import EditorialConfig, prepare_input, validate_revision
from bili_asr.editorial_issue_review import annotate_issues, issue_review
from tests.test_ai_editorial import no_changes, record


def sample():
    prepared = prepare_input(record(("设备是限号机。", "另一个段落。")), None, EditorialConfig())
    chunk = prepared["chunks"][0]
    response = no_changes(chunk)
    response["paragraphs"][0]["issues"] = [
        {"note": "设备名待核实", "candidate": "线号机", "evidence_refs": ["t1:s0"]}]
    blocks = validate_revision(chunk, response)
    revision = digest({"input_id": prepared["input_id"], "blocks": blocks})
    return prepared, blocks, revision


def annotation(prepared, blocks, revision):
    entry = issue_review(prepared, blocks, revision_id=revision)["issues"][0]
    return {"issue_id": entry["issue_id"], "issue_type": "name", "severity": "medium", "status": "open",
            "source_spans": [{"segment_id": "t1:s0", "start_char": 3, "end_char": 6, "quote": "限号机"}],
            "reviewer_kind": "agent", "reviewer": "test-agent", "reviewed_at": "2026-10-14T12:00:00+00:00",
            "note": "仅定位文字，尚未听音频。"}


def test_exact_source_location_and_attribution_preserve_model_issue_and_identities():
    prepared, blocks, revision = sample()
    frozen = deepcopy((prepared, blocks))
    empty = issue_review(prepared, blocks, revision_id=revision)["issues"][0]
    assert (empty["issue_type"], empty["severity"], empty["span_status"], empty["annotation"]) == (
        "unclassified", "unassessed", "unlocated", None)
    result = annotate_issues(prepared, blocks, revision_id=revision,
                             annotations=[annotation(prepared, blocks, revision)])
    entry = result["issues"][0]
    assert entry["model_issue"] == blocks[0]["issues"][0]
    assert entry["span_status"] == "located" and entry["status"] == "open"
    assert entry["annotation"]["reviewer_kind"] == "agent"
    assert (prepared, blocks) == frozen


@pytest.mark.parametrize("damage", ["unknown-id", "duplicate", "foreign-span", "wrong-quote", "wrong-offset",
                                    "bool-offset", "untyped-id", "untyped-class", "naive-time", "missing-field"])
def test_invalid_or_unattributed_review_annotations_are_rejected(damage):
    prepared, blocks, revision = sample()
    item = annotation(prepared, blocks, revision)
    annotations = [item]
    if damage == "unknown-id": item["issue_id"] = "a" * 64
    elif damage == "duplicate": annotations.append(deepcopy(item))
    elif damage == "foreign-span": item["source_spans"] = [
        {"segment_id": "t1:s1", "start_char": 0, "end_char": 2, "quote": "另一"}]
    elif damage == "wrong-quote": item["source_spans"][0]["quote"] = "线号机"
    elif damage == "wrong-offset": item["source_spans"][0]["end_char"] = 999
    elif damage == "bool-offset": item["source_spans"][0]["start_char"] = True
    elif damage == "untyped-id": item["issue_id"] = []
    elif damage == "untyped-class": item["issue_type"] = []
    elif damage == "naive-time": item["reviewed_at"] = "2026-10-14T12:00:00"
    else: item.pop("reviewer")
    with pytest.raises(ValueError):
        annotate_issues(prepared, blocks, revision_id=revision, annotations=annotations)
