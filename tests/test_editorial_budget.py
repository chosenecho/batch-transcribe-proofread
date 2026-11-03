"""Issues #411/#412: token units, bounded plans and immutable prior inputs."""
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from bili_asr import cli
from bili_asr.deepseek import request_body
from bili_asr.editorial import EditorialConfig, canonical, digest, prepare_input
from bili_asr.editorial_budget import (
    BudgetExceeded,
    plan_report,
    replan,
    require_frozen_plan,
)
from bili_asr.editorial_input_v2 import prepare_input_v2
from bili_asr.editorial_recovery import recovery_request
from bili_asr.platform_identity import ContentRef
from bili_asr.source_metadata import SourceMetadataSnapshot
from bili_asr.storage import open_database
from bili_asr.storage.editorial import EditorialRepository
from bili_asr.workflow_errors import JobExecutionError
from tests.test_ai_editorial import insert_record, record
from tests.test_workflow_control_plane import _seed_part


def multilingual(base, reference, config):
    metadata = SourceMetadataSnapshot(ContentRef("youtube", "abcdef12345"), "fixture", None, None, 1, 2)
    return prepare_input_v2(base, reference, config, source_metadata=metadata)


@pytest.mark.parametrize("text", ["中" * 8000, "english words " * 600, "中English混合 " * 700], ids=("chinese", "english", "mixed"))
@pytest.mark.parametrize("prepare", [prepare_input, multilingual])
def test_new_planners_use_one_token_unit_for_complete_serialized_requests(text, prepare):
    config = EditorialConfig(max_input_tokens=20_000, max_chunk_chars=50_000)
    prepared = prepare(record((text,)), None, config)
    assert len(prepared["chunks"]) == 1
    chunk = prepared["chunks"][0]
    budget = chunk["budget_estimate"]
    wire = canonical(request_body(chunk, config, system_prompt=prepared["snapshot"]["system_prompt"]))
    assert budget["request_bytes"] == len(wire.encode("utf-8")) + 256
    assert budget["request_tokens"] == (budget["request_bytes"] + 2) // 3
    assert budget["request_tokens"] <= 20_000
    assert budget["output_reserve_tokens"] == budget["estimated_output_bytes"]


def test_budget_boundary_is_inclusive_and_one_token_less_is_explicit_failure():
    source = record(("中文内容" * 120,))
    prepared = prepare_input(source, None, EditorialConfig())
    limit = prepared["chunks"][0]["budget_estimate"]["request_tokens"]
    assert len(prepare_input(source, None, EditorialConfig(max_input_tokens=limit))["chunks"]) == 1
    with pytest.raises(BudgetExceeded) as failure:
        prepare_input(source, None, EditorialConfig(max_input_tokens=limit - 1))
    assert failure.value.safe_details["limits"] == ["input_tokens"]


def test_reference_and_readonly_context_are_budgeted_before_calls():
    source = record(("one", "context " * 1500, "three"))
    reference = record(("reference " * 4000,), transcript_id=2, source="subtitle-cc")
    config = EditorialConfig(max_input_tokens=9000, max_chunk_segments=1, context_segments=2)
    with pytest.raises(BudgetExceeded, match="input_tokens"):
        multilingual(source, reference, config)
    with pytest.raises(BudgetExceeded, match="input_tokens"):
        prepare_input(source, None, replace(config, max_input_tokens=3000))


def test_output_reserve_and_single_long_segment_stay_guarded():
    with pytest.raises(BudgetExceeded, match="output_reserve_tokens"):
        prepare_input(record(("中" * 4000,)), None, EditorialConfig(max_output_tokens=8192, max_chunk_chars=50_000))
    with pytest.raises(BudgetExceeded, match="editable_characters"):
        prepare_input(record(("long",)), None, EditorialConfig(max_chunk_chars=3))


