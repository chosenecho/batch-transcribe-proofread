import subprocess

import pytest

from scripts.check_changed_coverage import changed_lines, check
from scripts.report_coverage_risks import inventory


def sample():
    return {"files": {"src/example.py": {"executed_lines": [1, 2], "missing_lines": [3], "excluded_lines": [4]}}}


def test_changed_executable_lines_require_real_execution():
    rows, errors = check(sample(), {"src/example.py": {1, 3, 5}}, 90)
    assert rows[0]["total"] == 2 and rows[0]["missing_lines"] == [3]
    assert len(errors) == 1


def test_coverage_exclusions_cannot_hide_new_untested_code():
    rows, errors = check(sample(), {"src/example.py": {4}}, 90)
    assert rows[0]["percent"] == 0 and errors


def test_unchanged_missing_code_does_not_block_changed_covered_code():
    assert check(sample(), {"src/example.py": {1, 2}}, 90)[1] == []


def test_absent_changed_module_fails_closed():
    assert "absent" in check(sample(), {"src/new.py": {1}}, 90)[1][0]


def test_deletions_and_non_executable_changes_need_no_coverage():
    assert check(sample(), {"src/deleted.py": set(), "src/example.py": {5}}, 90) == ([], [])


def test_exact_unrounded_changed_floor():
    entry = {"executed_lines": list(range(1, 10)), "missing_lines": [10]}
    data = {"files": {"src/example.py": entry}}
    assert check(data, {"src/example.py": set(range(1, 11))}, 90)[1] == []
    assert check(data, {"src/example.py": set(range(1, 11))}, 90.01)[1]


def test_real_git_diff_handles_add_replace_delete_and_spaces(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()
    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Synthetic test")
    (tmp_path / "src").mkdir()
    file = tmp_path / "src/with space.py"
    file.write_text("A = 1\nB = 2\nC = 3\n")
    git("add", ".")
    git("commit", "-qm", "baseline")
    base = git("rev-parse", "HEAD")
    file.write_text("A = 4\nC = 3\nD = 5\n")
    (tmp_path / "src/new.py").write_text("VALUE = 1\n")
    git("add", ".")
    git("commit", "-qm", "change")
    assert changed_lines(base) == {"src/new.py": {1}, "src/with space.py": {1, 3}}


@pytest.mark.parametrize("base", ["main", "--help", "", "a" * 39])
def test_changed_coverage_requires_exact_baseline(base):
    with pytest.raises(ValueError, match="full Git"):
        changed_lines(base)


def test_quoted_git_path_cannot_silently_escape_coverage(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        stdout='+++ "b/src/a\\tfile.py"\n@@ -0,0 +1 @@\n+VALUE = 1\n'))
    with pytest.raises(ValueError, match="quoted product paths"):
        changed_lines("a" * 40)


def test_risk_inventory_prioritizes_persistence_over_more_missing_utilities():
    data = {"files": {
        "src/bili_asr/storage/workflow.py": {"missing_lines": [1], "missing_branches": [[1, 2]], "summary": {}},
        "src/bili_asr/utils.py": {"missing_lines": list(range(100)), "missing_branches": [], "summary": {}},
    }}
    rows = inventory(data)
    assert rows[0]["risk"] == "critical" and rows[0]["missing_branches"] == [[1, 2]]
