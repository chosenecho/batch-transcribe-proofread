"""Every downstream consumer of a part, including jobs without a part column."""
from __future__ import annotations

_PARTS = """WITH RECURSIVE consumer_parts(job_id,video_part_id) AS (
    SELECT job_id,video_part_id FROM workflow_jobs WHERE video_part_id IS NOT NULL
    UNION
    SELECT d.job_id,p.video_part_id FROM workflow_job_dependencies d
    JOIN consumer_parts p ON d.prerequisite_job_id=p.job_id
) """
_FROM_PART = """WITH RECURSIVE consumers(job_id) AS (
    SELECT job_id FROM workflow_jobs WHERE video_part_id=?
    UNION
    SELECT d.job_id FROM workflow_job_dependencies d JOIN consumers c ON d.prerequisite_job_id=c.job_id
) """


def consumer_part_rows(connection):
    # UNION bounds repeated edges and cycles by the finite job/part relation.
    return connection.execute(_PARTS + "SELECT p.job_id,p.video_part_id,j.status,j.updated_at,"
        "EXISTS(SELECT 1 FROM workflow_attempts a WHERE a.job_id=j.job_id AND a.outcome='running') "
        "FROM consumer_parts p JOIN workflow_jobs j ON j.job_id=p.job_id")


def consumer_job_ids(connection, part_id):
    return [row[0] for row in connection.execute(_FROM_PART + "SELECT job_id FROM consumers", (part_id,))]


def has_live_consumer(connection, part_id):
    return connection.execute(_FROM_PART + "SELECT 1 FROM consumers c JOIN workflow_jobs j ON j.job_id=c.job_id "
        "WHERE (j.status!='succeeded' OR EXISTS(SELECT 1 FROM workflow_attempts a "
        "WHERE a.job_id=j.job_id AND a.outcome='running')) LIMIT 1", (part_id,)).fetchone() is not None


def latest_completion(connection, object_id):
    return connection.execute("WITH RECURSIVE consumers(job_id) AS ("
        "SELECT j.job_id FROM workflow_jobs j JOIN part_audio_objects b ON b.video_part_id=j.video_part_id "
        "JOIN audio_objects a ON a.audio_id=b.audio_id WHERE a.sha256=? UNION "
        "SELECT d.job_id FROM workflow_job_dependencies d JOIN consumers c ON d.prerequisite_job_id=c.job_id) "
        "SELECT MAX(j.updated_at) FROM consumers c JOIN workflow_jobs j ON j.job_id=c.job_id", (object_id,)).fetchone()[0] or 0
