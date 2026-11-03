"""Explicit measured-ASR evidence for synthetic editorial fixture records."""

import json


def measured_fixture_evidence(connection, value):
    """Fixture timestamps define its substituted decoder's actual duration."""
    run_id = f"editorial-fixture-{value.transcript_id}"
    decoded = max(segment.end_ms for segment in value.segments) / 1000
    produced = (max(segment.end_ms for segment in value.segments) - min(segment.start_ms for segment in value.segments)) / 1000
    connection.execute(
        "INSERT INTO acquisition_runs(run_id,kind,selector_kind,selector_target,requested_limit,credential_present,started_at,finished_at,outcome) "
        "VALUES (?,'asr','pending',NULL,1,0,1,2,'complete')", (run_id,))
    connection.execute(
        "INSERT INTO acquisition_attempts(run_id,video_part_id,outcome,error_code,transcript_id,started_at,finished_at) "
        "VALUES (?,?,'stored',NULL,?,1,2)", (run_id, value.video_part_id, value.transcript_id))
    connection.execute(
        "INSERT INTO transcript_coverage_attestations(run_id,video_part_id,transcript_id,decoded_s,produced_s,coverage,coverage_min,coverage_short) "
        "VALUES (?,?,?,?,?,?,.85,?)", (run_id, value.video_part_id, value.transcript_id, decoded, produced, produced / decoded, int(produced / decoded < .85)))
    connection.execute("INSERT INTO transcript_asr_evidence VALUES (?,?,?,1,?)",
        (run_id, value.video_part_id, value.transcript_id,
         json.dumps({"schema_version": 1, "audio": {"duration_ms": round(decoded * 1000)},
                     "diagnostics": {"quality": {"status": "no-known-risk", "flags": []}}})))


def coverage(decoded=4.0, produced=4.0):
    return {"decoded_s": decoded, "produced_s": produced, "coverage": produced / decoded,
            "coverage_min": .85, "coverage_short": produced / decoded < .85}


def cue(start=0.0, end=4.0, text="正常完整转录"):
    return {"start": start, "end": end, "text": text}
