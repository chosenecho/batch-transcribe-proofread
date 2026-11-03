"""Posthoc quality over frozen cues and explicitly exported producer evidence."""
from __future__ import annotations

import json
from pathlib import Path

from bili_asr.asr.admission import assess_output

PRODUCER_FILE = "producer-quality-posthoc.json"


def producer_quality(folder: Path, prepared: dict) -> dict:
    """Missing or ambiguous producer evidence remains unknown, never a pass."""
    result = {"producer_admission": "unknown", "reason": "producer_evidence_not_in_sample"}
    path = folder / PRODUCER_FILE
    if not path.exists():
        return result
    export = json.loads(path.read_bytes())
    base = prepared["snapshot"]["base"]
    if (export["transcript_id"] != base["transcript_id"]
            or export["content_sha256"] != base["content_sha256"]):
        raise ValueError("producer quality evidence transcript mismatch")
    result["producer_check"] = {"basis": "posthoc_exported_producer_evidence", "observed_at": export["observed_at"]}
    rows = export["rows"]
    evidence_rows = rows.get("transcript_asr_evidence", [])
    # Retain old policy assessments as evidence, but use the workflow reader
    # for precedence decisions rather than approximating it in this importer.
    if len(evidence_rows) != 1 or rows.get("workflow_quality_assessments"):
        result["reason"] = "producer_requires_workflow_assessment"
        return result
    row = evidence_rows[0]
    if row["transcript_id"] != base["transcript_id"] or row["video_part_id"] != prepared["snapshot"]["video_part_id"]:
        raise ValueError("producer run transcript mismatch")
    evidence = json.loads(row["evidence_json"])
    if type(evidence.get("schema_version")) is not int or evidence["schema_version"] != 1:
        result["reason"] = "unsupported_producer_evidence"
        return result
    for key in ("run_id", "transcript_id", "video_part_id"):
        if key in evidence and evidence[key] != row[key]:
            raise ValueError("embedded producer evidence identity mismatch")
    coverage_rows = [c for c in rows.get("transcript_coverage_attestations", []) if c["run_id"] == row["run_id"]
                     and c["transcript_id"] == base["transcript_id"]]
    if len(coverage_rows) != 1:
        result["reason"] = "producer_coverage_missing_or_ambiguous"
        return result
    coverage = dict(coverage_rows[0])
    if coverage["video_part_id"] != prepared["snapshot"]["video_part_id"]:
        raise ValueError("producer coverage part mismatch")
    if type(coverage["coverage_short"]) is not int or coverage["coverage_short"] not in (0, 1):
        raise ValueError("invalid exported producer coverage flag")
    coverage["coverage_short"] = bool(coverage["coverage_short"])
    cues = [{"start": s["start_ms"] / 1000, "end": s["end_ms"] / 1000, "text": s["text"]}
            for s in base["segments"]]
    measured = assess_output(cues, duration_ms=evidence.get("audio", {}).get("duration_ms"),
                             coverage=coverage, diagnostics=evidence.get("diagnostics"))
    result.update(producer_admission=measured["status"], reason="posthoc_measured_admission",
                  producer_check=result["producer_check"] | {"run_id": row["run_id"], "assessment": measured,
                                                             "coverage_attestation": coverage})
    return result
