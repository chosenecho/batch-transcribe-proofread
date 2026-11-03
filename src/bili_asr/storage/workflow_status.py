"""Set-based, read-only facts for workflow failure reports.

No payload or model response crosses this projection. SQLite selects only the
bounded diagnostic envelope; the service validates that envelope again.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def read_snapshot(connection: sqlite3.Connection) -> Iterator[None]:
    owned = not connection.in_transaction
    if owned:
        connection.execute("BEGIN")
    try:
        yield
    finally:
        if owned:
            connection.rollback()


_CANCELLED = """WITH RECURSIVE cancellation_descendants(job_id) AS (
    SELECT job_id FROM workflow_jobs WHERE status='cancelled'
    UNION SELECT d.job_id FROM workflow_job_dependencies d
        JOIN cancellation_descendants c ON c.job_id=d.prerequisite_job_id
) """
_REPAIRS = _CANCELLED.rstrip() + """, repair_counts AS (
    SELECT json_extract(payload_json,'$.repair_of_job_id') AS original_job_id,count(*) AS total,
        sum(CASE WHEN status IN ('queued','running','succeeded') THEN 1 ELSE 0 END) AS active
    FROM workflow_jobs WHERE json_valid(payload_json)
        AND json_type(payload_json,'$.repair_of_job_id')='text'
    GROUP BY original_job_id
) """
_FACTS = """SELECT j.job_id,j.kind,j.status,j.video_part_id,j.attempt_count,
    j.last_error_code AS error_code,j.available_at,j.created_at,j.updated_at,
    (SELECT count(*) FROM workflow_job_dependencies d WHERE d.job_id=j.job_id) AS prerequisite_count,
    (SELECT count(*) FROM workflow_job_dependencies d JOIN workflow_jobs p ON p.job_id=d.prerequisite_job_id
        WHERE d.job_id=j.job_id AND p.status<>'succeeded') AS blocker_count,
    (SELECT count(*) FROM workflow_job_dependencies d JOIN workflow_jobs p ON p.job_id=d.prerequisite_job_id
        WHERE d.job_id=j.job_id AND p.status='cancelled') AS cancelled_blocker_count,
    EXISTS (SELECT 1 FROM cancellation_descendants c WHERE c.job_id=j.job_id) AS has_cancelled_ancestor,
    coalesce(r.total,0) AS replacement_count,
    p.bvid,p.page_index,a.worker_id,a.outcome,a.started_at,a.finished_at,
    a.error_code AS attempt_error_code,
    CASE WHEN json_valid(a.result_json) AND length(CAST(a.result_json AS BLOB))<=32768
        THEN CASE WHEN json_type(a.result_json,'$.diagnostic')='object'
            THEN json_extract(a.result_json,'$.diagnostic') END END AS diagnostic_json,
    CASE WHEN a.result_json IS NULL THEN 'missing'
        WHEN NOT json_valid(a.result_json) OR length(CAST(a.result_json AS BLOB))>32768 THEN 'unavailable'
        WHEN json_type(a.result_json,'$.diagnostic')='object' THEN 'stored'
        ELSE 'missing' END AS diagnostic_state
    FROM workflow_jobs j LEFT JOIN video_parts p ON p.video_part_id=j.video_part_id
    LEFT JOIN workflow_attempts a ON a.rowid=(SELECT a2.rowid FROM workflow_attempts a2
        WHERE a2.job_id=j.job_id ORDER BY a2.rowid DESC LIMIT 1)
    LEFT JOIN repair_counts r ON r.original_job_id=j.job_id """


def counts(connection: sqlite3.Connection, *, since: int | None, until: int) -> dict:
    states = {row['status']: int(row['count']) for row in connection.execute(
        "SELECT status,count(*) AS count FROM workflow_jobs GROUP BY status")}
    failed = int(connection.execute(
        "SELECT count(*) FROM workflow_attempts WHERE outcome='failed'").fetchone()[0])
    window = int(connection.execute(
        "SELECT count(*) FROM workflow_attempts WHERE outcome='failed' AND finished_at>=? AND finished_at<?",
        (0 if since is None else since, until)).fetchone()[0])
    blocked = int(connection.execute(_CANCELLED + """SELECT count(*) FROM workflow_jobs j
        JOIN cancellation_descendants c ON c.job_id=j.job_id WHERE j.status='queued'""").fetchone()[0])
    return {'status_counts': states, 'current_failed_jobs': states.get('failed', 0),
            'historical_failed_attempts': failed, 'failures_since': window,
            'blocked_by_cancelled': blocked}


def failure_groups(connection: sqlite3.Connection, *, since: int | None, until: int) -> list[dict]:
    return [dict(row) for row in connection.execute("""WITH facts AS (
        SELECT kind,last_error_code AS error_code,1 AS current_jobs,0 AS attempts,0 AS window_failures,
            coalesce((SELECT a.finished_at FROM workflow_attempts a
                WHERE a.job_id=j.job_id AND a.outcome='failed' ORDER BY a.rowid DESC LIMIT 1),
                updated_at) AS occurred FROM workflow_jobs j WHERE status='failed'
        UNION ALL
        SELECT j.kind,a.error_code,0,1,CASE WHEN a.finished_at>=? AND a.finished_at<? THEN 1 ELSE 0 END,
            a.finished_at FROM workflow_attempts a JOIN workflow_jobs j ON j.job_id=a.job_id
            WHERE a.outcome='failed')
        SELECT kind,error_code,sum(current_jobs) AS current_failed_jobs,
            sum(attempts) AS historical_failed_attempts,sum(window_failures) AS failures_since,
            max(occurred) AS last_occurrence FROM facts GROUP BY kind,error_code ORDER BY kind,error_code""",
        (0 if since is None else since, until))]


def failed_facts(connection: sqlite3.Connection):
    """Stream scalar decision inputs, never model data or all jobs in memory."""
    return connection.execute(_REPAIRS + """SELECT j.job_id,j.kind,j.status,j.attempt_count,
        j.last_error_code AS error_code,j.available_at,j.updated_at,
        (SELECT finished_at FROM workflow_attempts a WHERE a.job_id=j.job_id
            ORDER BY a.rowid DESC LIMIT 1) AS latest_finished_at,
        coalesce(r.active,0)>0 AS has_replacement
        FROM workflow_jobs j LEFT JOIN repair_counts r ON r.original_job_id=j.job_id
        WHERE j.status='failed' ORDER BY j.job_id""")


def job_page(connection: sqlite3.Connection, *, failed_only: bool, limit: int, offset: int) -> tuple[int, list[dict]]:
    where = " WHERE j.status='failed'" if failed_only else ""
    total = int(connection.execute("SELECT count(*) FROM workflow_jobs j" + where).fetchone()[0])
    rows = [dict(row) for row in connection.execute(_REPAIRS + _FACTS + where +
        " ORDER BY j.created_at,j.job_id LIMIT ? OFFSET ?", (limit, offset))]
    return total, rows


def replacements(connection: sqlite3.Connection, job_ids: tuple[str, ...]) -> dict[str, list[dict]]:
    if not job_ids:
        return {}
    marks = ','.join('?' for _ in job_ids)
    result: dict[str, list[dict]] = {}
    for row in connection.execute(f"""WITH repairs AS (
        SELECT json_extract(payload_json,'$.repair_of_job_id') AS original_job_id,job_id,kind,status,
            ROW_NUMBER() OVER (PARTITION BY json_extract(payload_json,'$.repair_of_job_id')
                ORDER BY CASE WHEN status IN ('queued','running','succeeded') THEN 0 ELSE 1 END,
                    created_at DESC,job_id DESC) AS recency
        FROM workflow_jobs WHERE json_valid(payload_json)
            AND json_extract(payload_json,'$.repair_of_job_id') IN ({marks}))
        SELECT original_job_id,job_id,kind,status FROM repairs WHERE recency<=100
        ORDER BY original_job_id,recency""", job_ids):
        item = dict(row)
        result.setdefault(item.pop('original_job_id'), []).append(item)
    return result


def prerequisites(connection: sqlite3.Connection, job_ids: tuple[str, ...]) -> dict[str, list[dict]]:
    if not job_ids:
        return {}
    marks = ','.join('?' for _ in job_ids)
    result: dict[str, list[dict]] = {}
    for row in connection.execute(f"""WITH dependencies AS (
        SELECT d.job_id AS dependent_job_id,p.job_id,p.kind,p.status,p.last_error_code AS error_code,
            ROW_NUMBER() OVER (PARTITION BY d.job_id ORDER BY p.job_id) AS ordinal
        FROM workflow_job_dependencies d JOIN workflow_jobs p ON p.job_id=d.prerequisite_job_id
        WHERE d.job_id IN ({marks}))
        SELECT dependent_job_id,job_id,kind,status,error_code FROM dependencies WHERE ordinal<=100
        ORDER BY dependent_job_id,ordinal""", job_ids):
        item = dict(row)
        result.setdefault(item.pop('dependent_job_id'), []).append(item)
    return result


def job_fact(connection: sqlite3.Connection, job_id: str) -> dict:
    row = connection.execute(_REPAIRS + _FACTS + " WHERE j.job_id=?", (job_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown workflow job: {job_id}")
    return dict(row)
