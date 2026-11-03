"""Persistent scheduling scope; pause never rewrites retry time or attempt history."""
from collections import Counter
from collections.abc import Iterable
import sqlite3
from typing import Any
import json
import time
from uuid import uuid4

from bili_asr.canonical_json import canonical, digest
from bili_asr.storage.job_commit import JobCommitGuard

from bili_asr.storage.workflow_batch_contract import POLICY, objects, require_batches


def install_batches(connection: sqlite3.Connection) -> dict[str, Any]:
    if connection.in_transaction:
        raise ValueError("batch installation requires its own transaction")
    if require_batches(connection):
        return {"policy": POLICY, "installed": True, "reused": True}
    from bili_asr.storage.database import require_manuscript_schema
    require_manuscript_schema(connection)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        for _, sql in objects().values():
            connection.execute(sql)
        connection.execute("INSERT INTO workflow_batch_contract VALUES(1,1)")
        require_batches(connection, required=True)
    return {"policy": POLICY, "installed": True, "reused": False}


def claim_filter(connection: sqlite3.Connection) -> str:
    if not require_batches(connection):
        return ""
    return """ AND NOT EXISTS (
        SELECT 1 FROM workflow_batch_members bm JOIN workflow_batches b USING(batch_id)
        WHERE bm.job_id=j.job_id AND (b.state='paused' OR
            (SELECT count(*) FROM workflow_batch_members live
             JOIN workflow_jobs w USING(job_id)
             WHERE live.batch_id=b.batch_id AND w.status='running' AND w.lease_expires_at>?)>=b.desired_workers)
    )"""


def require_frozen_member(connection: sqlite3.Connection, job: sqlite3.Row) -> None:
    """A modified payload requires an explicit reviewed batch replacement."""
    row = connection.execute(
        "SELECT b.frozen_config_json FROM workflow_batch_members m "
        "JOIN workflow_batches b USING(batch_id) WHERE m.job_id=?", (job["job_id"],)).fetchone()
    if row is None:
        return
    frozen = {member["job_id"]: member["payload_sha256"] for member in json.loads(row[0])}
    if frozen.get(job["job_id"]) != digest(json.loads(job["payload_json"])):
        raise ValueError("workflow-batches/v1: member payload differs from frozen configuration")


class BatchRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        require_batches(connection, required=True)
        self.connection = connection
        self.guard = JobCommitGuard(connection)

    def _event(self, batch_id: str, event: str, actor: str, detail: dict[str, Any]) -> None:
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("batch actor must be nonempty")
        self.connection.execute(
            "INSERT INTO workflow_batch_events(batch_id,event,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
            (batch_id, event, actor, canonical(detail), int(time.time())))

    def create(self, job_ids: Iterable[str], *, desired_workers: int = 10, actor: str, reason: str) -> dict[str, Any]:
        ids = tuple(dict.fromkeys(job_ids))
        if not ids or any(not isinstance(job, str) or not job for job in ids):
            raise ValueError("select explicit nonempty job IDs")
        if type(desired_workers) is not int or not 1 <= desired_workers <= 64:
            raise ValueError("desired_workers must be between 1 and 64")
        batch_id = uuid4().hex
        with self.guard.transaction():
            frozen = []
            for job_id in ids:
                row = self.connection.execute(
                    "SELECT payload_json,status FROM workflow_jobs WHERE job_id=?", (job_id,)).fetchone()
                if row is None or row["status"] == "running":
                    raise ValueError("batch members must be known jobs without active executions")
                frozen.append({"job_id": job_id, "payload_sha256": digest(json.loads(row["payload_json"]))})
            now = int(time.time())
            self.connection.execute("INSERT INTO workflow_batches VALUES(?,'active',?,?,?,?,?)",
                                    (batch_id, desired_workers, canonical(frozen), reason, now, now))
            self.connection.executemany("INSERT INTO workflow_batch_members VALUES(?,?)", [(job, batch_id) for job in ids])
            self._event(batch_id, "created", actor, {"members": ids, "desired_workers": desired_workers, "reason": reason})
        return self.status(batch_id)

    def set_state(self, batch_id: str, *, paused: bool, actor: str, reason: str,
                  desired_workers: int | None = None, dry_run: bool = False) -> dict[str, Any]:
        if type(paused) is not bool or (desired_workers is not None and
                (type(desired_workers) is not int or not 1 <= desired_workers <= 64)):
            raise ValueError("invalid batch state/concurrency")
        if dry_run:
            current = self.status(batch_id)
            return {**current, "dry_run": True, "requested_state": "paused" if paused else "active",
                    "requested_workers": desired_workers if desired_workers is not None else current["desired_workers"],
                    "retry_times_changed": False, "attempts_recreated": False}
        with self.guard.transaction():
            self.status(batch_id)
            running = [row[0] for row in self.connection.execute(
                "SELECT m.job_id FROM workflow_batch_members m JOIN workflow_jobs j USING(job_id) "
                "WHERE m.batch_id=? AND j.status='running'", (batch_id,))]
            self.connection.execute("UPDATE workflow_batches SET state=?,reason=?,updated_at=?,"
                                    "desired_workers=coalesce(?,desired_workers) WHERE batch_id=?",
                                    ("paused" if paused else "active", reason, int(time.time()), desired_workers, batch_id))
            self._event(batch_id, "paused" if paused else "resumed", actor,
                        {"reason": reason, "claimed_job_ids": running, "boundary": "already-claimed-jobs-only",
                         "desired_workers": desired_workers})
        return self.status(batch_id)

    def extract(self, batch_id: str, job_ids: Iterable[str], *, actor: str, reason: str,
                desired_workers: int = 1) -> dict[str, Any]:
        """Move an explicit stopped subset into an independent active batch."""
        ids = tuple(dict.fromkeys(job_ids))
        if not ids or type(desired_workers) is not int or not 1 <= desired_workers <= 64:
            raise ValueError("select members and a valid desired worker count")
        new_id = uuid4().hex
        with self.guard.transaction():
            source = self.status(batch_id)
            if source["state"] != "paused":
                raise ValueError("extract requires a paused source batch")
            frozen = []
            for job_id in ids:
                row = self.connection.execute(
                    "SELECT j.payload_json,j.status FROM workflow_batch_members m "
                    "JOIN workflow_jobs j USING(job_id) WHERE m.batch_id=? AND m.job_id=?",
                    (batch_id, job_id)).fetchone()
                if row is None or row["status"] == "running":
                    raise ValueError("extract members must belong to the source and have no active execution")
                frozen.append({"job_id": job_id, "payload_sha256": digest(json.loads(row["payload_json"]))})
            now = int(time.time())
            self.connection.execute("INSERT INTO workflow_batches VALUES(?,'active',?,?,?,?,?)",
                                    (new_id, desired_workers, canonical(frozen), reason, now, now))
            self.connection.executemany("UPDATE workflow_batch_members SET batch_id=? WHERE job_id=? AND batch_id=?",
                                         [(new_id, job, batch_id) for job in ids])
            detail = {"members": ids, "source_batch_id": batch_id, "target_batch_id": new_id, "reason": reason}
            self._event(batch_id, "members-extracted", actor, detail)
            self._event(new_id, "created-from-paused-batch", actor, detail)
        return self.status(new_id)

    def status(self, batch_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM workflow_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise ValueError("unknown batch")
        jobs = self.connection.execute("SELECT j.job_id,j.status,j.available_at,j.lease_owner,j.lease_expires_at "
                                       "FROM workflow_batch_members m JOIN workflow_jobs j USING(job_id) "
                                       "WHERE m.batch_id=?", (batch_id,)).fetchall()
        counts = Counter(job["status"] for job in jobs)
        now = int(time.time())
        running = [job for job in jobs if job["status"] == "running" and job["lease_expires_at"] > now]
        return {"policy": POLICY, "batch_id": batch_id, "state": row["state"], "reason": row["reason"],
                "desired_workers": row["desired_workers"], "active_workers": len({job["lease_owner"] for job in running}),
                "running": len(running), "queued": counts["queued"],
                "active_leases": [{"job_id": job["job_id"], "worker_id": job["lease_owner"],
                                   "expires_at": job["lease_expires_at"]} for job in running],
                "paused": counts["queued"] if row["state"] == "paused" else 0,
                "retry_delayed": sum(job["status"] == "queued" and job["available_at"] > now for job in jobs),
                "failed": counts["failed"], "cancelled": counts["cancelled"], "succeeded": counts["succeeded"],
                "retry_held": None, "retry_hold_source": "external retry control; no implicit reset or import",
                "complete": bool(jobs) and counts["succeeded"] == len(jobs),
                "completion_boundary": "all explicitly selected member jobs"}
