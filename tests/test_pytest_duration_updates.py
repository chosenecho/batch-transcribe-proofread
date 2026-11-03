import json
from pathlib import Path

import pytest

from scripts.update_pytest_durations import latest_reports, main, timing_hints


def junit(directory, shard, *, attempt=1, seconds="2", failure=""):
    report = directory / f"pytest-diagnostics-123-{attempt}-{shard}" / "test-results" / f"pytest-{shard}.xml"
    report.parent.mkdir(parents=True)
    report.with_name("shard-plan.json").write_text(json.dumps({
        "selected_shard": shard - 1, "collected": shard,
        "shards": [["tests/test_sample.py::example"] for _ in range(shard)],
    }))
    report.write_text(f'<testsuites><testsuite tests="1" failures="{int(bool(failure))}" errors="0">'
                     f'<testcase classname="tests.test_sample.Examples" name="example" time="{seconds}">'
                     f'{failure}</testcase></testsuite></testsuites>', encoding="utf-8")
    return report


def test_latest_rerun_replaces_failed_report_but_retains_older_successes(tmp_path):
    junit(tmp_path, 1, failure='<failure message="old failure"/>')
    newest = junit(tmp_path, 1, attempt=2)
    other = junit(tmp_path, 2)
    assert latest_reports(tmp_path, 2) == [newest, other]


def test_missing_shard_prevents_publishing_new_timing_hints(tmp_path):
    junit(tmp_path, 1)
    output = tmp_path / "hints.json"
    with pytest.raises(SystemExit) as rejected:
        main([str(tmp_path), "--output", str(output), "--source-ref", "commit"])
    assert rejected.value.code == 1 and not output.exists()


def test_interrupted_rerun_cannot_reuse_older_successful_report(tmp_path):
    junit(tmp_path, 1)
    interrupted = tmp_path / "pytest-diagnostics-123-2-1" / "test-results"
    interrupted.mkdir(parents=True)
    (interrupted / "pytest.log").write_text("worker stopped before producing JUnit")
    with pytest.raises(ValueError, match="latest artifact has no completed"):
        latest_reports(tmp_path, 1)


@pytest.mark.parametrize("change", ["count", "missing-time"])
def test_incomplete_junit_cannot_supply_timing_hints(tmp_path, change):
    report = junit(tmp_path, 1)
    text = report.read_text()
    text = text.replace('tests="1"', 'tests="100"') if change == "count" else text.replace(' time="2"', '')
    report.write_text(text)
    with pytest.raises(ValueError, match="test count|no recorded duration"):
        timing_hints([report], tmp_path)


def test_interrupted_shard_cannot_publish_partial_completed_times(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sample.py").touch()
    report = junit(tmp_path / "reports", 1)
    report.with_name("shard-plan.json").write_text(json.dumps({
        "selected_shard": 0, "collected": 2,
        "shards": [["tests/test_sample.py::first", "tests/test_sample.py::second"]],
    }))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        main(["reports", "--shard-count", "1", "--output", "hints.json", "--source-ref", "commit"])
    assert not (tmp_path / "hints.json").exists()


@pytest.mark.parametrize("seconds,failure", [
    ("nan", ""), ("inf", ""), ("-1", ""), ("2", '<failure message="failed"/>'),
])
def test_unusable_reports_cannot_replace_valid_hints(tmp_path, monkeypatch, seconds, failure):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sample.py").touch()
    junit(tmp_path / "reports", 1, seconds=seconds, failure=failure)
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "hints.json"
    output.write_text("prior valid hints")
    with pytest.raises(SystemExit):
        main(["reports", "--shard-count", "1", "--output", str(output), "--source-ref", "commit"])
    assert output.read_text() == "prior valid hints"


def test_timings_include_fixture_cost_and_map_test_classes_to_their_module(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sample.py").touch()
    reports = [junit(tmp_path / "reports", 1, seconds="8"), junit(tmp_path / "reports", 2, seconds="2")]
    assert timing_hints(reports, tmp_path) == {"tests/test_sample.py": 5.0}


def test_report_output_records_source_and_input_hashes(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sample.py").touch()
    junit(tmp_path / "reports", 1)
    monkeypatch.chdir(tmp_path)
    assert main(["reports", "--shard-count", "1", "--output", "hints.json", "--source-ref", "commit"]) == 0
    hints = json.loads(Path("hints.json").read_text())
    assert hints["source"]["head"] == "commit"
    assert len(hints["source"]["inputs"][0]["sha256"]) == 64
    assert hints["seconds_per_test"] == {"tests/test_sample.py": 2.0}
