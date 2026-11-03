"""Explicit optimistic repair of historical subtitle-gated producer graphs."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import time

from bili_asr.storage.job_commit import JobCommitGuard


def _preview(connection, part_ids, *, retry_failed):
    ids = tuple(sorted(set(part_ids)))
    if not ids or len(ids) > 256 or any(type(item) is not int or item < 1 for item in ids):
        raise ValueError("dependency repair requires 1 to 256 explicit positive part IDs")
    marks = ",".join("?" for _ in ids)
    known = {row[0] for row in connection.execute(f"SELECT video_part_id FROM video_parts WHERE video_part_id IN ({marks})", ids)}
    if known != set(ids):
        raise ValueError("dependency repair contains unknown parts")
    jobs = [dict(row) for row in connection.execute(
        f"SELECT job_id,video_part_id,kind,status,attempt_count,updated_at,lease_owner,lease_expires_at,last_error_code "
        f"FROM workflow_jobs WHERE video_part_id IN ({marks}) ORDER BY job_id", ids)]
    edges = [dict(row) for row in connection.execute(
        "SELECT d.job_id,d.prerequisite_job_id,p.kind AS prerequisite_kind,p.video_part_id AS prerequisite_part "
        "FROM workflow_job_dependencies d JOIN workflow_jobs j ON j.job_id=d.job_id "
        "JOIN workflow_jobs p ON p.job_id=d.prerequisite_job_id "
        f"WHERE j.video_part_id IN ({marks}) ORDER BY d.job_id,d.prerequisite_job_id", ids)]
    by_id = {job["job_id"]: job for job in jobs}
    removals = [edge for edge in edges if by_id[edge["job_id"]]["kind"] in {"audio", "asr"}
                and by_id[edge["job_id"]]["status"] in {"queued", "failed"}
                and edge["prerequisite_kind"] == "subtitle"]
    touched_parts = {by_id[edge["job_id"]]["video_part_id"] for edge in removals}
    if any(job["status"] == "running" for job in jobs if job["video_part_id"] in touched_parts):
        raise ValueError("stop running jobs on selected parts before repairing their dependencies")
    if any(edge["prerequisite_part"] != by_id[edge["job_id"]]["video_part_id"] for edge in removals):
        raise ValueError("cross-part dependency requires manual graph review")
    grouped = defaultdict(list)
    for job in jobs:
        grouped[job["video_part_id"]].append(job)
    additions = []
    edge_pairs = {(edge["job_id"], edge["prerequisite_job_id"]) for edge in edges}
    for part in sorted(touched_parts):
        audio = [job for job in grouped[part] if job["kind"] == "audio"]
        if len(audio) != 1:
            raise ValueError("historical producer graph must have exactly one audio job per part")
        for job in grouped[part]:
            if job["kind"] != "asr" or job["status"] in {"succeeded", "cancelled"}:
                continue
            pair = job["job_id"], audio[0]["job_id"]
            if pair not in edge_pairs:
                additions.append({"job_id": pair[0], "prerequisite_job_id": pair[1]})
    retries = [job["job_id"] for job in jobs if retry_failed and job["video_part_id"] in touched_parts
               and job["kind"] in {"audio", "asr"} and job["status"] == "failed"]
    changes = {"remove": [{"job_id": edge["job_id"], "prerequisite_job_id": edge["prerequisite_job_id"]} for edge in removals],
               "add": additions, "retry_failed_job_ids": retries}
    identity = {"part_ids": ids, "jobs": jobs, "edges": edges, "retry_failed": retry_failed, "changes": changes}
    plan_id = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"operation": "repair-producer-dependencies", "applied": False,
            "plan_id": plan_id, "part_ids": list(ids), "changes": changes,
            "retains_attempt_history": True, "cancelled_jobs_remain_terminal": True}


def repair_producer_dependencies(connection, part_ids, *, apply=False, expected_plan_id=None, retry_failed=False):
    """Plan by default; apply rechecks every selected fact under BEGIN IMMEDIATE."""
    if not apply:
        return _preview(connection, part_ids, retry_failed=retry_failed)
    if not isinstance(expected_plan_id, str) or len(expected_plan_id) != 64:
        raise ValueError("applying dependency repair requires its reviewed plan ID")
    with JobCommitGuard(connection).transaction():
        report = _preview(connection, part_ids, retry_failed=retry_failed)
        if report["plan_id"] != expected_plan_id:
            raise ValueError("dependency repair plan is stale; inspect the updated graph first")
        for edge in report["changes"]["remove"]:
            connection.execute("DELETE FROM workflow_job_dependencies WHERE job_id=? AND prerequisite_job_id=?",
                               (edge["job_id"], edge["prerequisite_job_id"]))
        for edge in report["changes"]["add"]:
            connection.execute("INSERT INTO workflow_job_dependencies VALUES (?,?)",
                               (edge["job_id"], edge["prerequisite_job_id"]))
        now = int(time.time())
        for job_id in report["changes"]["retry_failed_job_ids"]:
            connection.execute("UPDATE workflow_jobs SET status='queued',available_at=?,updated_at=?,"
                               "lease_owner=NULL,lease_expires_at=NULL,last_error_code=NULL WHERE job_id=? AND status='failed'",
                               (now, now, job_id))
        report["applied"] = True
    return report
