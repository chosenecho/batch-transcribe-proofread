"""A comparison never resets reservations or infers semantic quality from JSON."""
import json
import sqlite3
from copy import deepcopy
from dataclasses import replace

import pytest

from bili_asr import cli
from bili_asr.deepseek import DeepSeekError
from bili_asr.editorial import EditorialConfig, canonical, digest, prepare_input
from bili_asr.services.editorial_comparison import (
    PRESETS,
    RUBRIC,
    compare_presets,
    comparison_manifest,
    comparison_report,
    record_review,
    write_review_packet,
)
from bili_asr.storage import open_database
from bili_asr.storage.editorial import EditorialRepository
from bili_asr.workflow_errors import JobExecutionError
from tests.test_ai_editorial import envelope, insert_record, no_changes, record
from tests.test_workflow_control_plane import _seed_part

ENDPOINT = "https://api.deepseek.com"
MODEL = "test-requested-alias"


class Provider:
    def __init__(self, *, broken=False, interrupted=False, missing_usage=False):
        self.calls = []
        self.broken = broken
        self.interrupted = interrupted
        self.missing_usage = missing_usage

    def complete(self, request, config):
        self.calls.append(deepcopy(request))
        if self.interrupted:
            raise KeyboardInterrupt()
        chunk = json.loads(request["messages"][1]["content"])
        result = no_changes(chunk)
        if self.broken and len(result["paragraphs"]) > 1:
            result["paragraphs"][0]["issues"] = [{"note": "fixture cross paragraph reference", "candidate": "",
                "evidence_refs": result["paragraphs"][1]["segment_ids"]}]
        response = envelope(result)
        response["model"] = "actual-returned-model"
        if not self.missing_usage:
            response["usage"].update(prompt_cache_hit_tokens=20, prompt_cache_miss_tokens=80,
                                     completion_tokens_details={"reasoning_tokens": 10})
        return response

    def close(self):
        pass


def source(count=2):
    return prepare_input(record(tuple(f"冻结内容{i}" for i in range(count))), None, EditorialConfig())


def compare(prepared, ledger, client, **values):
    return compare_presets(prepared, ledger, endpoint=ENDPOINT, model=MODEL,
                           allow_paid_calls=True, client=client, **values)


def test_bounded_comparison_keeps_raw_evidence_and_replay_never_resends(tmp_path):
    prepared = source(600)
    original = canonical(prepared)
    provider = Provider()
    ledger = tmp_path / "comparison.db"
    report = compare(prepared, ledger, provider)
    assert report["physical_calls"] == len(provider.calls) == 4
    assert report["default_preset"] == "large-v1" and report["provider_capability"] == "not_verified"
    assert report["variants"]["baseline-v1"]["plan"]["planned_calls"] == 3
    assert report["variants"]["large-v1"]["plan"]["planned_calls"] == 1
    assert all(v["structure_complete"] and v["semantic_quality"] == "not_reviewed" for v in report["variants"].values())
    large = report["variants"]["large-v1"]
    assert large["usage"]["reasoning_tokens"] == {"known_total": 10, "missing_call_counters": 0}
    assert large["calls"][0]["usage"]["model"] == "actual-returned-model"
    assert large["calls"][0]["requested_model"] == MODEL
    assert large["actual_cost"]["status"] == "unknown"
    assert compare(prepared, ledger, provider) == comparison_report(ledger) == report
    assert len(provider.calls) == 4 and canonical(prepared) == original
    with sqlite3.connect(ledger) as connection:
        for request, response in connection.execute("SELECT request,response FROM calls"):
            assert json.loads(request) in provider.calls
            assert json.loads(response)["model"] == "actual-returned-model"


def test_global_budget_and_terminal_failures_cannot_reset_or_grow(tmp_path):
    ledger = tmp_path / "comparison.db"
    prepared = source()
    provider = Provider(broken=True)
    report = compare(prepared, ledger, provider, maximum=3)
    assert report["physical_calls"] == len(provider.calls) == 3
    assert all(not v["structure_complete"] for v in report["variants"].values())
    assert report["variants"]["baseline-v1"]["retry_calls"] == 1
    assert compare(prepared, ledger, provider, maximum=3)["physical_calls"] == 3
    with pytest.raises(ValueError, match="identity or budget changed"):
        compare(prepared, ledger, provider, maximum=4)
    assert len(provider.calls) == 3


