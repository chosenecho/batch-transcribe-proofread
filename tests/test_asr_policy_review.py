"""Independent review: execution counters cannot define a configuration group."""

from dataclasses import replace
import json
import pytest

from bili_asr.services.asr_performance import asr_performance_report
from test_asr_performance import _database
from test_asr_qwen import _runner


@pytest.mark.parametrize("legacy_records", [0, 1, 2])
@pytest.mark.parametrize("switch_cache", [False, True])
def test_repeated_same_cache_policy_aggregates_one_configuration(monkeypatch, tmp_path, legacy_records, switch_cache):
    runner, _ = _runner(monkeypatch, text="今天。", chunk_seconds=1)
    runner.config = replace(runner.config, asr_cache_implementation="dynamic")
    connection = _database(tmp_path)
    for index in (1, 2):
        if index == 2 and switch_cache:
            runner, _ = _runner(monkeypatch, text="今天。", chunk_seconds=1, asr_cache_implementation="static")
        runner.transcribe("/virtual/input")
        evidence = {"schema_version": 1,
            "audio": {"sha256": str(index) * 64, "duration_ms": 3000},
            "runtime_binding": {"model": "same-model"},
            "diagnostics": runner.diagnostics()}
        if index <= legacy_records:
            evidence["diagnostics"]["execution_policy"]["runtime_strategies"] = (
                evidence["diagnostics"]["passes"][0].pop("runtime_strategies"))
        if index == 1:
            connection.execute("UPDATE transcript_asr_evidence SET evidence_json=? WHERE run_id='r1'",
                               (json.dumps(evidence),))
        else:
            connection.execute("INSERT INTO workflow_jobs VALUES ('j2', 'asr', 1, 2, 'succeeded')")
            connection.execute("INSERT INTO workflow_attempts VALUES ('a2','j2',102,113,'succeeded',?)",
                               (json.dumps({"run_id": "r2"}),))
            connection.execute("INSERT INTO transcript_asr_evidence VALUES ('r2',2,5,?)",
                               (json.dumps(evidence),))
    report = asr_performance_report(connection, start=100, end=120)
    assert len(report["groups"]) == (2 if switch_cache else 1)
    assert sum(group["attempt_outcomes"]["succeeded"] for group in report["groups"]) == 2
    assert sum(group["unique_success_audio_s"] for group in report["groups"]) == 6
    connection.close()
