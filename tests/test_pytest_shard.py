from collections import Counter
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import pytest_shard


def timing_hints(path, values):
    path.write_text(json.dumps({"schema_version": 1, "seconds_per_test": values}), encoding="utf-8")
    return path


def test_plan_keeps_files_together_and_includes_new_nodes_exactly_once(tmp_path):
    nodes = [f"tests/test_{name}.py::test_case[{index}]"
             for name, count in [("slow", 2), ("medium", 4), ("new", 9), ("fast", 6)]
             for index in range(count)]
    hints = timing_hints(tmp_path / "times.json", {
        "tests/test_slow.py": 10, "tests/test_medium.py": 3,
        "tests/test_fast.py": 0.5, "tests/test_retired.py": 999,
    })
    shards, loads = pytest_shard._shards(nodes, 3, hints)
    assert Counter(node for shard in shards for node in shard) == Counter(nodes)
    owners = {}
    for index, shard in enumerate(shards):
        for node in shard:
            name = node.split("::", 1)[0]
            assert owners.setdefault(name, index) == index
    assert loads == [20, 12, 12]
    assert pytest_shard._shards(list(reversed(nodes)), 3, hints) == (shards, loads)


def test_missing_hints_still_assign_every_test_and_allow_empty_shards(tmp_path):
    nodes = ["tests/test_a.py::test_a", "tests/test_a.py::test_b", "tests/test_new.py::test_a"]
    shards, loads = pytest_shard._shards(nodes, 4, tmp_path / "absent.json")
    assert shards == [nodes[:2], nodes[2:], [], []]
    assert loads == [2, 1, 0, 0]


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "slow"])
def test_invalid_duration_cannot_silently_change_shard_ownership(tmp_path, value):
    hints = timing_hints(tmp_path / "times.json", {"tests/test_a.py": value})
    with pytest.raises(ValueError, match="finite positive"):
        pytest_shard._shards(["tests/test_a.py::test_a"], 4, hints)


def permit_collection(monkeypatch, nodes):
    from bili_asr.contracts import release_gate
    monkeypatch.delenv("BILI_ASR_CONTRACT_BASELINE_REF", raising=False)
    monkeypatch.setattr(pytest_shard, "_node_ids", lambda: nodes)
    monkeypatch.setattr(release_gate, "validate_release_collection", lambda *a, **kw: {
        "registered_edges": 9, "frozen_files": 3, "baseline_count": 1,
    })


def test_empty_shard_never_invokes_pytest_on_the_whole_repository(monkeypatch, tmp_path):
    permit_collection(monkeypatch, ["tests/test_a.py::test_a"])
    monkeypatch.setattr(pytest_shard.subprocess, "run", lambda *a, **kw: pytest.fail("empty shard must not execute"))
    assert pytest_shard.main(["--shard-index", "1", "--shard-count", "2",
                              "--durations-file", str(tmp_path / "absent.json")]) == 0


def test_response_file_preserves_nodes_and_failure_status(monkeypatch, tmp_path):
    nodes = ["tests/test_example.py::test_names[with spaces]", "tests/test_example.py::test_names[中文]"]
    permit_collection(monkeypatch, nodes)
    selection_paths = []
    def execute(command, **kwargs):
        assert command[:7] == [sys.executable, "-m", "coverage", "run", "--branch", "--parallel-mode", "-m"]
        assert command[7:9] == ["pytest", "-q"]
        selection = Path(command[-1][1:])
        selection_paths.append(selection)
        assert selection.read_text(encoding="utf-8").splitlines() == nodes
        return subprocess.CompletedProcess(command, 1)
    monkeypatch.setattr(pytest_shard.subprocess, "run", execute)
    plan = tmp_path / "diagnostics/plan.json"
    assert pytest_shard.main(["--shard-index", "0", "--shard-count", "1", "--coverage",
                              "--plan-output", str(plan), "--", "-q"]) == 1
    assert json.loads(plan.read_text())["shards"] == [nodes]
    assert not selection_paths[0].exists()
    assert not selection_paths[0].parent.exists()


def test_real_pytest_reads_selection_and_only_builds_module_fixture_once(tmp_path):
    project = tmp_path / "test-project"
    project.mkdir()
    # Keep collection node ids relative to this isolated project even when
    # pytest's temporary root is inside the repository checkout.
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    module = project / "test_sample.py"
    module.write_text('''import pytest
from pathlib import Path
@pytest.fixture(scope="module")
def installed():
    counter = Path("fixture-count.txt")
    counter.write_text(counter.read_text() + "x" if counter.exists() else "x")
@pytest.mark.parametrize("name", ["with spaces", "中文"])
def test_name(name, installed):
    assert name
def test_unselected():
    raise AssertionError("response file must not collect this test")
''', encoding="utf-8")
    collected = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"],
                               cwd=project, check=True, capture_output=True, text=True)
    nodes = [line for line in collected.stdout.splitlines() if line.startswith("test_sample.py::test_name[")]
    selection = tmp_path / "selection.txt"
    selection.write_text("\n".join(nodes) + "\n", encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", f"@{selection}"],
                            cwd=project, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    assert (project / "fixture-count.txt").read_text() == "x"


def test_response_directory_is_cleaned_when_pytest_cannot_start(monkeypatch, tmp_path):
    permit_collection(monkeypatch, ["tests/test_example.py::test_one"])
    selection_paths = []
    def execute(command, **kwargs):
        selection = Path(command[-1][1:])
        selection_paths.append(selection)
        assert selection.is_file()
        raise OSError("pytest process could not start")
    monkeypatch.setattr(pytest_shard.subprocess, "run", execute)
    with pytest.raises(OSError, match="could not start"):
        pytest_shard.main(["--shard-index", "0", "--shard-count", "1"])
    assert not selection_paths[0].parent.exists()
