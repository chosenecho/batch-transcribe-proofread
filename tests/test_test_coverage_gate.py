from pathlib import Path
import json
import os
import subprocess
import sys

import pytest

from scripts.check_test_coverage import check


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = Path("src")
    root.mkdir()
    (root / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
    return root


def report():
    return {
        "files": {"src/example.py": {}},
        "totals": {"covered_lines": 80, "num_statements": 100, "covered_branches": 70, "num_branches": 100},
    }


def test_exact_floors_pass(source):
    assert check(report(), source, 80, 70) == []


def test_branch_drop_fails_even_with_high_line_coverage(source):
    data = report()
    data["totals"]["covered_lines"] = 100
    data["totals"]["covered_branches"] = 69
    assert check(data, source, 80, 70) == ["branch coverage below 70.00%"]


def test_unrounded_line_drop_fails(source):
    data = report()
    data["totals"].update(covered_lines=79999, num_statements=100000)
    assert check(data, source, 80, 70) == ["line coverage below 80.00%"]


def test_missing_source_and_inflating_test_files_are_rejected(source):
    data = report()
    data["files"] = {"tests/test_example.py": {}}
    errors = check(data, source, 80, 70)
    assert len(errors) == 2
    assert "src/example.py" in errors[0]
    assert "tests/test_example.py" in errors[1]


def test_empty_measurement_fails(source):
    data = report()
    data["totals"] = dict.fromkeys(data["totals"], 0)
    assert len(check(data, source, 80, 70)) == 2


def test_repository_config_measures_subprocess_and_unexecuted_source(tmp_path):
    config = Path(__file__).resolve().parents[1] / "pyproject.toml"
    (tmp_path / "pyproject.toml").write_bytes(config.read_bytes())
    source = tmp_path / "src"
    source.mkdir()
    (source / "child_module.py").write_text("VALUE = 42\n", encoding="utf-8")
    (source / "untouched.py").write_text("VALUE = 0\n", encoding="utf-8")
    (tmp_path / "driver.py").write_text(
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', "
        "\"import sys; sys.path.insert(0, 'src'); import child_module\"], check=True)\n",
        encoding="utf-8",
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("COVERAGE_")}
    env["COVERAGE_RCFILE"] = str(tmp_path / "pyproject.toml")
    env["COVERAGE_FILE"] = str(tmp_path / ".coverage")
    for args in (["run", "driver.py"], ["combine"], ["json", "-o", "report.json"]):
        subprocess.run([sys.executable, "-m", "coverage", *args], cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
    data = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert set(data["files"]) == {"src/child_module.py", "src/untouched.py"}
    assert data["files"]["src/child_module.py"]["summary"]["covered_lines"] == 1
    assert data["files"]["src/untouched.py"]["summary"]["covered_lines"] == 0
