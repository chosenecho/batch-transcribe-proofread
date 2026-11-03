"""A release probe must execute and pass; skipped probes are unresolved."""
from __future__ import annotations

import argparse
from pathlib import Path
import xml.etree.ElementTree as ET


def validate(path: Path) -> int:
    root = ET.parse(path).getroot()
    suites = list(root.iter("testsuite"))
    cases = list(root.iter("testcase"))
    if not suites or not cases:
        raise ValueError("validation produced no completed tests")
    for suite in suites:
        if int(suite.attrib["tests"]) != len(list(suite.iter("testcase"))):
            raise ValueError("validation report is incomplete")
        if any(int(suite.attrib[key]) for key in ("errors", "failures", "skipped")):
            raise ValueError("validation failed or was skipped; release evidence remains unresolved")
    if any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
        raise ValueError("validation testcase did not pass")
    return len(cases)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        count = validate(args.report)
    except (ValueError, KeyError, OSError, ET.ParseError) as error:
        parser.exit(1, f"Validation rejected: {error}\n")
    print(f"Validation: {count} executed tests passed; none skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
