"""Bounded read-side queries for ASR performance reports."""

from __future__ import annotations

import sqlite3


def performance_attempts(connection: sqlite3.Connection, start: int, end: int, limit: int):
    rows = connection.execute("""
        SELECT a.attempt_id, a.job_id, a.started_at, a.finished_at, a.outcome,
               p.config_sha256, j.status AS job_status,
               CASE WHEN e.run_id IS NULL THEN NULL
                    WHEN NOT json_valid(e.evidence_json) THEN '{}'
                    WHEN json_type(e.evidence_json, '$.schema_version') IS NOT 'integer' THEN '{}'
                    ELSE json_object(
                        'schema_version', json_extract(e.evidence_json, '$.schema_version'),
                        'audio', json_extract(e.evidence_json, '$.audio'),
                        'runtime_binding', json_extract(e.evidence_json, '$.runtime_binding'),
                        'diagnostics', json_object(
                            'execution_policy', json_extract(e.evidence_json, '$.diagnostics.execution_policy'),
                            'passes', CASE WHEN json_type(e.evidence_json, '$.diagnostics.passes')='array'
                                AND NOT EXISTS (SELECT 1 FROM json_each(e.evidence_json, '$.diagnostics.passes') invalid_pass
                                                WHERE invalid_pass.type!='object')
                                THEN (SELECT json_group_array(json_object(
                                         'prefetch', json_extract(pass.value, '$.prefetch')))
                                      FROM json_each(e.evidence_json, '$.diagnostics.passes') pass
                                      WHERE pass.type='object') END
                        )
                    ) END AS evidence_json,
               EXISTS(SELECT 1 FROM workflow_attempts previous
                      WHERE previous.job_id=a.job_id
                        AND (previous.started_at<a.started_at
                             OR (previous.started_at=a.started_at AND previous.rowid<a.rowid))) AS is_retry
        FROM workflow_attempts a
        JOIN workflow_jobs j ON j.job_id=a.job_id AND j.kind='asr'
        LEFT JOIN workflow_asr_profiles p ON p.profile_id=j.profile_id
        LEFT JOIN transcript_asr_evidence e
          ON e.video_part_id=j.video_part_id
         AND e.run_id=CASE WHEN json_valid(a.result_json)
                          THEN json_extract(a.result_json, '$.run_id') END
        WHERE a.started_at < ? AND (a.finished_at IS NULL OR a.finished_at > ?
              OR (a.started_at=a.finished_at AND a.started_at>=?))
        ORDER BY a.started_at, a.attempt_id LIMIT ?
    """, (end, start, start, limit + 1)).fetchall()
    if len(rows) > limit:
        raise ValueError("ASR report attempt limit exceeded; narrow the window or increase --max-attempts")
    return rows


def acquisition_counts(connection: sqlite3.Connection, start: int, end: int) -> dict:
    rows = connection.execute("""
        SELECT ar.outcome, COUNT(*) AS count
        FROM acquisition_runs ar
        WHERE ar.kind='asr' AND ar.started_at < ?
          AND (ar.finished_at IS NULL OR ar.finished_at > ?)
        GROUP BY ar.outcome
    """, (end, start)).fetchall()
    return {row["outcome"]: row["count"] for row in rows}
