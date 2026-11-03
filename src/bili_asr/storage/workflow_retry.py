"""Scalar retry snapshots and transaction-fenced persistence operations."""

from __future__ import annotations


def retry_candidates(connection, *, part_ids=None, job_ids=None, kinds=None, failed_only=True):
    where, values = [], []
    for column, selector in (("video_part_id", part_ids), ("job_id", job_ids), ("kind", kinds)):
        if selector is not None:
            selector = tuple(dict.fromkeys(getattr(item, "value", item) for item in selector))
            if not selector:
                return []
            where.append(f"j.{column} IN (" + ",".join("?" for _ in selector) + ")")
            values.extend(selector)
    if failed_only:
        where.append("j.status='failed'")
    query = (
        "SELECT j.job_id,j.kind,j.status,j.last_error_code,j.attempt_count,j.available_at,j.updated_at,"
        "a.attempt_id AS latest_attempt_id,a.finished_at AS latest_finished_at,a.error_code AS latest_attempt_error,a.outcome AS latest_attempt_outcome "
        "FROM workflow_jobs j LEFT JOIN workflow_attempts a ON a.rowid="
        "(SELECT x.rowid FROM workflow_attempts x WHERE x.job_id=j.job_id ORDER BY x.rowid DESC LIMIT 1)")
    if where:
        query += " WHERE " + " AND ".join(where)
    return [dict(row) for row in connection.execute(query + " ORDER BY j.job_id", values)]


def retry_replacements(connection, job_ids):
    """One scalar scan; never project frozen payloads into Python memory."""
    selected = set(job_ids)
    result = {job_id: [] for job_id in selected}
    for row in connection.execute(
        "SELECT job_id,kind,status,json_extract(payload_json,'$.repair_of_job_id') AS repair_of_job_id "
        "FROM workflow_jobs WHERE kind IN ('proofread','render_document') "
        "AND json_type(payload_json,'$.repair_of_job_id')='text'"):
        if row["repair_of_job_id"] in selected:
            result[row["repair_of_job_id"]].append({name: row[name] for name in ("job_id", "kind", "status")})
    return result


def requeue_decided_job(connection, snapshot, *, now):
    """Only callable inside the use case's writer fence after revalidation."""
    if not connection.in_transaction:
        raise RuntimeError("guarded requeue requires a write transaction")
    cursor = connection.execute(
        "UPDATE workflow_jobs SET status='queued',available_at=?,lease_owner=NULL,lease_expires_at=NULL,"
        "last_error_code=NULL,updated_at=? WHERE job_id=? AND status='failed' AND attempt_count=? AND last_error_code IS ?",
        (now, now, snapshot["job_id"], snapshot["attempt_count"], snapshot["last_error_code"]))
    return int(cursor.rowcount)


def editorial_retry_facts(connection, jobs):
    """Scalar frozen physical-budget projection, never model request/response text.

    Historical inputs did not have a physical budget. For current inputs every
    provider reservation (including unfinished calls) counts, while replay and
    local-precheck rows do not. A stored successful envelope may be replayed
    without sending a new request after the budget has been spent.
    """
    from bili_asr.storage.editorial_call_budget import PHYSICAL_CALL_SQL

    selected = {job['job_id'] for job in jobs if job.get('kind') == 'proofread'}
    if not selected:
        return {}
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('editorial_inputs','editorial_job_inputs','editorial_model_calls','editorial_chunk_results')")}
    if len(tables) != 4:
        return {job_id: {'eligible': True, 'exhausted': False, 'reason': 'physical_budget_not_available'}
                for job_id in selected}
    result = {}
    for offset in range(0, len(selected), 500):
        ids = tuple(sorted(selected))[offset:offset + 500]
        marks = ','.join('?' for _ in ids)
        rows = connection.execute(f"""WITH selected AS (
            SELECT j.job_id,coalesce(b.input_id,json_extract(j.payload_json,'$.input_id')) AS input_id
            FROM workflow_jobs j LEFT JOIN editorial_job_inputs b ON b.job_id=j.job_id
            WHERE j.job_id IN ({marks})
        ), chunks AS (
            SELECT s.job_id,s.input_id,json_extract(i.prepared_json,'$.snapshot.config.request_policy') AS request_policy,
                coalesce(json_extract(i.prepared_json,'$.snapshot.config.max_chunk_regenerations'),0)+1 AS maximum,
                json_extract(c.value,'$.chunk_id') AS chunk_id
            FROM selected s JOIN editorial_inputs i ON i.input_id=s.input_id
            JOIN json_each(i.prepared_json,'$.chunks') c
        ), provider_calls AS (
            SELECT m.rowid AS call_rowid,m.input_id,m.chunk_id,m.error_code,m.response_json IS NOT NULL AS has_response,
                CASE WHEN json_valid(m.response_json) THEN
                    CASE WHEN json_type(m.response_json,'$.choices[0]')='object' THEN
                        CASE json_extract(m.response_json,'$.choices[0].finish_reason')
                            WHEN 'content_filter' THEN 'deepseek_content_filtered'
                            WHEN 'tool_calls' THEN 'deepseek_unsupported_completion'
                            WHEN 'function_call' THEN 'deepseek_unsupported_completion'
                            WHEN 'stop' THEN NULL WHEN 'length' THEN NULL
                            ELSE 'deepseek_output_interrupted' END
                    END END AS termination_code
            FROM editorial_model_calls m JOIN (SELECT DISTINCT input_id FROM selected) s ON s.input_id=m.input_id
            WHERE {PHYSICAL_CALL_SQL}
        ), budgets AS (
            SELECT input_id,chunk_id,count(*) AS consumed,max(call_rowid) AS latest_rowid
            FROM provider_calls GROUP BY input_id,chunk_id
        ) SELECT c.job_id,c.request_policy,c.maximum,c.chunk_id,coalesce(b.consumed,0) AS consumed,
            p.has_response,p.error_code,p.termination_code,r.chunk_id IS NOT NULL AS complete
            FROM chunks c LEFT JOIN budgets b ON b.input_id=c.input_id AND b.chunk_id=c.chunk_id
            LEFT JOIN provider_calls p ON p.call_rowid=b.latest_rowid
            LEFT JOIN editorial_chunk_results r ON r.input_id=c.input_id AND r.chunk_id=c.chunk_id""", ids)
        for row in rows:
            gate = result.setdefault(row['job_id'], {'eligible': True, 'exhausted': False,
                                                    'reason': 'remaining_physical_budget'})
            if not row['complete'] and row['termination_code'] is not None:
                gate.update(eligible=False, exhausted=False, reason=row['termination_code'])
            elif (not row['complete'] and row['request_policy'] == 'deepseek-v2'
                    and not gate['reason'].startswith('deepseek_')
                    and row['consumed'] >= row['maximum']
                    and not (row['has_response'] and row['error_code'] is None)):
                gate.update(eligible=False, exhausted=True, reason='physical_budget_exhausted')
        for job_id in ids:
            result.setdefault(job_id, {'eligible': True, 'exhausted': False, 'reason': 'unfrozen_or_legacy_input'})
    return result