def test_unknown_send_survives_interruption_and_does_not_get_resent(tmp_path):
    prepared = source()
    ledger = tmp_path / "comparison.db"
    interrupted = Provider(interrupted=True)
    with pytest.raises(KeyboardInterrupt):
        compare(prepared, ledger, interrupted, maximum=4)
    first = comparison_report(ledger)
    assert first["physical_calls"] == 1
    assert first["variants"]["baseline-v1"]["calls"][0]["outcome"] == "unknown_send_consumed"
    provider = Provider()
    report = compare(prepared, ledger, provider, maximum=4)
    assert report["physical_calls"] == 2 and len(provider.calls) == 1
    assert not report["variants"]["baseline-v1"]["structure_complete"]
    assert report["variants"]["large-v1"]["structure_complete"]


def test_mutated_later_response_is_rejected_before_any_earlier_new_send(tmp_path):
    prepared = source()
    ledger = tmp_path / "comparison.db"
    provider = Provider()
    compare(prepared, ledger, provider)
    with sqlite3.connect(ledger) as connection:
        connection.execute("UPDATE calls SET response='{}' WHERE ordinal=2")
    with pytest.raises(ValueError, match="outcome changed"):
        compare(prepared, ledger, provider)
    assert len(provider.calls) == 2
    with pytest.raises(ValueError, match="outcome changed"):
        comparison_report(ledger)


def pricing():
    return {"currency": "USD", "source": "operator frozen price evidence", "observed_at": "2026-10-13T12:00:00+08:00",
            "input_cache_hit_per_million": .1, "input_cache_miss_per_million": 1, "output_per_million": 2}


def test_costs_need_explicit_price_evidence_and_missing_usage_remains_unknown(tmp_path):
    prepared = source()
    known = compare(prepared, tmp_path / "known.db", Provider(), pricing=pricing(), max_output_tokens=8192)
    baseline = known["variants"]["baseline-v1"]
    assert baseline["actual_cost"]["amount"] == pytest.approx((20 * .1 + 80 + 80 * 2) / 1_000_000)
    assert baseline["estimated_cost_range"]["minimum"] <= baseline["estimated_cost_range"]["maximum_including_one_retry_per_chunk"]
    missing = compare(prepared, tmp_path / "missing.db", Provider(missing_usage=True), pricing=pricing())
    assert missing["variants"]["baseline-v1"]["actual_cost"]["reason"] == "missing_provider_usage"
    assert missing["variants"]["baseline-v1"]["usage"]["reasoning_tokens"]["missing_call_counters"] == 1


def test_named_semantic_review_is_immutable_and_agent_review_is_distinct(tmp_path):
    ledger = tmp_path / "comparison.db"
    compare(source(), ledger, Provider())
    packet_path = tmp_path / "private-review.json"
    write_review_packet(ledger, packet_path)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert all(value["semantic_status"] == "unverified" and value["review_required"]
               for value in packet["source_lineage"].values())
    review = packet["review_template"]
    with pytest.raises(ValueError, match="named reviewer|name its reviewer"):
        record_review(ledger, review, tmp_path / "record.json")
    review.update(reviewer="isolated fixture reviewer", reviewed_at="2026-10-13T12:00:00+08:00")
    for preset in PRESETS:
        for criterion in RUBRIC:
            review["rubric"][preset][criterion] = {"verdict": "pass", "notes": "Compared every source ID and output paragraph in offline fixture."}
    result = record_review(ledger, review, tmp_path / "record.json")
    assert result["human_review_status"] == "recorded" and result["review_sha256"] == digest(review)
    with pytest.raises(FileExistsError):
        record_review(ledger, review, tmp_path / "record.json")
    review["reviewer_kind"] = "agent"
    assert record_review(ledger, review, tmp_path / "agent.json")["human_review_status"] == "not_reviewed"
    review["comparison_id"] = "f" * 64
    with pytest.raises(ValueError, match="identity"):
        record_review(ledger, review, tmp_path / "changed.json")