def test_recovery_feedback_is_measured_in_same_wire_token_unit_before_a_reservation():
    prepared = prepare_input(record(), None, EditorialConfig())
    chunk = prepared["chunks"][0]
    original = request_body(chunk, EditorialConfig())
    config = EditorialConfig(max_input_tokens=chunk["budget_estimate"]["request_tokens"])
    assert recovery_request(original, None, config)[0] == original
    previous = {"call_id": "prior-failed", "stored_request": {"request": original, "recovery": {"regeneration": 0},
        "validation_diagnostic": {"reason": "cross_paragraph_reference", "details": "bounded feedback"}}}
    with pytest.raises(JobExecutionError) as error:
        recovery_request(original, previous, config)
    assert error.value.error_code == "editorial_budget_exceeded"
    assert error.value.safe_details["reason_code"] == "serialized_request_tokens_exceeded"
    # Historical replay keeps the frozen policy even if the old heuristic
    # planned the same request above the newly introduced admission ceiling.
    assert len(recovery_request(original, previous, replace(config, budget_policy="legacy-v1"))[0]["messages"]) == 3


@pytest.mark.parametrize("mutation", ["omit_chunk", "changed_text", "missing_estimate", "reorder_chunk", "missing_plan"])
def test_new_frozen_boundaries_and_chunk_caches_cannot_silently_change(mutation):
    prepared = prepare_input(record(tuple(f"具体内容{i}" for i in range(600))), None, EditorialConfig.preset("baseline-v1"))
    changed = deepcopy(prepared)
    if mutation == "omit_chunk":
        changed["chunks"].pop()
    elif mutation == "changed_text":
        changed["chunks"][0]["editable_segments"][0]["text"] = "mutable corruption"
    elif mutation == "missing_estimate":
        changed["chunks"][0].pop("budget_estimate")
    elif mutation == "reorder_chunk":
        changed["chunks"].reverse()
    else:
        changed["snapshot"].pop("budget_plan")
    with pytest.raises(ValueError, match="frozen editorial budget plan"):
        require_frozen_plan(changed)
    require_frozen_plan(prepared)


def test_600_short_segments_have_explainable_explicit_baseline_and_large_default():
    original = prepare_input(record(tuple(f"具体内容{i}" for i in range(600))), None, EditorialConfig.preset("baseline-v1"))
    before = canonical(original)
    large = replan(original, EditorialConfig.preset("large-v1"))
    baseline_report, large_report = plan_report(original), plan_report(large)
    assert baseline_report["planned_calls"] == 3 and large_report["planned_calls"] == 1
    assert baseline_report["chunks"][0]["capacity_limits"] == ["editable_segments"]
    assert large_report["chunks"][0]["capacity_limits"] == ["end_of_input"]
    assert large_report["budget_plan"]["provider_capability"]["status"] == "config_only"
    assert large_report["physical_calls_max"] == 3
    assert canonical(original) == before and original["input_id"] != large["input_id"]
    assert [s["segment_id"] for c in large["chunks"] for s in c["editable_segments"]] == [
        s["segment_id"] for s in original["snapshot"]["base"]["segments"]]


def test_planning_uses_logical_boundary_and_overlapping_readonly_context():
    source = record(("first", "second", "third", "sentence ends.", "next", "sixth", "seventh"))
    prepared = prepare_input(source, None, EditorialConfig(max_chunk_segments=5))
    plan = prepared["snapshot"]["budget_plan"]
    assert plan["boundaries"] == [0, 4, 7] and plan["cut_reasons"][0]["logical_boundary"]
    assert prepared["chunks"][0]["readonly_context"][-1]["segment_id"] == "t1:s5"
    assert prepared["chunks"][1]["readonly_context"][0]["segment_id"] == "t1:s2"
    assert prepared["input_id"] == digest(prepared["snapshot"])


