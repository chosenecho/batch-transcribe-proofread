"""Report historical cochanges as review evidence, never as an architecture gate.

Run in an installed project environment. Renames are intentionally treated as
delete/add; only Python source paths present at the selected revision rank.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import itertools
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def generated_paths() -> set[str]:
    from bili_asr.contracts.__main__ import documentation_files

    return {"docs/contracts/" + name for name in documentation_files()}


def package(path: str) -> str:
    parts = PurePosixPath(path).parts
    return parts[2] if len(parts) > 3 else "root"


def source(path: str) -> bool:
    return path.startswith("src/bili_asr/") and path.endswith(".py")


def analyze(root: Path, *, revision="HEAD", base=None, since=None, minimum=6,
            top=20, evidence_pair=None) -> dict:
    if minimum < 1 or top < 1:
        raise ValueError("minimum and top must be positive")
    if git(root, "rev-parse", "--is-shallow-repository").strip() == b"true":
        raise ValueError("incomplete history: fetch the full repository before analysis")
    sha = git(root, "rev-parse", "--verify", revision + "^{commit}").decode().strip()
    base_sha = None
    if base:
        base_sha = git(root, "rev-parse", "--verify", base + "^{commit}").decode().strip()
        git(root, "merge-base", "--is-ancestor", base_sha, sha)
    current = {p.decode("utf-8") for p in git(root, "ls-tree", "-r", "-z", "--name-only", sha,
                                           "--", "src/bili_asr").split(b"\0") if p}
    excluded = generated_paths()
    args = ["log", "--no-merges", "--no-renames", "--format=%x00%H", "--name-only", "-z", sha]
    if base_sha:
        args.append("^" + base_sha)
    if since:
        args.append("--since=" + since)
    # NUL delimits paths (including spaces); each commit starts with an empty
    # token followed by its hash. Git adds a newline before its first path.
    tokens = git(root, *args, "--", "src/bili_asr", "tests", "docs").split(b"\0")
    commits = []
    commit = None
    files = set()
    for token in tokens:
        if not token:
            continue
        value = token.decode("utf-8").lstrip("\n")
        if len(value) == 40 and all(c in "0123456789abcdef" for c in value):
            if commit is not None:
                commits.append((commit, files))
            commit, files = value, set()
        else:
            files.add(value)
    if commit is not None:
        commits.append((commit, files))
    touches = Counter()
    pairs = Counter()
    evidence = defaultdict(list)
    companions = Counter()
    source_commits = 0
    requested = tuple(sorted(evidence_pair)) if evidence_pair else None
    for commit, files in commits:
        sources = sorted(p for p in files if source(p) and p in current)
        if any(source(p) for p in files):
            source_commits += 1
        touches.update(sources)
        for pair in itertools.combinations(sources, 2):
            pairs[pair] += 1
            if pair == requested:
                evidence[pair].append(commit)
        companions.update(p for p in files if sources and p.startswith(("tests/", "docs/"))
                          and p not in excluded)
    qualified = [(a, b, n) for (a, b), n in pairs.items() if n >= minimum]
    rows = [dict(a=a, b=b, cochange=n, touches=[touches[a], touches[b]],
                 strength=n / min(touches[a], touches[b]))
            for a, b, n in qualified if package(a) != package(b)]
    rows.sort(key=lambda r: (-r["strength"], -r["cochange"], r["a"], r["b"]))
    return dict(revision=sha, base=base_sha, since=since, source_commits=source_commits,
                policy="non-merge commits; renames=delete/add; current source paths only; root files share root package",
                minimum=minimum, qualified_pairs=len(qualified), cross_package_pairs=len(rows),
                generated_exclusions=sorted(excluded), ranking=rows[:top],
                companion_files=[dict(path=p, commits=n) for p, n in sorted(companions.items(), key=lambda x: (-x[1], x[0]))[:top]],
                evidence=None if requested is None else dict(a=requested[0], b=requested[1], commits=evidence[requested]))


def render(report: dict) -> str:
    lines = ["# Architecture change coupling", "", f"Revision: {report['revision']}",
             f"Base (exclusive): {report['base']}; since: {report['since']}", report["policy"],
             f"Source commits: {report['source_commits']}; minimum cochanges: {report['minimum']}; "
             f"qualified pairs: {report['qualified_pairs']}; cross-package: {report['cross_package_pairs']}",
             "", "Association is review evidence, not proof of a required joint change.", "",
             "| Strength | Cochange | Touches | A | B |", "|---|---|---|---|---|"]
    for row in report["ranking"]:
        lines.append(f"| {row['strength']:.3f} | {row['cochange']} | {row['touches']} | {row['a']} | {row['b']} |")
    if report["evidence"] is not None:
        lines.extend(["", "Evidence: " + json.dumps(report["evidence"], ensure_ascii=False)])
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--base", help="exclusive ancestor revision")
    parser.add_argument("--since", help="Git date filter")
    parser.add_argument("--min-cochanges", type=int, default=6)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--evidence-pair", nargs=2, metavar=("A", "B"))
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = analyze(args.repo, revision=args.revision, base=args.base, since=args.since,
                         minimum=args.min_cochanges, top=args.top, evidence_pair=args.evidence_pair)
        if args.json_output:
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(render(report), end="")
    except (subprocess.CalledProcessError, ValueError, OSError) as exc:
        print(f"Architecture report unavailable: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
