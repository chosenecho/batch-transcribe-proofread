"""Enforce separate product line and branch coverage floors."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def check(report: dict, source: Path, line_floor: float, branch_floor: float) -> list[str]:
    files = report["files"]
    expected = {path.as_posix() for path in source.rglob("*.py")}
    measured = {Path(name).as_posix() for name in files}
    errors = []
    if missing := expected - measured:
        errors.append("source files absent from coverage report: " + ", ".join(sorted(missing)))
    if unexpected := measured - expected:
        errors.append("non-product files in coverage report: " + ", ".join(sorted(unexpected)))
    totals = report["totals"]
    for label, covered, total, floor in (
        ("line", totals["covered_lines"], totals["num_statements"], line_floor),
        ("branch", totals["covered_branches"], totals["num_branches"], branch_floor),
    ):
        rate = 100 * covered / total if total else 0
        print(f"Product {label} coverage: {rate:.2f}% ({covered}/{total}); minimum {floor:.2f}%")
        if total == 0 or rate < floor:
            errors.append(f"{label} coverage below {floor:.2f}%")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--source", type=Path, default=Path("src"))
    parser.add_argument("--line-min", type=float, required=True)
    parser.add_argument("--branch-min", type=float, required=True)
    args = parser.parse_args()
    for value in (args.line_min, args.branch_min):
        if not 0 <= value <= 100:
            parser.error("coverage floors must be between 0 and 100")
    errors = check(json.loads(args.report.read_text(encoding="utf-8")), args.source, args.line_min, args.branch_min)
    for error in errors:
        print(f"ERROR: {error}")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
