"""Preserve inference and admission evidence atomically under the job fence."""

from __future__ import annotations

from contextlib import contextmanager

from bili_asr.asr.admission import assess_output, evidence_value
from bili_asr.storage.asr_quality import record_assessment
from bili_asr.storage.models import TranscriptSegmentRecord
from bili_asr.storage.transcripts import TranscriptRepository


def preserve_inference(workflow, job, *, run_id, segments, coverage, asr_evidence,
                       language, model_name, model_revision, started_at, finished_at):
    original = list(segments)
    quality = assess_output(original, duration_ms=asr_evidence["audio"].get("duration_ms"),
                            coverage=coverage, diagnostics=asr_evidence["diagnostics"])
    captured = {"run_id": run_id, "transcript_id": None, "job_id": job.job_id,
                "attempt_count": job.attempt_count, "quality": quality,
                "original_segments": original, "coverage": coverage,
                "asr_evidence": asr_evidence, "started_at": started_at,
                "finished_at": finished_at}
    if "repair_of_job_id" in job.payload:
        captured["repair_of_job_id"] = job.payload["repair_of_job_id"]
    if "repair_of_transcript_id" in job.payload:
        captured["repair_of_transcript_id"] = job.payload["repair_of_transcript_id"]
    assessment_id = None

    @contextmanager
    def result_transaction():
        nonlocal assessment_id
        with workflow.owned_transaction(job):
            yield
            row = workflow.connection.execute(
                "SELECT transcript_id FROM acquisition_attempts WHERE run_id=? AND video_part_id=?",
                (run_id, job.video_part_id)).fetchone()
            captured["transcript_id"] = row["transcript_id"]
            assessment_id = record_assessment(workflow.connection, video_part_id=job.video_part_id, details=captured)

    repository = TranscriptRepository(workflow.connection, write_transaction=result_transaction)
    stored = None
    if quality["representable"]:
        records = tuple(TranscriptSegmentRecord(round(cue["start"] * 1000), round(cue["end"] * 1000), cue["text"])
                        for cue in original)
        stored = repository.record_local_transcript(
            run_id=run_id, video_part_id=job.video_part_id, language=language, segments=records,
            model_name=model_name, model_revision=model_revision, started_at=started_at,
            finished_at=finished_at, created_at=finished_at,
            coverage=coverage if quality["coverage"] not in {"invalid", "unknown"} else None,
            asr_evidence=evidence_value({**asr_evidence, "admission": quality}))
    else:
        # The canonical transcript schema cannot represent NaN, zero-length
        # or empty cues. The complete original inference stays in assessment
        # evidence instead of silently dropping the offending rows.
        repository.record_subtitle_attempt(
            run_id=run_id, video_part_id=job.video_part_id, outcome="failed",
            error_code="asr_quality_unusable", started_at=started_at, finished_at=finished_at)
    return stored, {**quality, "assessment_id": assessment_id, "run_id": run_id,
                    "video_part_id": job.video_part_id, "transcript_id": captured["transcript_id"]}
