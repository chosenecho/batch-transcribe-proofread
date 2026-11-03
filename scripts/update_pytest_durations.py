"""Produce reviewable shard timing hints from successful pytest JUnit reports."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def latest_reports(directory: Path, shard_count: int) -> list[Path]:
    selected: dict[int, tuple[int, Path]] = {}
    artifacts: dict[int, tuple[int, Path]] = {}
    for parent in directory.rglob("pytest-diagnostics-*"):
        match = re.fullmatch(r"pytest-diagnostics-\d+-(\d+)-(\d+)", parent.name)
        if not parent.is_dir() or match is None:
            continue
        attempt, shard = int(match[1]), int(match[2])
        previous = artifacts.get(shard)
        if previous is not None and previous[0] == attempt:
            raise ValueError(f"duplicate artifact for shard {shard}, attempt {attempt}")
        if previous is None or attempt > previous[0]:
            artifacts[shard] = (attempt, parent)
    for shard, (_, parent) in artifacts.items():
        if not (parent / "test-results" / f"pytest-{shard}.xml").is_file():
            raise ValueError(f"latest artifact has no completed JUnit report for shard {shard}")
    for path in directory.rglob("pytest-*.xml"):
        match = re.fullmatch(r"pytest-(\d+)\.xml", path.name)
        if match is None:
            continue
        shard = int(match[1])
        attempt = 0
        for parent in path.parents:
            artifact = re.fullmatch(r"pytest-diagnostics-\d+-(\d+)-(\d+)", parent.name)
            if artifact:
                if int(artifact[2]) != shard:
                    raise ValueError("JUnit shard differs from artifact identity")
                attempt = int(artifact[1])
                break
        previous = selected.get(shard)
        if previous is not None and previous[0] == attempt:
            raise ValueError(f"duplicate JUnit report for shard {shard}, attempt {attempt}")
        if previous is None or attempt > previous[0]:
            selected[shard] = (attempt, path)
    if set(selected) != set(range(1, shard_count + 1)):
        raise ValueError(f"expected complete JUnit reports for {shard_count} shards")
    return [selected[shard][1] for shard in sorted(selected)]


def timing_hints(reports: list[Path], root: Path) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for report in reports:
        document = ET.parse(report).getroot()
        if document.tag not in {"testsuites", "testsuite"} or not list(document.iter("testsuite")):
            raise ValueError("JUnit report has no test suites")
        for suite in document.iter("testsuite"):
            if "tests" not in suite.attrib or int(suite.attrib["tests"]) != len(list(suite.iter("testcase"))):
                raise ValueError("JUnit declared test count differs from completed testcases")
            if "failures" not in suite.attrib or "errors" not in suite.attrib:
                raise ValueError("JUnit report is missing outcome counts")
            if int(suite.attrib["failures"]) or int(suite.attrib["errors"]):
                raise ValueError(f"failed tests cannot update timing hints: {report.name}")
        cases = list(document.iter("testcase"))
        if not cases:
            raise ValueError(f"JUnit report contains no tests: {report.name}")
        for case in cases:
            if case.find("failure") is not None or case.find("error") is not None:
                raise ValueError("failed testcase cannot update timing hints")
            if "time" not in case.attrib:
                raise ValueError("JUnit testcase has no recorded duration")
            seconds = float(case.attrib["time"])
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError("JUnit times must be finite and nonnegative")
            # classname may include a test class; choose the actual module.
            parts = case.get("classname", "").split(".")
            if parts[0] != "tests" or not all(part.isidentifier() for part in parts):
                raise ValueError("JUnit classname must name a repository test module")
            while parts:
                name = "/".join(parts) + ".py"
                if (root / name).is_file():
                    break
                parts.pop()
            if not parts:
                raise ValueError("JUnit test module is missing from the checkout")
            totals[name] += seconds
            counts[name] += 1
    return {name: max(0.01, round(totals[name] / counts[name], 4)) for name in sorted(totals)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path)
    parser.add_argument("--shard-count", type=int, default=4)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--environment", default="GitHub Actions Ubuntu 24.04 / Python 3.12 / coverage")
    args = parser.parse_args(argv)
    if args.shard_count < 1:
        parser.error("shard count must be positive")
    try:
        reports = latest_reports(args.reports, args.shard_count)
        for index, report in enumerate(reports):
            plan = json.loads(report.with_name("shard-plan.json").read_text(encoding="utf-8"))
            if (plan["selected_shard"] != index or len(plan["shards"]) != args.shard_count
                    or sum(len(shard) for shard in plan["shards"]) != plan["collected"]
                    or len(list(ET.parse(report).getroot().iter("testcase"))) != len(plan["shards"][index])):
                raise ValueError("JUnit report does not contain the complete planned shard")
        hints = timing_hints(reports, Path.cwd())
    except (ValueError, KeyError, TypeError, OSError, ET.ParseError) as error:
        parser.exit(1, f"Timing report rejected: {error}\n")
    result = {
        "schema_version": 1,
        "source": {
            "head": args.source_ref,
            "date": datetime.now(timezone.utc).date().isoformat(),
            "environment": args.environment,
            "inputs": [{"path": path.as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                       for path in reports],
            "note": "Scheduling hints only; review after changes to fixtures or runtime performance.",
        },
        "seconds_per_test": hints,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Measured {len(hints)} test modules across {len(reports)} complete shard reports")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