def test_historical_input_does_not_change_on_replanning_or_readonly_comparison(tmp_path, capsys):
    connection = open_database(tmp_path)
    try:
        _seed_part(connection)
        insert_record(connection, record(tuple(f"具体内容{i}" for i in range(600))))
        repository = EditorialRepository(connection)
        legacy = repository.prepare(1, None, EditorialConfig.preset("baseline-v1", budget_policy="legacy-v1"))
        # Reproduce a historical config with no newly installed policy fields.
        legacy["snapshot"]["config"].pop("budget_policy")
        legacy["snapshot"]["config"].pop("chunk_preset")
        legacy["input_id"] = digest(legacy["snapshot"])
        for chunk in legacy["chunks"]:
            interval = chunk["chunk_id"].split(":")[1]
            chunk["chunk_id"] = f"{legacy['input_id'][:16]}:{interval}"
        with connection:
            repository.store_input(legacy)
        before = [tuple(row) for row in connection.execute("SELECT * FROM editorial_inputs")]
        assert repository.build_input(1, None, EditorialConfig.preset("baseline-v1"), reuse_existing=True)["input_id"] == legacy["input_id"]
        assert EditorialConfig.from_snapshot(legacy["snapshot"]["config"]).budget_policy == "legacy-v1"
        assert cli.main(["editorial", "plan", "--archive-root", str(tmp_path), "--input-id", legacy["input_id"]]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["physical_calls"] == 0 and [p["planned_calls"] for p in result["plans"]] == [3, 1]
        assert before == [tuple(row) for row in connection.execute("SELECT * FROM editorial_inputs")]
    finally:
        connection.close()


@pytest.mark.parametrize("config", [{"budget_policy": "unknown"}, {"chunk_preset": "unknown"}])
def test_unknown_planning_policy_fails_explicitly(config):
    with pytest.raises(ValueError, match="unsupported editorial"):
        EditorialConfig(**config)


@pytest.mark.parametrize("prepare", [prepare_input, multilingual])
def test_new_default_uses_large_chunks_while_explicit_baseline_remains_available(prepare):
    source = record(tuple(f"具体内容{i}" for i in range(600)))
    default = prepare(source, None, EditorialConfig())
    baseline = prepare(source, None, EditorialConfig.preset("baseline-v1"))
    assert default["snapshot"]["config"]["chunk_preset"] == "large-v1"
    assert default["snapshot"]["config"]["max_chunk_segments"] == 2048
    assert default["snapshot"]["config"]["max_chunk_chars"] == 128_000
    assert len(default["chunks"]) == 1 and len(baseline["chunks"]) == 3
    require_frozen_plan(default)
    assert [s["segment_id"] for c in default["chunks"] for s in c["editable_segments"]] == [s["segment_id"] for s in default["snapshot"]["base"]["segments"]]


def test_cli_large_default_baseline_opt_out_and_explicit_limits():
    from bili_asr.cli.workflow import _editorial_config
    parser = cli.build_parser()
    arguments = ["workflow", "proofread", "--base-transcript-id", "1"]
    default = _editorial_config(parser.parse_args(arguments))
    assert default == EditorialConfig()
    baseline = _editorial_config(parser.parse_args(arguments + ["--chunk-preset", "baseline-v1"]))
    assert baseline == EditorialConfig.preset("baseline-v1")
    custom = _editorial_config(parser.parse_args(arguments + ["--max-chunk-segments", "40", "--max-chunk-chars", "4000"]))
    assert custom.chunk_preset == "large-v1" and custom.max_chunk_segments == 40 and custom.max_chunk_chars == 4000


def test_missing_historical_scheduling_fields_keep_baseline_guards():
    saved = EditorialConfig.preset("baseline-v1").to_dict()
    for name in ("chunk_preset", "max_chunk_segments", "max_chunk_chars"):
        saved.pop(name)
    before = canonical(saved)
    restored = EditorialConfig.from_snapshot(saved)
    assert restored.chunk_preset == "baseline-v1"
    assert restored.max_chunk_segments == 256 and restored.max_chunk_chars == 20_000
    assert canonical(saved) == before
    assert EditorialConfig.from_snapshot(EditorialConfig().to_dict()) == EditorialConfig()
