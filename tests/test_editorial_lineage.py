"""Structural success remains semantically unverified, including neighbouring borrowing."""
import json
from copy import deepcopy

import pytest

from bili_asr.canonical_json import digest
from bili_asr.contracts.json_schema import ContractValidationError, validate_json
from bili_asr.editorial import EditorialConfig, prepare_input, validate_revision
from bili_asr.editorial_input_v2 import prepare_input_v2
from bili_asr.editorial_lineage import lineage_summary, source_lineage
from bili_asr.platform_identity import ContentRef
from bili_asr.source_metadata import SourceMetadataSnapshot
from tests.test_ai_editorial import FakeClient, no_changes, record, runtime

pytest_plugins = ["tests.test_ai_editorial"]


def sample():
    prepared = prepare_input(record(("Declared claim.", "Adjacent evidence.", "Final statement.")), None,
                             EditorialConfig(max_chunk_segments=1, context_segments=1))
    blocks = [block for chunk in prepared["chunks"] for block in validate_revision(chunk, no_changes(chunk))]
    return prepared, blocks


def test_borrowing_adjacent_content_never_becomes_verified_support():
    prepared, blocks = sample()
    blocks[0]["text"] = blocks[1]["text"]
    frozen = deepcopy((prepared, blocks))
    revision = digest({"input_id": prepared["input_id"], "blocks": blocks})
    report = source_lineage(prepared, blocks, revision_id=revision)
    validate_json("editorial-source-lineage/v1", report)
    assert report["structure_status"] == "passed"
    assert report["semantic_status"] == "unverified" and report["review_required"]
    assert report["human_review_status"] == "not_reviewed" and report["reviewer_kind"] is None
    entry = report["paragraphs"][0]
    assert entry["declared_source_range"]["segment_ids"] == ["t1:s0"]
    assert entry["declared_source_range"]["semantic_support"] == "unverified"
    assert entry["cross_chunk_comparison"]
    assert entry["comparison_only_adjacent_paragraph_indices"] == [1]
    assert entry["comparison_only_adjacent_source_segments"][0]["segment_id"] == "t1:s1"
    assert report["repeated_draft_groups"][entry["same_draft_group"]] == [0, 1]
    assert (prepared, blocks) == frozen
    summary = lineage_summary(report)
    assert summary["report_sha256"] == digest(report) and summary["paragraph_count"] == 3
    assert "paragraphs" not in summary and "draft_text" not in json.dumps(summary)


def test_single_chunk_has_no_cross_chunk_signal_and_unchanged_source_is_still_unverified():
    prepared = prepare_input(record(), None, EditorialConfig())
    blocks = validate_revision(prepared["chunks"][0], no_changes(prepared["chunks"][0]))
    report = source_lineage(prepared, blocks)
    assert not any(entry["cross_chunk_comparison"] for entry in report["paragraphs"])
    assert report["repeated_draft_groups"] == {}
    assert all(entry["semantic_status"] == "unverified" for entry in report["paragraphs"])


def test_legacy_chunk_duplicate_cannot_hide_behind_dictionary_keys():
    prepared = prepare_input(record(), None, EditorialConfig(budget_policy="legacy-v1"))
    blocks = validate_revision(prepared["chunks"][0], no_changes(prepared["chunks"][0]))
    prepared["chunks"].append(deepcopy(prepared["chunks"][0]))
    with pytest.raises(ValueError, match="chunk coverage"):
        source_lineage(prepared, blocks)


@pytest.mark.parametrize("universal", [False, True])
def test_new_source_support_prompt_has_new_identity_without_rewriting_legacy_prompt(universal):
    config = EditorialConfig()
    legacy = EditorialConfig(budget_policy="legacy-v1")
    if universal:
        metadata = SourceMetadataSnapshot(ContentRef("youtube", "dQw4w9WgXcQ", 0), "Fixture", "UCfixture", "Author", None, 1)
        old = prepare_input_v2(record(), None, legacy, source_metadata=metadata)
        new = prepare_input_v2(record(), None, config, source_metadata=metadata)
        assert "Every substantive claim" in new["snapshot"]["system_prompt"]
        assert "Every substantive claim" not in old["snapshot"]["system_prompt"]
    else:
        old, new = prepare_input(record(), None, legacy), prepare_input(record(), None, config)
        assert "每项实质信息" in new["snapshot"]["system_prompt"]
        assert "每项实质信息" not in old["snapshot"]["system_prompt"]
    assert new["snapshot"]["prompt_sha256"] != old["snapshot"]["prompt_sha256"]
    assert new["input_id"] != old["input_id"]


@pytest.mark.parametrize("damage", ["input", "revision", "coverage", "chunk-duplicate", "source", "time"])
def test_tampered_source_draft_binding_is_rejected(damage):
    prepared, blocks = sample()
    revision = digest({"input_id": prepared["input_id"], "blocks": blocks})
    if damage == "input":
        prepared["snapshot"]["base"]["segments"][0]["text"] = "tampered"
    elif damage == "revision":
        revision = "a" * 64
    elif damage == "coverage":
        blocks.pop()
        revision = None
    elif damage == "chunk-duplicate":
        prepared["chunks"].append(deepcopy(prepared["chunks"][0]))
    elif damage == "source":
        blocks[0]["original_text"] = "tampered"
        revision = None
    else:
        blocks[0]["start_ms"] = 42
        revision = None
    with pytest.raises(ValueError, match="source lineage|frozen editorial"):
        source_lineage(prepared, blocks, revision_id=revision)


@pytest.mark.parametrize("damage", ["semantic", "paragraph-extra", "source-extra"])
def test_lineage_schema_cannot_silently_claim_pass_or_add_unbound_fields(damage):
    prepared, blocks = sample()
    report = source_lineage(prepared, blocks)
    if damage == "semantic":
        report["semantic_status"] = "pass"
    elif damage == "paragraph-extra":
        report["paragraphs"][0]["unbound"] = "claim"
    else:
        report["paragraphs"][0]["source_segments"][0]["unbound"] = "claim"
    with pytest.raises(ContractValidationError):
        validate_json("editorial-source-lineage/v1", report)


def test_proofread_automatically_records_compact_unverified_lineage(database, tmp_path):
    workflow, repository, _, executor = runtime(database, tmp_path, FakeClient())
    prepared = repository.prepare(1, None, EditorialConfig())
    workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert executor.run().succeeded == 2
    raw = database.execute("SELECT result_json FROM workflow_attempts a JOIN workflow_jobs j USING(job_id) "
                           "WHERE j.kind='proofread' AND a.outcome='succeeded'").fetchone()[0]
    status = json.loads(raw)["source_lineage"]
    assert status["semantic_status"] == "unverified" and status["review_required"]
    assert "paragraphs" not in status
