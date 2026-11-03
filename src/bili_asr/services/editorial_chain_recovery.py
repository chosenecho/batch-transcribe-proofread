"""Explicit reconciliation after one exact ASR replacement has succeeded.

Planning is read-only. Applying the inspected plan creates a new frozen DAG
and retires only never-attempted consumers under the workflow writer fence.
No provider client or worker is composed by this use case.
"""

from __future__ import annotations

import json
import time

from bili_asr.canonical_json import digest
from bili_asr.editorial import EditorialConfig
from bili_asr.editorial_quality import check_source_quality
from bili_asr.services.workflow_retry import load_control_state
from bili_asr.storage.editorial import EditorialRepository
from bili_asr.storage.workflow_status import read_snapshot

POLICY = "editorial-chain-recovery/v1"


def _job(connection, job_id, kind):
    row = connection.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None or row["kind"] != kind:
        raise ValueError("recovery requires an existing " + kind + " job")
    return dict(row)


def _retirable(job):
    return job["attempt_count"] == 0 and (
        job["status"] == "queued" or (
            job["status"] == "cancelled" and job["last_error_code"] == "superseded_by_recovery"))


def _inspect(workflow, proofread_job_id, replacement_asr_job_id, control):
    connection = workflow.connection
    old = _job(connection, proofread_job_id, "proofread")
    if not _retirable(old):
        raise ValueError("recovery only retires never-attempted queued consumers")
    payload = json.loads(old["payload_json"])
    origin = payload.get("asr_job_id")
    if not isinstance(origin, str) or not isinstance(payload.get("editorial_config"), dict):
        raise ValueError("recovery requires the original frozen ASR dependency and editorial policy")
    previous = _job(connection, origin, "asr")
    replacement = _job(connection, replacement_asr_job_id, "asr")
    if (origin == replacement_asr_job_id or previous["status"] != "failed"
            or replacement["status"] != "succeeded"
            or previous["video_part_id"] != old["video_part_id"]
            or replacement["video_part_id"] != old["video_part_id"]):
        raise ValueError("recovery requires an explicitly selected successful replacement for the same part")
    dependencies = [row[0] for row in connection.execute(
        "SELECT prerequisite_job_id FROM workflow_job_dependencies WHERE job_id=?", (proofread_job_id,))]
    if dependencies != [origin]:
        raise ValueError("original proofreading dependencies are ambiguous")
    consumers = [dict(row) for row in connection.execute(
        "SELECT j.* FROM workflow_jobs j JOIN workflow_job_dependencies d ON d.job_id=j.job_id "
        "WHERE d.prerequisite_job_id=? ORDER BY j.job_id", (proofread_job_id,))]
    if (len(consumers) != 1 or consumers[0]["kind"] != "render_document"
            or consumers[0]["video_part_id"] != old["video_part_id"]
            or not _retirable(consumers[0])
            or json.loads(consumers[0]["payload_json"]).get("proofread_job_id") != proofread_job_id):
        raise ValueError("recovery requires one never-attempted render consumer; inspect other chains explicitly")
    render = consumers[0]
    if (old["status"] == "cancelled") != (render["status"] == "cancelled"):
        raise ValueError("partially retired chain requires manual inspection")
    render_dependencies = [row[0] for row in connection.execute(
        "SELECT prerequisite_job_id FROM workflow_job_dependencies WHERE job_id=?", (render["job_id"],))]
    if render_dependencies != [proofread_job_id]:
        raise ValueError("original render dependencies are ambiguous")
    attempt = connection.execute(
        "SELECT attempt_id,result_json FROM workflow_attempts WHERE job_id=? AND outcome='succeeded' "
        "ORDER BY finished_at DESC,rowid DESC LIMIT 1", (replacement_asr_job_id,)).fetchone()
    result = {} if attempt is None or attempt["result_json"] is None else json.loads(attempt["result_json"])
    if (not isinstance(result, dict) or type(result.get("transcript_id")) is not int
            or not isinstance(result.get("run_id"), str) or not result["run_id"]):
        raise ValueError("successful replacement lacks exact transcript and run evidence")
    editorial = EditorialRepository(connection, commit_guard=workflow.commit_guard)
    base = editorial.read_source(result["transcript_id"])
    if base.video_part_id != old["video_part_id"] or base.source_kind != "asr-local":
        raise ValueError("replacement ASR result belongs to another part or producer")
    config = EditorialConfig.from_snapshot(payload["editorial_config"])
    reference = json.loads(previous["payload_json"]).get("reference_transcript_id")
    prepared = editorial.build_input(base.transcript_id, reference, config, reuse_existing=True)
    check_source_quality(prepared, connection=connection, run_id=result["run_id"])
    repair_rows = [dict(row) for row in connection.execute(
        "SELECT * FROM workflow_jobs WHERE kind='proofread' AND json_valid(payload_json) "
        "AND json_extract(payload_json,'$.repair_of_job_id')=?", (proofread_job_id,))]
    matching = [row for row in repair_rows if (
        json.loads(row["payload_json"]).get("input_id") == prepared["input_id"]
        and json.loads(row["payload_json"]).get("replacement_asr_job_id") == replacement_asr_job_id
        and json.loads(row["payload_json"]).get("replacement_attempt_id") == attempt["attempt_id"]
        and json.loads(row["payload_json"]).get("replacement_run_id") == result["run_id"])]
    if len(matching) > 1 or any(row not in matching and row["status"] in {"queued", "running", "succeeded"}
                                for row in repair_rows):
        raise ValueError("an active alternative recovery already exists; inspect it before replacing")
    replacement_proof = matching[0] if matching else None
    replacement_render = None
    if replacement_proof is not None:
        rows = connection.execute(
            "SELECT * FROM workflow_jobs WHERE kind='render_document' AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.repair_of_job_id')=? "
            "AND json_extract(payload_json,'$.proofread_job_id')=?",
            (render["job_id"], replacement_proof["job_id"])).fetchall()
        if len(rows) > 1:
            raise ValueError("ambiguous recorded recovery render; inspect duplicate identities")
        replacement_render = None if not rows else dict(rows[0])
    already = old["status"] == render["status"] == "cancelled"
    if already and (replacement_proof is None or replacement_render is None):
        raise ValueError("retired chain has no complete recorded recovery")
    for consumer, prerequisite in ((replacement_proof, replacement_asr_job_id),
                                   (replacement_render, None if replacement_proof is None else replacement_proof["job_id"])):
        if consumer is None:
            continue
        edges = [row[0] for row in connection.execute(
            "SELECT prerequisite_job_id FROM workflow_job_dependencies WHERE job_id=?", (consumer["job_id"],))]
        if consumer["video_part_id"] != old["video_part_id"] or edges != [prerequisite]:
            raise ValueError("recorded recovery graph differs from the frozen producer chain")
    if not already and replacement_proof is not None and replacement_proof["status"] in {"failed", "cancelled"}:
        raise ValueError("existing recovery is terminal; inspect it without resetting its attempt history")
    affected = [origin, replacement_asr_job_id, proofread_job_id, render["job_id"]]
    affected.extend(row["job_id"] for row in (replacement_proof, replacement_render) if row is not None)
    holds = sorted(set(affected) & control.retry_holds)
    blockers = (["control_state_unknown"] if control.status == "not_provided" else []) + (["retry_hold"] if holds else [])
    report = {
        "policy_version": POLICY, "producer_recovered": True,
        "delivery_recovered": replacement_render is not None and replacement_render["status"] == "succeeded",
        "video_part_id": old["video_part_id"], "original_asr_job_id": origin,
        "replacement_asr_job_id": replacement_asr_job_id, "replacement_attempt_id": attempt["attempt_id"],
        "replacement_run_id": result["run_id"], "base_transcript_id": base.transcript_id,
        "reference_transcript_id": reference, "input_id": prepared["input_id"],
        "consumers": [{"job_id": row["job_id"], "kind": row["kind"], "status": row["status"],
                       "attempt_count": row["attempt_count"], "payload_sha256": digest(row["payload_json"])}
                      for row in (old, render)],
        "replacement_jobs": [{"job_id": row["job_id"], "kind": row["kind"], "status": row["status"]}
                             for row in (replacement_proof, replacement_render) if row is not None],
        "control_state": control.as_dict(), "held_job_ids": holds, "blockers": blockers,
        "allowed": not blockers, "already_applied": already,
        "budget": {"chunks": len(prepared["chunks"]),
                   "physical_calls_max": (len(prepared["chunks"]) * (1 + config.max_chunk_regenerations)
                                          if config.request_policy == "deepseek-v2" else None),
                   "physical_budget_enforced": config.request_policy == "deepseek-v2",
                   "max_output_tokens_per_call": config.max_output_tokens,
                   "estimated_cost": "unknown", "provider_calls_started": 0},
        "execution": {"provider_execution_started": False,
                      "next_action": "review_budget_and_explicitly_start_editorial_workers"},
        "retention": {"old_consumer_job_ids": [proofread_job_id, render["job_id"]],
                      "deletion_authorized": False, "next_action": "inspect_artifact_retention_guards"},
    }
    report["plan_id"] = digest(report)
    return report, prepared, editorial, render["job_id"]


