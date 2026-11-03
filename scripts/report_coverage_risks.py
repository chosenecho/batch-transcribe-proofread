"""Write a risk-prioritized inventory of uncovered product lines and branches."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

CRITICAL = (
    "storage/", "workflow", "file_ops", "publication", "content_rights",
    "archive_snapshot", "archive_migration", "reference_backup", "artifact_restore",
    "retry_maintenance", "editorial_runtime",
)


def inventory(report: dict) -> list[dict]:
    rows = []
    for filename, entry in report["files"].items():
        missing = entry["missing_lines"]
        branches = entry.get("missing_branches", [])
        if not missing and not branches:
            continue
        rows.append({"file": filename, "risk": "critical" if any(part in filename for part in CRITICAL) else "standard",
                     "missing_lines": missing, "missing_branches": branches,
                     "excluded_lines": entry.get("excluded_lines", []), "summary": entry["summary"]})
    return sorted(rows, key=lambda row: (row["risk"] != "critical", -len(row["missing_branches"]), -len(row["missing_lines"]), row["file"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, default=Path("coverage-risks.json"))
    parser.add_argument("--markdown", type=Path, default=Path("coverage-risks.md"))
    args = parser.parse_args()
    rows = inventory(json.loads(args.report.read_text(encoding="utf-8")))
    args.output.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    lines = ["# Uncovered product behavior", "", "Prioritized review inventory; coverage is execution evidence, not proof of correctness.", "",
             "| Risk | Module | Missing branches | Missing lines |", "| --- | --- | ---: | ---: |"]
    lines.extend(f"| {r['risk']} | {r['file']} | {len(r['missing_branches'])} | {len(r['missing_lines'])} |" for r in rows)
    args.markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
