"""Explicit replacement jobs for failed proofreading; old evidence is immutable."""

from __future__ import annotations

import json

from bili_asr.editorial import EditorialConfig
from bili_asr.editorial_quality import check_source_quality
from bili_asr.storage.editorial import EditorialRepository


def repair_proofread(workflow, *, job_id: str, config: EditorialConfig,
                     base_transcript_id: int | None = None) -> dict:
    connection = workflow.connection
    # Fences inspection and enqueue against a concurrent retry/claim.
    with workflow.commit_guard.transaction():
        job = connection.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (job_id,)).fetchone()
        if job is None or job["kind"] != "proofread" or job["status"] != "failed":
            raise ValueError("repair requires an existing failed proofread job")
        row = connection.execute(
            "SELECT input_id FROM editorial_job_inputs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            payload = json.loads(job["payload_json"])
            old_input = payload.get("input_id")
            if old_input is None:
                raise ValueError("failed job has no frozen input to repair")
        else:
            old_input = row["input_id"]
        editorial = EditorialRepository(connection, commit_guard=workflow.commit_guard)
        previous = editorial.load_input(old_input)["snapshot"]
        base_id = previous["base"]["transcript_id"] if base_transcript_id is None else base_transcript_id
        base = editorial.read_source(base_id)
        if base.video_part_id != job["video_part_id"]:
            raise ValueError("replacement transcript belongs to another part")
        reference_id = None if previous["reference"] is None else previous["reference"]["transcript_id"]
        prepared = editorial.build_input(base_id, reference_id, config)
        check_source_quality(prepared, connection=connection)
        if prepared["input_id"] == old_input:
            raise ValueError("repair must change prompt, configuration or transcript; use retry for identical input")
        editorial.store_input(prepared)
        proof, render, created, _ = workflow.enqueue_editorial(
            video_part_id=job["video_part_id"], input_id=prepared["input_id"], repair_of_job_id=job_id)
        return {"repair_of_job_id": job_id, "previous_input_id": old_input,
                "input_id": prepared["input_id"], "job_id": proof,
                "render_job_id": render, "chunks": len(prepared["chunks"]), "created": created}
