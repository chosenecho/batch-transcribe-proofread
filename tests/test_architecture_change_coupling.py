"""Historical metrics must remain reproducible on small known histories."""
import subprocess

import pytest

from scripts.architecture_change_coupling import analyze, generated_paths, main


def run(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def history(tmp_path):
    run(tmp_path, "init", "-b", "main")
    run(tmp_path, "config", "user.name", "Architecture Test")
    run(tmp_path, "config", "user.email", "architecture@example.invalid")
    return tmp_path


def commit(root, *paths):
    for name in paths:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write("# change\n")
    run(root, "add", "--all")
    run(root, "commit", "-qm", "change")
    return run(root, "rev-parse", "HEAD")


A = "src/bili_asr/cli/one.py"
B = "src/bili_asr/services/two.py"


def test_counts_single_file_touches_and_exact_generated_exclusions(history):
    first = commit(history, A, B, "docs/contracts/registry.json", "tests/test_one.py")
    commit(history, A, B, "docs/other/registry.json")
    commit(history, A)
    report = analyze(history, minimum=2, evidence_pair=[B, A])
    assert report["source_commits"] == 3
    assert report["ranking"] == [dict(a=A, b=B, cochange=2, touches=[3, 2], strength=1)]
    assert first in report["evidence"]["commits"]
    assert len(report["evidence"]["commits"]) == 2
    companions = {r["path"] for r in report["companion_files"]}
    assert "docs/contracts/registry.json" not in companions
    assert "docs/other/registry.json" in companions
    assert "tests/test_one.py" in companions
    assert "docs/contracts/registry.json" in generated_paths()
    limited = analyze(history, base=first, minimum=1)
    assert limited["ranking"][0]["touches"] == [2, 1]
    assert analyze(history, revision=first, minimum=1)["source_commits"] == 1


def test_merge_does_not_double_count_and_rename_starts_new_identity(history):
    commit(history, A, B)
    run(history, "checkout", "-qb", "feature")
    commit(history, A, B)
    run(history, "checkout", "main")
    run(history, "merge", "--no-ff", "feature", "-m", "merge")
    assert analyze(history, minimum=1)["ranking"][0]["cochange"] == 2
    renamed = "src/bili_asr/services/renamed.py"
    run(history, "mv", B, renamed)
    commit(history, A)
    report = analyze(history, minimum=1)
    assert all(B not in (r["a"], r["b"]) for r in report["ranking"])
    assert report["ranking"][0]["cochange"] == 1


def test_same_package_not_ranked_and_shallow_history_is_rejected(history, tmp_path):
    commit(history, A, "src/bili_asr/cli/three.py")
    report = analyze(history, minimum=1)
    assert report["qualified_pairs"] == 1
    assert report["ranking"] == []
    clone = tmp_path / "clone"
    subprocess.check_call(["git", "clone", "--depth=1", history.as_uri(), str(clone)])
    with pytest.raises(ValueError, match="incomplete history"):
        analyze(clone)


def test_root_package_deletion_threshold_and_date_filter(history):
    root_a = "src/bili_asr/root_a.py"
    root_b = "src/bili_asr/root_b.py"
    commit(history, root_a, root_b, B)
    report = analyze(history, minimum=1)
    assert report["qualified_pairs"] == 3
    assert report["cross_package_pairs"] == 2
    assert analyze(history, minimum=2)["ranking"] == []
    assert analyze(history, since="2030-01-01T00:00:00Z", minimum=1)["source_commits"] == 0
    run(history, "rm", B)
    commit(history, root_a)
    assert analyze(history, minimum=1)["ranking"] == []


def test_command_outputs_json_and_reports_errors(history, capsys):
    import json

    commit(history, A, B)
    output = history / "reports" / "coupling.json"
    assert main(["--repo", str(history), "--min-cochanges", "1", "--json-output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["ranking"][0]["cochange"] == 1
    assert report["revision"] in capsys.readouterr().out
    assert main(["--repo", str(history), "--revision", "missing-ref"]) == 1
    assert "unavailable" in capsys.readouterr().err
    with pytest.raises(ValueError, match="positive"):
        analyze(history, minimum=0)
