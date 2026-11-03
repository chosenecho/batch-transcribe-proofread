"""Explicit ASR replacement; never clear holds or rewrite failed attempts."""

from __future__ import annotations

import json
import time

from bili_asr.asr.admission import POLICY_VERSION
from bili_asr.storage.asr_quality import assess_transcript, read_assessment


def repair_asr(workflow, *, profile, reason: str, job_id: str | None = None,
               transcript_id: int | None = None) -> dict:
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 512 or any(ord(ch) < 32 for ch in reason):
        raise ValueError("repair reason must be a nonempty single line of at most 512 characters")
    connection = workflow.connection
    with workflow.commit_guard.transaction():
        if (job_id is None) == (transcript_id is None):
            raise ValueError("ASR repair requires exactly one job or transcript")
        if transcript_id is not None:
            quality = assess_transcript(connection, transcript_id)
            if quality["status"] == "usable":
                raise ValueError("ASR output is already usable; quality repair is unnecessary")
            report = workflow.enqueue_asr_repair(repair_of_job_id=None, video_part_id=quality["video_part_id"],
                profile=profile, reference_transcript_id=None, repair_of_transcript_id=transcript_id)
            _record_repair(connection, quality, report, reason)
            return report
        row = connection.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None or row["kind"] != "asr" or row["status"] not in {"failed", "succeeded"}:
            raise ValueError("ASR repair requires a terminal failed or succeeded ASR job")
        attempt = connection.execute(
            "SELECT result_json FROM workflow_attempts WHERE job_id=? ORDER BY rowid DESC LIMIT 1", (job_id,)).fetchone()
        result = {} if attempt is None or attempt["result_json"] is None else json.loads(attempt["result_json"])
        diagnostic = result.get("diagnostic", result)
        if diagnostic.get("run_id"):
            quality = read_assessment(connection, run_id=diagnostic["run_id"], video_part_id=row["video_part_id"])
        else:
            quality = None
        if quality is None and result.get("transcript_id") is not None:
            quality = assess_transcript(connection, int(result["transcript_id"]), run_id=result.get("run_id"))
        if quality is None:
            raise ValueError("ASR repair requires preserved completed inference evidence")
        if quality["status"] == "usable":
            raise ValueError("ASR output is already usable; quality repair is unnecessary")
        previous = workflow.profile(row["profile_id"])
        if quality["status"] != "unknown" and profile.canonical() == previous.canonical():
            raise ValueError("unusable output requires a changed model/alignment profile; identical retries cannot repair it")
        payload = json.loads(row["payload_json"])
        configs = [json.loads(item["payload_json"])["editorial_config"] for item in connection.execute(
            "SELECT j.payload_json FROM workflow_jobs j JOIN workflow_job_dependencies d ON d.job_id=j.job_id "
            "WHERE d.prerequisite_job_id=? AND j.kind='proofread' AND json_type(j.payload_json,'$.editorial_config')='object'",
            (job_id,))]
        if len(configs) > 1 and any(config != configs[0] for config in configs[1:]):
            raise ValueError("ambiguous editorial configuration; plan the replacement without implicit selection")
        report = workflow.enqueue_asr_repair(repair_of_job_id=job_id, video_part_id=row["video_part_id"],
            profile=profile, reference_transcript_id=payload.get("reference_transcript_id"),
            editorial_config=configs[0] if configs else None)
        _record_repair(connection, quality, report, reason)
        return report


def _record_repair(connection, quality, report, reason):
    if report["created"]:
        details = {"action": "explicit_asr_repair", "reason": reason.strip(),
                   "previous_quality": quality, **report}
        connection.execute(
            "INSERT INTO workflow_quality_assessments(video_part_id,source_kind,score,assessor,details_json,assessed_at) "
            "VALUES (?,'asr-local',0,?,?,?)",
            (quality["video_part_id"], POLICY_VERSION + "/repair", json.dumps(details, ensure_ascii=False, allow_nan=False), int(time.time())))
