"""Read-only verification of SQLite workflow publications."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .artifact_root import ArtifactRoots
from .archive import archive_bundle_complete
from .artifacts import REQUIRED_ARTIFACT_KEYS

DEFECT_CATEGORY = "defect"
BACKLOG_CATEGORY = "backlog"
RETRYABLE_INCOMPLETE = "retryable_incomplete"
BACKLOG_CODES = frozenset({RETRYABLE_INCOMPLETE})
MISSING_TRANSCRIPT = "missing_transcript"


@dataclass(frozen=True)
class IntegrityDefect:
    work_id: str
    code: str

    @property
    def category(self) -> str:
        return BACKLOG_CATEGORY if self.code in BACKLOG_CODES else DEFECT_CATEGORY

    def to_dict(self) -> dict[str, object]:
        return {"work_id": self.work_id, "code": self.code, "category": self.category}


@dataclass
class IntegrityReport:
    checked: int = 0
    defects: list[IntegrityDefect] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    authoritative: bool = True

    @property
    def defect_count(self) -> int:
        return sum(1 for defect in self.defects if defect.category == DEFECT_CATEGORY)

    @property
    def backlog_count(self) -> int:
        return sum(1 for defect in self.defects if defect.category == BACKLOG_CATEGORY)

    def to_dict(self) -> dict[str, object]:
        return {"checked": self.checked, "defect_count": self.defect_count, "backlog_count": self.backlog_count, "defects": [item.to_dict() for item in self.defects], "diagnostics": list(self.diagnostics), "authoritative": self.authoritative}


class IntegrityVerifier:
    """Verify the current workflow projection and its published bundle."""

    def verify(self, archive_root: Path, *, scope: str | None = None, policy: Any = None, artifact_roots: ArtifactRoots | None = None) -> IntegrityReport:
        del policy
        root = ArtifactRoots.of(archive_root).archive_root
        if not (root / "archive.db").is_file():
            return IntegrityReport(authoritative=False, diagnostics=["structural_input_error"])
        from bili_asr.services.workflow_projection import workflow_records

        roots = artifact_roots if artifact_roots is not None else ArtifactRoots.of(root)
        records = workflow_records(root, artifact_roots=roots, verify_artifacts=False)
        if scope in {"pending", "incomplete"}:
            records = {key: value for key, value in records.items() if value.get("status") != "archived"}
        elif scope:
            selected: dict[str, dict[str, Any]] = {}
            for token in scope.replace(",", " ").split():
                matches = {key: value for key, value in records.items() if key == token or value.get("bvid") == token}
                if not matches:
                    return IntegrityReport(authoritative=True, diagnostics=["unknown_scope"])
                selected.update(matches)
            records = selected
        report = IntegrityReport(authoritative=True, checked=len(records))
        for work_id, entry in sorted(records.items()):
            paths = {key: entry[key] for key in REQUIRED_ARTIFACT_KEYS if isinstance(entry.get(key), str)}
            complete = len(paths) == len(REQUIRED_ARTIFACT_KEYS) and any(archive_bundle_complete(base, paths) for base in roots.read_bases())
            status = str(entry.get("status") or "")
            publication_error = entry.get("publication_error")
            storage = entry.get("artifact_state")
            retained = storage is not None and storage["durability"] == "verified_group_recorded"
            if publication_error is not None:
                report.defects.append(IntegrityDefect(work_id, str(publication_error)))
            elif status == "archived" and not complete and not retained:
                report.defects.append(IntegrityDefect(work_id, "terminal_missing_artifact"))
            elif status in {"subtitle_done", "asr_done"} and not complete:
                report.defects.append(IntegrityDefect(work_id, RETRYABLE_INCOMPLETE))
        return report