def test_incomplete_or_absent_outputs_cannot_claim_review_passed(tmp_path):
    ledger = tmp_path / "comparison.db"
    compare(source(), ledger, Provider(broken=True), maximum=2)
    report = comparison_report(ledger)
    packet_path = tmp_path / "incomplete-review.json"
    write_review_packet(ledger, packet_path)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert all(value == {"structure_status": "incomplete", "semantic_status": "unverified", "review_required": True}
               for value in packet["source_lineage"].values())
    review = {"comparison_id": report["comparison_id"], "reviewer_kind": "human", "reviewer": "fixture",
              "reviewed_at": "2026-10-13T12:00:00+08:00", "rubric": {preset: {
                  criterion: {"verdict": "pass", "notes": "fixture"} for criterion in RUBRIC} for preset in PRESETS}}
    with pytest.raises(ValueError, match="incomplete comparison"):
        record_review(ledger, review, tmp_path / "review.json")


@pytest.mark.parametrize("maximum", [True, 1, 21])
def test_comparison_budget_bounds_are_explicit(maximum):
    with pytest.raises(ValueError, match="physical budget"):
        comparison_manifest(source(), endpoint=ENDPOINT, model=MODEL, maximum=maximum)


def test_permission_planning_failure_and_transport_are_bounded(tmp_path):
    with pytest.raises(ValueError, match="allow-paid-calls"):
        compare_presets(source(), tmp_path / "no.db", endpoint=ENDPOINT, model=MODEL, allow_paid_calls=False)
    assert not (tmp_path / "no.db").exists()
    with pytest.raises(ValueError, match="cannot cover"):
        comparison_manifest(source(600), endpoint=ENDPOINT, model=MODEL, maximum=2)
    class Timeout:
        calls = 0
        def complete(self, request, config):
            self.calls += 1
            raise DeepSeekError("fixture transport")
    timeout = Timeout()
    report = compare(source(), tmp_path / "timeout.db", timeout, maximum=4)
    assert report["physical_calls"] == timeout.calls == 2
    assert compare(source(), tmp_path / "timeout.db", timeout, maximum=4) == report
    assert timeout.calls == 2


def test_disabled_regeneration_and_abnormal_asr_fail_before_additional_sends(tmp_path):
    no_retries = prepare_input(record(), None, EditorialConfig(max_chunk_regenerations=0))
    provider = Provider(broken=True)
    report = compare(no_retries, tmp_path / "zero-retry.db", provider)
    assert report["physical_calls"] == len(provider.calls) == 2
    source_record = record()
    invalid = replace(source_record, segments=tuple(reversed(source_record.segments)))
    prepared = prepare_input(invalid, None, EditorialConfig())
    with pytest.raises(JobExecutionError) as error:
        compare(prepared, tmp_path / "invalid.db", provider)
    assert error.value.error_code == "asr_quality_unusable"
    assert not (tmp_path / "invalid.db").exists() and len(provider.calls) == 2


def test_cli_comparison_report_and_named_review_keep_archive_readonly(tmp_path, monkeypatch, capsys):
    from bili_asr.services import editorial_comparison
    archive = tmp_path / "archive"
    connection = open_database(archive)
    try:
        _seed_part(connection)
        insert_record(connection, record())
        prepared = EditorialRepository(connection).prepare(1, None, EditorialConfig())
        before = [tuple(row) for row in connection.execute("SELECT * FROM editorial_inputs")]
        calls_before = connection.execute("SELECT COUNT(*) FROM editorial_model_calls").fetchone()[0]
        provider = Provider()
        monkeypatch.setattr(editorial_comparison, "DeepSeekClient", lambda: provider)
        ledger = tmp_path / "comparison.db"
        assert cli.main(["editorial", "compare", "--archive-root", str(archive), "--input-id", prepared["input_id"],
            "--ledger", str(ledger), "--editorial-base-url", ENDPOINT, "--editorial-model", MODEL,
            "--max-physical-calls", "4", "--max-output-tokens", "8192", "--allow-paid-calls"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["physical_calls"] == len(provider.calls) == 2
        missing_archive = tmp_path / "not-created"
        assert cli.main(["editorial", "compare-report", "--archive-root", str(missing_archive), "--ledger", str(ledger)]) == 0
        assert json.loads(capsys.readouterr().out) == report and not missing_archive.exists()
        packet = tmp_path / "packet.json"
        assert cli.main(["editorial", "compare-review", "--ledger", str(ledger), "--out", str(packet)]) == 0
        capsys.readouterr()
        assert before == [tuple(row) for row in connection.execute("SELECT * FROM editorial_inputs")]
        assert connection.execute("SELECT COUNT(*) FROM editorial_model_calls").fetchone()[0] == calls_before
    finally:
        connection.close()
