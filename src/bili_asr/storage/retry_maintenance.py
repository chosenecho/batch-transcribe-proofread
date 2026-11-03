"""Exact failed-attempt fence for opt-in maintenance; no schema installation."""

from bili_asr.storage.job_commit import JobCommitGuard


def failed_candidates(connection, *, job_ids, part_ids, kinds):
    where, values = ["j.status = 'failed'"], []
    for column, selector in (("j.job_id", job_ids), ("j.video_part_id", part_ids), ("j.kind", kinds)):
        if selector:
            where.append(column + " IN (" + ",".join("?" for _ in selector) + ")")
            values.extend(selector)
    return [dict(row) for row in connection.execute(
        "SELECT j.job_id,j.kind,j.video_part_id,j.attempt_count,j.last_error_code,j.available_at,"
        "a.attempt_id,a.outcome,a.error_code,a.finished_at FROM workflow_jobs j "
        "JOIN workflow_attempts a ON a.rowid = (SELECT MAX(rowid) FROM workflow_attempts WHERE job_id=j.job_id) "
        "WHERE " + " AND ".join(where) + " ORDER BY j.job_id", values)]


def requeue_exact_failure(connection, candidate: dict, now: int, *, eligible) -> bool:
    with JobCommitGuard(connection).transaction():
        if not eligible():
            return False
        result = connection.execute(
            "UPDATE workflow_jobs SET status='queued', available_at=?, updated_at=?, "
            "lease_owner=NULL,lease_expires_at=NULL,last_error_code=NULL "
            "WHERE job_id=? AND status='failed' AND attempt_count=? AND last_error_code=? "
            "AND EXISTS (SELECT 1 FROM workflow_attempts a WHERE a.attempt_id=? AND a.job_id=workflow_jobs.job_id "
            "AND a.outcome='failed' AND a.error_code=? AND a.finished_at=? "
            "AND a.rowid=(SELECT MAX(rowid) FROM workflow_attempts WHERE job_id=workflow_jobs.job_id))",
            (now, now, candidate["job_id"], candidate["attempt_count"], candidate["last_error_code"],
             candidate["attempt_id"], candidate["error_code"], candidate["finished_at"]))
        return result.rowcount == 1
