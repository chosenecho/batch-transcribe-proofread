"""Read-only coverage projection for the SQLite workflow database."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact_root import ArtifactRoots
from .archive import archive_bundle_complete
from .artifacts import REQUIRED_ARTIFACT_KEYS

SCHEMA_VERSION = "coverage-report-v2"
CSV_COLUMNS = ("schema_version", "scope", "denominator_unit", "denominator_count", "denominator_state", "denominator_source", "cumulative_unit", "cumulative_complete", "cumulative_total", "cumulative_state", "batch_unit", "batch_complete", "batch_total", "batch_state", "work_id", "category", "status", "artifact_present", "reclaimed_audio", "coverage", "coverage_short", "evidence_summary", "diagnostic_summary")


def _select(records: dict[str, dict[str, Any]], scope: str | None) -> tuple[dict[str, dict[str, Any]], bool]:
    if scope is None:
        return records, True
    if scope in {"pending", "incomplete"}:
        return {k: v for k, v in records.items() if v.get("status") != "archived"}, True
    selected: dict[str, dict[str, Any]] = {}
    tokens = scope.replace(",", " ").split()
    if not tokens:
        return {}, False
    for token in tokens:
        matches = {k: v for k, v in records.items() if k == token or v.get("bvid") == token}
        if not matches:
            return {}, False
        selected.update(matches)
    return selected, True


def _build_workflow_data(root: Path, *, scope: str | None, artifact_roots: ArtifactRoots) -> dict[str, Any]:
    from bili_asr.services.workflow_projection import workflow_records

    database_available = (root / "archive.db").is_file()
    records, scope_available = _select(workflow_records(root, artifact_roots=artifact_roots, verify_artifacts=False), scope)
    scope_available = scope_available and database_available
    diagnostics: list[dict[str, str]] = []
    if not database_available:
        diagnostics.append({"code": "workflow_database_missing", "category": "database"})
    rows: list[dict[str, Any]] = []
    for work_id, entry in sorted(records.items()):
        paths = {key: entry[key] for key in REQUIRED_ARTIFACT_KEYS if isinstance(entry.get(key), str)}
        artifact_present = len(paths) == len(REQUIRED_ARTIFACT_KEYS) and any(archive_bundle_complete(base, paths) for base in artifact_roots.read_bases())
        status = str(entry.get("status") or "unknown")
        publication_error = entry.get("publication_error")
        storage = entry.get("artifact_state")
        retained = storage is not None and storage["durability"] == "verified_group_recorded"
        terminal = status == "archived" and (artifact_present or retained) and publication_error is None
        if publication_error is not None:
            diagnostics.append({"code": str(publication_error), "category": "database"})
        elif status == "archived" and not artifact_present and not retained:
            diagnostics.append({"code": "terminal_missing_artifact", "category": "transcript"})
        rows.append({"work_id": work_id, "category": "defect" if publication_error else ("complete" if terminal else "backlog"), "status": status, "artifact_present": artifact_present, "reclaimed_audio": False, "coverage": entry.get("coverage"), "coverage_short": entry.get("coverage_short"), "cumulative_complete": terminal, "batch_complete": terminal})
        if storage is not None:
            rows[-1]["artifact_state"] = {**storage,
                "locality": "local_complete" if artifact_present else storage["locality"],
                "readiness": "ready" if artifact_present else storage["readiness"]}
    total = len(rows) if scope_available else 0
    complete = sum(1 for row in rows if row["cumulative_complete"])
    state = "unavailable" if not scope_available else ("complete" if complete == total and not diagnostics else "incomplete")
    return {"schema_version": SCHEMA_VERSION, "scope": scope, "denominator": {"unit": "work_items", "count": total if scope_available else None, "state": "available" if scope_available else "unavailable", "source": "workflow_database"}, "cumulative": {"unit": "work_items", "complete": complete, "total": total, "state": state}, "batch": {"unit": "work_items", "complete": complete, "total": total, "state": state}, "evidence": {"workflow_database": {"state": "available"}, "workflow_jobs": {"state": "available"}}, "rows": rows, "diagnostics": diagnostics}


@dataclass(frozen=True)
class CoverageReport:
    data: dict[str, Any]

    @classmethod
    def build(cls, archive_root: str | Path, *, scope: str | None = None, policy: Any = None, artifact_roots: ArtifactRoots | None = None) -> "CoverageReport":
        del policy
        root = ArtifactRoots.of(archive_root).archive_root
        roots = artifact_roots if artifact_roots is not None else ArtifactRoots.of(root)
        return cls(_build_workflow_data(root, scope=scope, artifact_roots=roots))

    def to_json(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def to_csv(self) -> str:
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        denominator, cumulative, batch = self.data["denominator"], self.data["cumulative"], self.data["batch"]
        evidence = ";".join(f"{key}:{value['state']}" for key, value in sorted(self.data["evidence"].items()))
        diagnostics = ";".join(f"{item['code']}:{item.get('category', '')}" for item in self.data["diagnostics"])
        for row in self.data["rows"] or [{}]:
            writer.writerow({"schema_version": self.data["schema_version"], "scope": self.data["scope"] or "", "denominator_unit": denominator["unit"], "denominator_count": denominator["count"] if denominator["count"] is not None else "", "denominator_state": denominator["state"], "denominator_source": denominator["source"], "cumulative_unit": cumulative["unit"], "cumulative_complete": cumulative["complete"], "cumulative_total": cumulative["total"], "cumulative_state": cumulative["state"], "batch_unit": batch["unit"], "batch_complete": batch["complete"], "batch_total": batch["total"], "batch_state": batch["state"], "work_id": row.get("work_id", ""), "category": row.get("category", "summary"), "status": row.get("status", ""), "artifact_present": row.get("artifact_present", ""), "reclaimed_audio": row.get("reclaimed_audio", ""), "coverage": row.get("coverage", ""), "coverage_short": row.get("coverage_short", ""), "evidence_summary": evidence, "diagnostic_summary": diagnostics})
        return output.getvalue()
