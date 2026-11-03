"""Require coverage for executable product lines added since an exact Git base."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess


def changed_lines(base: str) -> dict[str, set[int]]:
    if re.fullmatch(r"[0-9a-fA-F]{40}", base) is None:
        raise ValueError("coverage base must be a full Git commit SHA")
    # Compare to the exact PR base, without relying on shallow merge ancestry.
    diff = subprocess.run(
        ["git", "-c", "core.quotePath=false", "diff", "--no-ext-diff", "--no-renames",
         "--unified=0", base, "HEAD", "--", "src"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout
    result: dict[str, set[int]] = {}
    filename: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            if line.startswith('+++ "'):
                raise ValueError("quoted product paths require explicit coverage review")
            path = line[6:].rstrip("\t")
            filename = path if line.startswith("+++ b/") and path.endswith(".py") else None
        elif filename and (match := re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)):
            start, count = int(match[1]), int(match[2] or "1")
            result.setdefault(filename, set()).update(range(start, start + count))
    return result


def check(report: dict, changed: dict[str, set[int]], floor: float) -> tuple[list[dict], list[str]]:
    rows, errors = [], []
    for filename, lines in sorted(changed.items()):
        if not lines:
            continue
        entry = report["files"].get(filename)
        if entry is None:
            errors.append(f"changed product file is absent from coverage: {filename}")
            continue
        executed, missing = set(entry["executed_lines"]), set(entry["missing_lines"])
        # Exclusions may document old impossibilities, but cannot exempt newly
        # changed executable code from review with a pragma: no cover.
        excluded = set(entry.get("excluded_lines", []))
        eligible = lines & (executed | missing | excluded)
        uncovered = eligible - executed
        if not eligible:
            continue
        rate = 100 * (len(eligible) - len(uncovered)) / len(eligible)
        rows.append({"file": filename, "covered": len(eligible) - len(uncovered),
                     "total": len(eligible), "percent": rate, "missing_lines": sorted(uncovered)})
        if rate < floor:
            errors.append(f"{filename}: changed-line coverage {rate:.2f}% below {floor:.2f}%; missing {sorted(uncovered)}")
    return rows, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--base", required=True)
    parser.add_argument("--line-min", type=float, default=90)
    parser.add_argument("--output", type=Path, default=Path("changed-coverage.json"))
    args = parser.parse_args()
    if not 0 <= args.line_min <= 100:
        parser.error("coverage floor must be between 0 and 100")
    try:
        rows, errors = check(json.loads(args.report.read_text(encoding="utf-8")), changed_lines(args.base), args.line_min)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Changed coverage rejected: {error}\n")
    args.output.write_text(json.dumps({"base": args.base, "files": rows, "errors": errors}, indent=2) + "\n", encoding="utf-8")
    for row in rows:
        print(f"{row['file']}: {row['covered']}/{row['total']} changed lines ({row['percent']:.2f}%)")
    for error in errors:
        print(f"ERROR: {error}")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
