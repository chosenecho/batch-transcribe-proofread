"""Append-only ASR admission evidence in the existing assessment contract.

No core schema or historical transcript is upgraded by a quality read.
The numeric score is a storage carrier, never a semantic-accuracy score.
"""

from __future__ import annotations

import json
import time
from typing import Any

from bili_asr.asr.admission import POLICY_VERSION, assess_output, evidence_value
from bili_asr.workflow_errors import JobExecutionError


def record_assessment(connection, *, video_part_id: int, details: dict[str, Any]) -> int:
    if not connection.in_transaction:
        raise RuntimeError("ASR assessment requires a fenced write transaction")
    cursor = connection.execute(
        "INSERT INTO workflow_quality_assessments(video_part_id,source_kind,score,assessor,details_json,assessed_at) "
        "VALUES (?,'asr-local',?,?,?,?)",
        (video_part_id, 1 if details["quality"]["status"] == "usable" else 0, POLICY_VERSION,
         json.dumps(evidence_value(details), ensure_ascii=False, sort_keys=True, allow_nan=False), int(time.time())))
    return int(cursor.lastrowid)


def read_assessment(connection, *, run_id: str, video_part_id: int, include_evidence: bool = False) -> dict | None:
    row = connection.execute(
        "SELECT assessment_id,details_json FROM workflow_quality_assessments "
        "WHERE video_part_id=? AND assessor=? AND json_valid(details_json) "
        "AND json_extract(details_json,'$.run_id')=? ORDER BY assessment_id DESC LIMIT 1",
        (video_part_id, POLICY_VERSION, run_id)).fetchone()
    if row is None:
        return None
    details = json.loads(row["details_json"])
    result = {"assessment_id": row["assessment_id"], **details["quality"],
              "run_id": details["run_id"], "video_part_id": video_part_id,
              "transcript_id": details["transcript_id"]}
    if include_evidence:
        result["evidence"] = details
    return result


def assess_transcript(connection, transcript_id: int, *, run_id: str | None = None) -> dict:
    """Read current policy over immutable cues and one exact producing run.

    An explicit run is never silently replaced by a newer, healthier run.
    For a direct transcript selection the latest producer is reported and
    its identity included; absent coverage is kept unknown.
    """
    transcript = connection.execute("SELECT * FROM transcripts WHERE transcript_id=?", (transcript_id,)).fetchone()
    if transcript is None or transcript["source_kind"] != "asr-local":
        raise ValueError("quality requires an existing ASR transcript")
    segments = [dict(row) for row in connection.execute(
        "SELECT start_ms,end_ms,text FROM transcript_segments WHERE transcript_id=? ORDER BY ordinal", (transcript_id,))]
    cues = [{"start": row["start_ms"] / 1000, "end": row["end_ms"] / 1000, "text": row["text"]} for row in segments]
    query = ("SELECT e.run_id,e.evidence_json FROM transcript_asr_evidence e "
             "JOIN acquisition_runs r ON r.run_id=e.run_id WHERE e.transcript_id=?")
    parameters = [transcript_id]
    if run_id is not None:
        query += " AND e.run_id=?"
        parameters.append(run_id)
    query += " ORDER BY r.finished_at DESC,r.rowid DESC LIMIT 1"
    row = connection.execute(query, parameters).fetchone()
    evidence = {} if row is None else json.loads(row["evidence_json"])
    valid_evidence = (isinstance(evidence, dict) and type(evidence.get("schema_version")) is int
                      and evidence["schema_version"] == 1)
    if not valid_evidence:
        evidence = {}
    selected_run = run_id if row is None else row["run_id"]
    coverage_row = None if selected_run is None else connection.execute(
        "SELECT * FROM transcript_coverage_attestations WHERE run_id=? AND transcript_id=?",
        (selected_run, transcript_id)).fetchone()
    coverage = None if coverage_row is None else dict(coverage_row)
    if coverage is not None:
        coverage["coverage_short"] = bool(coverage["coverage_short"])
    audio = evidence.get("audio")
    duration = audio.get("duration_ms") if isinstance(audio, dict) else None
    measured = assess_output(cues, duration_ms=duration, coverage=coverage,
                             diagnostics=evidence.get("diagnostics"))
    if not valid_evidence and measured["status"] == "usable":
        measured["status"] = "unknown"
    # Original sub-millisecond and invalid coverage evidence can be stricter
    # than the representable stored cues. Preserve its producing-run verdict.
    original = None if selected_run is None else read_assessment(
        connection, run_id=selected_run, video_part_id=transcript["video_part_id"])
    priority = {"usable": 0, "unknown": 1, "needs-review": 2, "unusable": 3}
    if (original is not None and original["status"] != "usable"
            and priority[original["status"]] >= priority[measured["status"]]):
        measured = {name: value for name, value in original.items()
                    if name not in {"run_id", "video_part_id", "transcript_id", "assessment_id"}}
    return {**measured, "transcript_id": transcript_id, "video_part_id": transcript["video_part_id"],
            "run_id": selected_run, "historical_evidence": row is None,
            "evidence_status": "persisted" if valid_evidence else "missing" if row is None else "unavailable",
            "content_sha256": transcript["content_sha256"]}


def require_admission(report: dict, *, stage: str) -> None:
    if report["status"] == "usable":
        return
    code = "asr_quality_unknown" if report["status"] == "unknown" else "asr_quality_unusable"
    details = {"stage": stage, "policy_version": POLICY_VERSION, "quality_status": report["status"],
               "inference_completed": report["inference_completed"],
               "reason_codes": sorted(report["issue_counts"])[:32]}
    for name in ("assessment_id", "run_id", "video_part_id", "transcript_id"):
        if report.get(name) is not None:
            details[name] = report[name]
    raise JobExecutionError(code, details)