def recover_editorial_chain(workflow, *, proofread_job_id: str, replacement_asr_job_id: str,
                             apply: bool = False, expected_plan_id: str | None = None,
                             control_state_path=None, no_control_state: bool = False) -> dict:
    """Default to a read-only plan; an apply authorizes queuing, never execution."""
    connection = workflow.connection
    if apply and expected_plan_id is None:
        raise ValueError("--apply requires --expected-plan-id from the inspected recovery plan")
    context = workflow.commit_guard.transaction() if apply else read_snapshot(connection)
    with context:
        control = load_control_state(control_state_path, no_control_state=no_control_state, connection=connection)
        report, prepared, editorial, render_id = _inspect(
            workflow, proofread_job_id, replacement_asr_job_id, control)
        if not apply or report["already_applied"]:
            return dict(report, applied=False)
        if report["plan_id"] != expected_plan_id:
            raise ValueError("recovery plan changed; inspect the current plan before applying")
        if not report["allowed"]:
            raise ValueError("recovery is blocked by external retry controls")
        editorial.store_input(prepared)
        proof, render, created, _ = workflow.enqueue_editorial_recovery(
            video_part_id=report["video_part_id"], input_id=prepared["input_id"],
            previous_proofread_job_id=proofread_job_id, previous_render_job_id=render_id,
            replacement_asr_job_id=replacement_asr_job_id,
            replacement_attempt_id=report["replacement_attempt_id"], replacement_run_id=report["replacement_run_id"])
        for job_id in (proofread_job_id, render_id):
            changed = connection.execute(
                "UPDATE workflow_jobs SET status='cancelled',last_error_code='superseded_by_recovery',updated_at=? "
                "WHERE job_id=? AND status='queued' AND attempt_count=0", (int(time.time()), job_id))
            if changed.rowcount != 1:
                raise ValueError("consumer changed during recovery; no jobs were retired")
        after = load_control_state(control_state_path, no_control_state=no_control_state, connection=connection)
        if after != control:
            raise ValueError("retry control state changed during recovery")
        return dict(report, applied=True, created=created, job_id=proof, render_job_id=render)
