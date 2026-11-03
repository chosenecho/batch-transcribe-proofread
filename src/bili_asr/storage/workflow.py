"""SQLite-backed workflow control plane for independent archive producers.

The repository contains no network, filesystem, model, or global-process
state.  Workers claim immutable job descriptions, execute outside the
transaction, and report a terminal attempt afterwards.  This keeps scheduling
recoverable without allowing a subtitle observation to suppress local ASR.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable, Mapping
from typing import Any, Callable
from uuid import uuid4
from bili_asr.storage.job_commit import JobCommitGuard
from bili_asr.transcript_selection import choose_transcript
from bili_asr.workflow_planning import JobSpec, PlanningPart, editorial_specs, plan_producers
from bili_asr.workflow_payloads import validate_payload
from bili_asr.storage.workflow_state import validate_job_transition


from bili_asr.workflow_models import (
    AsrPolicy, AsrProfile, CancellationResult, JobCancelledError, JobKind,
    JobStatus, LeaseLostError, WorkflowJob, WorkflowPlan,
)


def _now() -> int:
    return int(time.time())


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


class WorkflowRepository:
    """Control-plane persistence with explicit job dependencies."""

    def __init__(self, connection: sqlite3.Connection):
        if connection.row_factory is None:
            raise TypeError("connection must return sqlite3.Row objects")
        self.connection = connection
        self.commit_guard = JobCommitGuard(connection, clock=lambda: _now())
        self._busy_timeout_ms = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
        row = connection.execute("PRAGMA database_list").fetchone()
        self._database_path = "" if row is None else str(row["file"] or "")

    def close(self) -> None:
        self.connection.close()

    def _assert_job_transition(self, job_id: str, target: str) -> str:
        row = self.connection.execute("SELECT status FROM workflow_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown workflow job: {job_id}")
        current = str(row["status"])
        validate_job_transition(current, target)
        return current

    def open_lease_repository(self) -> WorkflowRepository | None:
        """Open a connection suitable for a background lease heartbeat.

        SQLite connections are thread-affine by default.  A worker can spend
        minutes inside model inference, so the heartbeat cannot safely reuse
        the worker's connection.  The in-memory database used by unit tests has
        no second connection that can see the same state and therefore returns
        ``None``; file-backed workflow databases get an independent connection
        with the same row and foreign-key settings as :func:`open_database`.
        """

        if not self._database_path:
            return None
        from bili_asr.archive_session import ArchiveAccessMode, open_archive_connection

        connection = open_archive_connection(self._database_path, mode=ArchiveAccessMode.WRITE,
                                             busy_timeout_ms=min(self._busy_timeout_ms, 1000))
        return type(self)(connection)

    def register_profile(self, profile: AsrProfile) -> int:
        with self._write_transaction():
            return self._register_profile(profile)

    def _register_profile(self, profile: AsrProfile) -> int:
        self.require_cancellation_contract()
        if not isinstance(profile, AsrProfile):
            raise TypeError("profile must be AsrProfile")
        if not profile.profile_key.strip() or not profile.model_name.strip():
            raise ValueError("profile_key and model_name must be non-empty")
        digest = hashlib.sha256(profile.canonical().encode("utf-8")).hexdigest()
        now = _now()
        row = self.connection.execute(
            "SELECT profile_id FROM workflow_asr_profiles "
            "WHERE profile_key = ? AND config_sha256 = ?",
            (profile.profile_key, digest),
        ).fetchone()
        if row is not None:
            return int(row["profile_id"])
        self.connection.execute(
            """
            INSERT INTO workflow_asr_profiles(
                profile_key, model_name, model_revision, aligner_name, device,
                language, config_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile.profile_key,
                profile.model_name,
                profile.model_revision,
                profile.aligner_name,
                profile.device,
                profile.language,
                digest,
                now,
            ),
        )
        row = self.connection.execute(
            "SELECT profile_id FROM workflow_asr_profiles "
            "WHERE profile_key = ? AND config_sha256 = ?",
            (profile.profile_key, digest),
        ).fetchone()
        self.connection.execute(
            "INSERT INTO workflow_asr_profile_configs(profile_id, schema_version, config_json) "
            "VALUES (?, 2, ?)",
            (int(row["profile_id"]), profile.canonical()),
        )
        return int(row["profile_id"])

    def plan(self, *, part_ids: Iterable[int], policy: AsrPolicy,
             profile_id: int | None = None,
             editorial_config: Mapping[str, Any] | None = None) -> WorkflowPlan:
        self.require_cancellation_contract()
        ids = tuple(dict.fromkeys(int(part_id) for part_id in part_ids))
        if not ids:
            return WorkflowPlan(0, 0, 0)
        if profile_id is None:
            raise ValueError("profile_id is required for ASR planning")
        with self._write_transaction():
            return self._plan(ids, policy=policy, profile_id=profile_id,
                              editorial_config=editorial_config)

    def plan_with_profile(self, *, part_ids: Iterable[int], policy: AsrPolicy,
                          profile: AsrProfile,
                          editorial_config: Mapping[str, Any] | None = None) -> tuple[int, WorkflowPlan]:
        """Commit the frozen profile and its complete dependency graph together."""
        self.require_cancellation_contract()
        ids = tuple(dict.fromkeys(int(part_id) for part_id in part_ids))
        with self._write_transaction():
            profile_id = self._register_profile(profile)
            plan = self._plan(ids, policy=policy, profile_id=profile_id,
                              editorial_config=editorial_config)
        return profile_id, plan

    def enqueue_asr_repair(self, *, repair_of_job_id: str | None, video_part_id: int,
                           profile: AsrProfile, reference_transcript_id: int | None,
                           repair_of_transcript_id: int | None = None,
                           editorial_config: Mapping[str, Any] | None = None) -> dict:
        """Create a replacement graph inside an explicit repair transaction."""
        if not self.connection.in_transaction:
            raise RuntimeError("ASR repair enqueue requires a write transaction")
        profile_id = self._register_profile(profile)
        digest = self.profile_digest(profile_id)
        asr_key = "repair-asr"
        payload = {"video_part_id": video_part_id, "profile_id": profile_id,
                   "reference_transcript_id": reference_transcript_id}
        origin = repair_of_job_id if repair_of_job_id is not None else f"transcript-{repair_of_transcript_id}"
        if repair_of_job_id is not None:
            payload["repair_of_job_id"] = repair_of_job_id
        if repair_of_transcript_id is not None:
            payload["repair_of_transcript_id"] = repair_of_transcript_id
        specs = [
            JobSpec(asr_key, JobKind.ASR, video_part_id,
                    payload, dedupe_key=f"asr-repair:{origin}:{digest}", profile_id=profile_id,
                    policy_key=AsrPolicy.SELECTED.value)]
        if editorial_config is not None:
            specs.extend(editorial_specs(video_part_id, {"editorial_config": dict(editorial_config)}, prerequisite=asr_key))
        identities, created = self._enqueue_specs(specs)
        return {"repair_of_job_id": repair_of_job_id, "job_id": identities[asr_key],
                "repair_of_transcript_id": repair_of_transcript_id,
                "profile_id": profile_id, "profile_sha256": digest,
                "created": bool(created.get(JobKind.ASR))}

    def _plan(self, ids: tuple[int, ...], *, policy: AsrPolicy, profile_id: int,
              editorial_config: Mapping[str, Any] | None) -> WorkflowPlan:
        if editorial_config is not None:
            self._require_editorial_contract()
        facts = self._planning_parts(ids)
        specs = plan_producers(facts, policy=policy, profile_id=profile_id,
                               profile_digest=self.profile_digest(profile_id),
                               editorial_config=editorial_config)
        _, created = self._enqueue_specs(specs)
        return WorkflowPlan(*(created.get(kind, 0) for kind in (
            JobKind.SUBTITLE, JobKind.AUDIO, JobKind.ASR, JobKind.PROOFREAD, JobKind.RENDER_DOCUMENT)))

    def _planning_parts(self, ids: tuple[int, ...]) -> tuple[PlanningPart, ...]:
        facts: dict[int, PlanningPart] = {}
        for offset in range(0, len(ids), 500):
            chunk = ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"SELECT p.video_part_id FROM video_parts p WHERE p.video_part_id IN ({placeholders}) "
                "AND p.processing_status <> 'gone'", chunk)
            valid = {int(row["video_part_id"]) for row in rows}
            references: dict[int, list[sqlite3.Row]] = {}
            for row in self.connection.execute(
                    f"SELECT * FROM transcripts WHERE video_part_id IN ({placeholders}) "
                    "AND source_kind IN ('subtitle-cc', 'subtitle-ai')", chunk):
                references.setdefault(int(row["video_part_id"]), []).append(row)
            for part_id in valid:
                reference = choose_transcript(references.get(part_id, ()))
                facts[part_id] = PlanningPart(part_id, None if reference is None else int(reference["transcript_id"]))
        if set(facts) != set(ids):
            raise ValueError("part_ids contains unknown or gone video parts")
        return tuple(facts[part_id] for part_id in ids)

    def _enqueue_specs(self, specs: Iterable[JobSpec], *,
                       existing_ids: Mapping[str, str] | None = None) -> tuple[dict[str, str], dict[JobKind, int]]:
        ids = dict(existing_ids or {})
        counts: dict[JobKind, int] = {}
        for spec in specs:
            payload, dedupe_key = spec.materialize(ids)
            if spec.kind is JobKind.RENDER_DOCUMENT:
                from bili_asr.storage.editorial_facts import input_for_proofread, template_for_input

                proof_id = payload["proofread_job_id"]
                pending = self.connection.execute("SELECT job_id,status FROM workflow_jobs WHERE dedupe_key=?",
                                                   (f"render:{proof_id}:auto",)).fetchone()
                if pending is not None and pending["status"] in {"succeeded", "failed", "cancelled"}:
                    # Cancellation while proofreading was still in flight must
                    # not become a fresh render just because its input is known.
                    ids[spec.key] = pending["job_id"]
                    continue
                input_id = input_for_proofread(self.connection, proof_id)
                if input_id is not None:
                    self.bind_editorial_render(proof_id, template_for_input(self.connection, input_id))
                existing_render = self.connection.execute(
                    "SELECT j.job_id,j.status,j.dedupe_key,j.attempt_count FROM workflow_jobs j "
                    "JOIN workflow_job_dependencies d ON d.job_id=j.job_id "
                    "WHERE d.prerequisite_job_id=? AND j.kind='render_document' "
                    "AND json_extract(j.payload_json,'$.proofread_job_id')=? ORDER BY j.created_at,j.rowid LIMIT 1",
                    (proof_id, proof_id)).fetchone()
                # Acquisition has not frozen an input yet. This placeholder is
                # bound transactionally before proofreading starts any AI work.
                payload["template_version"] = template_for_input(self.connection, input_id) if input_id else "auto"
                dedupe_key = f"render:{proof_id}:{payload['template_version']}"
                canonical = self.connection.execute("SELECT job_id FROM workflow_jobs WHERE dedupe_key=?",
                                                     (dedupe_key,)).fetchone()
                if existing_render is not None and canonical is None and (
                        input_id is None or existing_render["attempt_count"] > 0
                        or existing_render["status"] in {"running", "succeeded", "failed", "cancelled"}):
                    # Existing acquisition chains (including the old guessed
                    # template) retain their scheduling and terminal identities.
                    # An already-failed wrong template needs explicit rerender.
                    ids[spec.key] = existing_render["job_id"]
                    continue
            job_id, created = self._ensure_job(kind=spec.kind, video_part_id=spec.video_part_id,
                profile_id=spec.profile_id, policy_key=spec.policy_key, payload=payload, dedupe_key=dedupe_key)
            ids[spec.key] = job_id
            counts[spec.kind] = counts.get(spec.kind, 0) + int(created)
            for key in spec.prerequisites:
                self.connection.execute("INSERT OR IGNORE INTO workflow_job_dependencies VALUES (?, ?)", (job_id, ids[key]))
        return ids, counts

    def _candidate(self, *, now: int, kinds: Iterable[JobKind] | None,
                   expired: bool = False, job_id: str | None = None, exclude_job_ids: tuple[str, ...] = ()):
        selected = None if kinds is None else tuple(k.value for k in kinds)
        if selected == ():
            return None
        status = "(j.status='queued' OR (j.status='running' AND j.lease_expires_at <= ?))" if expired else "j.status='queued'"
        parameters = [now, now] if expired else [now]
        filters = ""
        if selected is not None:
            filters += " AND j.kind IN (" + ",".join("?" for _ in selected) + ")"
            parameters.extend(selected)
        if job_id is not None:
            filters += " AND j.job_id=?"
            parameters.append(job_id)
        if exclude_job_ids:
            filters += " AND j.job_id NOT IN (SELECT value FROM json_each(?))"
            parameters.append(json.dumps(exclude_job_ids))
        from bili_asr.storage.workflow_batches import claim_filter, require_frozen_member
        batch_filter = claim_filter(self.connection)
        if batch_filter:
            filters += batch_filter
            parameters.append(now)
        row = self.connection.execute(
            "SELECT j.* FROM workflow_jobs AS j WHERE " + status + """ AND j.available_at <= ?
            AND NOT EXISTS (
                SELECT 1 FROM workflow_job_dependencies AS d
                JOIN workflow_jobs AS prerequisite ON prerequisite.job_id = d.prerequisite_job_id
                WHERE d.job_id = j.job_id AND prerequisite.status <> 'succeeded'
            )""" + filters + " ORDER BY j.priority DESC,j.created_at,j.job_id LIMIT 1",
            parameters,
        ).fetchone()
        if batch_filter and row is not None:
            require_frozen_member(self.connection, row)
        return row

    def peek_candidate(self, *, kinds: Iterable[JobKind] | None = None, exclude_job_ids: tuple[str, ...] = ()) -> WorkflowJob | None:
        """Read a hint for preparation; does not acquire a lease or recover attempts."""
        self.require_cancellation_contract()
        row = self._candidate(now=_now(), kinds=kinds, expired=True, exclude_job_ids=exclude_job_ids)
        return None if row is None else self._job_from_row(row)

    def claim(self, worker_id: str, *, lease_seconds: int = 900,
              kinds: Iterable[JobKind] | None = None,
              expected_candidate: WorkflowJob | None = None) -> WorkflowJob | None:
        self.require_cancellation_contract()
        if not worker_id.strip() or lease_seconds < 1:
            raise ValueError("worker_id and lease_seconds must be valid")
        kinds = None if kinds is None else tuple(kinds)
        selected = None if kinds is None else tuple(k.value for k in kinds)
        if selected == ():
            return None
        with self.commit_guard.transaction():
            now = _now()
            self.connection.execute(
                """UPDATE workflow_attempts
                   SET outcome = 'failed', finished_at = ?, error_code = 'lease_expired'
                   WHERE outcome = 'running' AND job_id IN (
                       SELECT job_id FROM workflow_jobs
                       WHERE status = 'running' AND lease_expires_at <= ?
                   )""",
                (now, now),
            )
            expired_ids = [str(item["job_id"]) for item in self.connection.execute(
                "SELECT job_id FROM workflow_jobs WHERE status = 'running' AND lease_expires_at <= ?", (now,)
            )]
            for expired_id in expired_ids:
                self._assert_job_transition(expired_id, "queued")
            self.connection.execute(
                """UPDATE workflow_jobs
                   SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL,
                       updated_at = ?
                   WHERE status = 'running' AND lease_expires_at <= ?""",
                (now, now),
            )
            row = self._candidate(now=now, kinds=kinds,
                                  job_id=None if expected_candidate is None else expected_candidate.job_id)
            if row is None:
                return None
            if expected_candidate is not None:
                from dataclasses import replace
                if self._job_from_row(row) != replace(expected_candidate, status=JobStatus.QUEUED, lease_owner=None):
                    return None
            attempt_id = str(uuid4())
            self._assert_job_transition(str(row["job_id"]), "running")
            self.connection.execute(
                """UPDATE workflow_jobs
                   SET status = 'running', lease_owner = ?, lease_expires_at = ?,
                       attempt_count = attempt_count + 1, updated_at = ?
                   WHERE job_id = ? AND status = 'queued'""",
                (worker_id, now + lease_seconds, now, row["job_id"]),
            )
            self.connection.execute(
                """INSERT INTO workflow_attempts(attempt_id, job_id, worker_id, started_at, outcome)
                   VALUES (?, ?, ?, ?, 'running')""",
                (attempt_id, row["job_id"], worker_id, now),
            )
            claimed = self.connection.execute(
                "SELECT * FROM workflow_jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
            job = self._job_from_row(claimed)
            self.commit_guard.assert_lease(job)
        return job

    def renew_lease(self, job: WorkflowJob, *, lease_seconds: int) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        with self.commit_guard.transaction():
            deadline = self.commit_guard.assert_lease(job)
            now = _now()
            self.connection.execute(
                "UPDATE workflow_jobs SET lease_expires_at = ?, updated_at = ? WHERE job_id = ?",
                (now + lease_seconds, now, job.job_id))
            renewed_deadline = self.commit_guard.assert_lease(job)
            self.commit_guard.assert_unexpired(min(deadline, renewed_deadline))

    def assert_lease(self, job: WorkflowJob) -> None:
        """Fence a side effect to the exact claimed attempt."""
        self.commit_guard.assert_lease(job)

    def is_cancelled(self, job: WorkflowJob) -> bool:
        row = self.connection.execute(
            "SELECT status, attempt_count FROM workflow_jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()
        return bool(row is not None and row["status"] == "cancelled"
                    and row["attempt_count"] == job.attempt_count)

    @contextmanager
    def owned_transaction(self, job: WorkflowJob, *, on_rollback: Callable[[], None] | None = None):
        """Serialize a short result commit with cancellation and reclamation.

        Network requests, inference and file preparation must happen outside.
        The ownership check and all authoritative writes share the write lock.
        Nested transactions would allow independent repositories to commit it.
        """
        with self.commit_guard.owned_transaction(job, on_rollback=on_rollback):
            yield

    @contextmanager
    def _write_transaction(self, *, on_rollback: Callable[[], None] | None = None):
        with self.commit_guard.transaction(on_rollback=on_rollback):
            yield

    def require_cancellation_contract(self) -> None:
        row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'workflow_attempts'"
        ).fetchone()
        if row is None or str(row["sql"]).count("'cancelled'") < 2:
            raise ValueError("workflow schema predates cancellation; use a rebuilt archive database")

    def cancel(self, *, job_ids: Iterable[str]) -> list[CancellationResult]:
        """Atomically cancel selected queued/running jobs; terminal jobs are noops.

        Running attempts finish as cancelled at acceptance. Workers observe the
        revoked lease at their next checkpoint. Dependencies are not cascaded.
        """
        self.require_cancellation_contract()
        ids = tuple(dict.fromkeys(str(value).strip() for value in job_ids))
        if not ids or any(not value for value in ids):
            raise ValueError("at least one non-empty job_id is required")
        now = _now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            rows = {}
            # Avoid SQLite's parameter limit for a large explicitly selected set.
            for job_id in ids:
                row = self.connection.execute(
                    "SELECT * FROM workflow_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown workflow job: {job_id}")
                rows[job_id] = row
            results = []
            for job_id in ids:
                row = rows[job_id]
                previous = str(row["status"])
                changed = previous in ("queued", "running")
                if changed:
                    if previous == "running":
                        cursor = self.connection.execute(
                            "UPDATE workflow_attempts SET outcome = 'cancelled', finished_at = ?, "
                            "error_code = 'cancelled', result_json = NULL "
                            "WHERE job_id = ? AND worker_id = ? AND outcome = 'running'",
                            (now, job_id, row["lease_owner"]),
                        )
                        if cursor.rowcount != 1:
                            raise RuntimeError("running attempt is missing")
                    self._assert_job_transition(job_id, "cancelled")
                    self.connection.execute(
                        "UPDATE workflow_jobs SET status = 'cancelled', lease_owner = NULL, "
                        "lease_expires_at = NULL, last_error_code = 'cancelled', updated_at = ? "
                        "WHERE job_id = ?", (now, job_id),
                    )
                results.append(CancellationResult(job_id, previous,
                                                   "cancelled" if changed else previous, changed))
            self.connection.commit()
            return results
        except BaseException:
            self.connection.rollback()
            raise

    def blocked_by_cancelled(self) -> set[str]:
        """Queued dependants of cancelled jobs, including transitive edges."""
        return {str(row[0]) for row in self.connection.execute(
            "WITH RECURSIVE blocked(job_id) AS ("
            "SELECT job_id FROM workflow_jobs WHERE status = 'cancelled' "
            "UNION SELECT d.job_id FROM workflow_job_dependencies AS d "
            "JOIN blocked AS b ON b.job_id = d.prerequisite_job_id) "
            "SELECT j.job_id FROM workflow_jobs AS j JOIN blocked AS b ON b.job_id = j.job_id "
            "WHERE j.status = 'queued'")}

    def dependency_result(self, job: WorkflowJob, kind: JobKind) -> Mapping[str, Any]:
        """Return the successful result of an exact prerequisite job."""
        row = self.connection.execute(
            """
            SELECT a.result_json
            FROM workflow_job_dependencies AS d
            JOIN workflow_jobs AS prerequisite ON prerequisite.job_id = d.prerequisite_job_id
            JOIN workflow_attempts AS a ON a.job_id = prerequisite.job_id
            WHERE d.job_id = ? AND prerequisite.kind = ? AND a.outcome = 'succeeded'
            ORDER BY a.finished_at DESC, a.rowid DESC LIMIT 1
            """,
            (job.job_id, kind.value),
        ).fetchone()
        if row is None or not row["result_json"]:
            raise LeaseLostError(f"successful {kind.value} prerequisite result is missing")
        value = json.loads(str(row["result_json"]))
        if not isinstance(value, dict):
            raise ValueError("workflow prerequisite result must be an object")
        return value

    def _require_editorial_contract(self) -> None:
        from bili_asr.storage.database import require_editorial_schema

        require_editorial_schema(self.connection)
        row = self.connection.execute("SELECT sql FROM sqlite_master WHERE name = 'workflow_jobs'").fetchone()
        if row is None or "'proofread'" not in row["sql"]:
            raise ValueError("workflow schema predates AI proofreading; use a rebuilt archive database")

    def request_editorial(self, *, video_part_id: int, input_id: str) -> tuple[str, str, bool, bool]:
        with self._write_transaction():
            return self.enqueue_editorial(video_part_id=video_part_id, input_id=input_id)

    def enqueue_editorial(self, *, video_part_id: int, input_id: str,
                          repair_of_job_id: str | None = None) -> tuple[str, str, bool, bool]:
        """Persist both jobs inside an application-owned write transaction."""
        if not self.connection.in_transaction:
            raise RuntimeError("editorial enqueue requires a write transaction")
        self.require_cancellation_contract()
        self._require_editorial_contract()
        payload = {"input_id": input_id}
        if repair_of_job_id is not None:
            payload["repair_of_job_id"] = repair_of_job_id
        return self._editorial_jobs(video_part_id=video_part_id, payload=payload)

    def _editorial_jobs(self, *, video_part_id: int, payload: Mapping[str, Any],
                        prerequisite_job_id: str | None = None) -> tuple[str, str, bool, bool]:
        external = None if prerequisite_job_id is None else "asr-parent"
        specs = editorial_specs(video_part_id, payload, prerequisite=external)
        ids, created = self._enqueue_specs(specs, existing_ids={} if external is None else {external: prerequisite_job_id})
        return ids[specs[0].key], ids[specs[1].key], bool(created.get(JobKind.PROOFREAD)), bool(created.get(JobKind.RENDER_DOCUMENT))

    def enqueue_editorial_recovery(self, *, video_part_id: int, input_id: str,
                                    previous_proofread_job_id: str, previous_render_job_id: str,
                                    replacement_asr_job_id: str, replacement_attempt_id: str,
                                    replacement_run_id: str) -> tuple[str, str, bool, bool]:
        """New frozen consumers with explicit provenance; old edges stay intact."""
        if not self.connection.in_transaction:
            raise RuntimeError("editorial recovery enqueue requires a write transaction")
        self.require_cancellation_contract()
        self._require_editorial_contract()
        specs = (
            JobSpec("recovery-proof", JobKind.PROOFREAD, video_part_id,
                    {"input_id": input_id, "repair_of_job_id": previous_proofread_job_id,
                     "replacement_asr_job_id": replacement_asr_job_id,
                     "replacement_attempt_id": replacement_attempt_id,
                     "replacement_run_id": replacement_run_id}, prerequisites=("recovery-asr",)),
            JobSpec("recovery-render", JobKind.RENDER_DOCUMENT, video_part_id,
                    {"template_version": "auto", "repair_of_job_id": previous_render_job_id},
                    prerequisites=("recovery-proof",),
                    payload_references=(("proofread_job_id", "recovery-proof"),)),
        )
        ids, created = self._enqueue_specs(specs, existing_ids={"recovery-asr": replacement_asr_job_id})
        return (ids["recovery-proof"], ids["recovery-render"],
                bool(created.get(JobKind.PROOFREAD)), bool(created.get(JobKind.RENDER_DOCUMENT)))

    def bind_editorial_render(self, proof_id: str, template: str) -> None:
        """Finalize unattempted placeholders without rewriting execution history."""
        if template not in {"ai-draft-v1", "ai-draft-v2"}:
            raise ValueError("unsupported document template version")
        if not self.connection.in_transaction:
            raise RuntimeError("render binding requires a write transaction")
        key = f"render:{proof_id}:{template}"
        rows = self.connection.execute(
            "SELECT j.* FROM workflow_jobs j JOIN workflow_job_dependencies d ON d.job_id=j.job_id "
            "WHERE d.prerequisite_job_id=? AND j.kind='render_document'", (proof_id,)).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("proofread_job_id") != proof_id or row["dedupe_key"] == key:
                continue
            # Completed, failed, cancelled and previously attempted rows are
            # evidence. The original attempt's payload/identity stays intact.
            if row["status"] != "queued" or row["attempt_count"] != 0:
                continue
            payload["template_version"] = template
            existing = self.connection.execute("SELECT job_id FROM workflow_jobs WHERE dedupe_key=?", (key,)).fetchone()
            if existing is None:
                self.connection.execute("UPDATE workflow_jobs SET payload_json=?,dedupe_key=?,updated_at=? WHERE job_id=?",
                                        (_json(payload), key, _now(), row["job_id"]))
            else:
                # Only the never-executed scheduling placeholder is discarded.
                # Redirect graph edges to the existing canonical job, retaining
                # its terminal state rather than resurrecting paid work.
                canonical_id = existing["job_id"]
                self.connection.execute("INSERT OR IGNORE INTO workflow_job_dependencies SELECT ?,prerequisite_job_id "
                                        "FROM workflow_job_dependencies WHERE job_id=?", (canonical_id, row["job_id"]))
                self.connection.execute("INSERT OR IGNORE INTO workflow_job_dependencies SELECT job_id,? "
                                        "FROM workflow_job_dependencies WHERE prerequisite_job_id=?", (canonical_id, row["job_id"]))
                self.connection.execute("DELETE FROM workflow_job_dependencies WHERE job_id=? OR prerequisite_job_id=?",
                                        (row["job_id"], row["job_id"]))
                if self.connection.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_input_states'").fetchone():
                    # Readiness is scoped to the discarded scheduling identity.
                    # Preserve canonical observations; a fresh preparation will
                    # establish its own state rather than inheriting stale hints.
                    self.connection.execute("DELETE FROM artifact_input_states WHERE job_id=?", (row["job_id"],))
                self.connection.execute("DELETE FROM workflow_jobs WHERE job_id=?", (row["job_id"],))

    def request_document(self, *, video_part_id: int, revision_id: str,
                         template_version: str | None = None) -> tuple[str, bool]:
        from bili_asr.storage.editorial_facts import revision, template_for_input

        if template_version is not None and template_version not in {"ai-draft-v1", "ai-draft-v2"}:
            raise ValueError("unsupported document template version")
        self.require_cancellation_contract()
        self._require_editorial_contract()
        prepared, _ = revision(self.connection, revision_id)
        expected = template_for_input(self.connection, prepared["input_id"])
        if template_version is not None and template_version != expected:
            raise ValueError("unsupported document template version for frozen input")
        if prepared["snapshot"]["video_part_id"] != video_part_id:
            raise ValueError("revision belongs to a different part")
        template_version = expected
        with self.connection:
            job_id, created = self._ensure_job(
                kind=JobKind.RENDER_DOCUMENT, video_part_id=video_part_id, profile_id=None, policy_key=None,
                payload={"revision_id": revision_id, "template_version": template_version},
                dedupe_key=f"render:{revision_id}:{template_version}")
            # Explicit rerender repairs missing artifacts without re-inference.
            current = self.connection.execute("SELECT status FROM workflow_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if current is not None and str(current["status"]) in {"succeeded", "failed"}:
                self._assert_job_transition(job_id, "queued")
                self.connection.execute("UPDATE workflow_jobs SET status = 'queued', available_at = ?, updated_at = ? "
                                        "WHERE job_id = ? AND status IN ('succeeded', 'failed')", (_now(), _now(), job_id))
        return job_id, created

    def finish(
        self,
        job_id: str,
        *,
        worker_id: str,
        result: Mapping[str, Any] | None = None,
        expected_attempt_count: int | None = None,
    ) -> None:
        self._terminal(job_id, worker_id=worker_id, outcome="succeeded", result=result,
                       expected_attempt_count=expected_attempt_count)

    def fail(
        self,
        job_id: str,
        *,
        worker_id: str,
        error_code: str,
        retry_at: int | None = None,
        expected_attempt_count: int | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        if not error_code or len(error_code) > 64:
            raise ValueError("error_code must be 1-64 characters")
        self._terminal(
            job_id,
            worker_id=worker_id,
            outcome="failed",
            error_code=error_code,
            retry_at=retry_at,
            expected_attempt_count=expected_attempt_count,
            result=result,
        )

    def list_jobs(self) -> list[WorkflowJob]:
        rows = self.connection.execute(
            "SELECT * FROM workflow_jobs ORDER BY created_at, job_id"
        ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def list_job_identities(self):
        return self.connection.execute(
            "SELECT j.*, p.bvid, p.page_index FROM workflow_jobs j "
            "LEFT JOIN video_parts p ON p.video_part_id = j.video_part_id "
            "ORDER BY j.created_at, j.job_id")

    def has_manuscript_contract(self) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'manuscript_contract'"
        ).fetchone() is not None

    def count_by_status(self) -> dict[str, int]:
        return {
            str(row["status"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT status, COUNT(*) AS count FROM workflow_jobs GROUP BY status"
            )
        }

    def explain_job(self, job_id: str) -> dict[str, Any]:
        """Derive readiness from dependencies without inventing a stored status."""
        row = self.connection.execute("SELECT * FROM workflow_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown workflow job: {job_id}")
        prerequisites = [dict(item) for item in self.connection.execute(
            "SELECT p.job_id, p.kind, p.status, p.last_error_code AS error_code "
            "FROM workflow_job_dependencies d JOIN workflow_jobs p ON p.job_id = d.prerequisite_job_id "
            "WHERE d.job_id = ? ORDER BY p.job_id", (job_id,))]
        blockers = [item for item in prerequisites if item["status"] != "succeeded"]
        attempt = self.connection.execute(
            "SELECT worker_id, outcome, error_code, started_at, finished_at FROM workflow_attempts "
            "WHERE job_id = ? ORDER BY rowid DESC LIMIT 1", (job_id,)).fetchone()
        queued = row["status"] == "queued"
        return {"job_id": job_id, "kind": row["kind"], "status": row["status"],
                "video_part_id": row["video_part_id"], "attempt_count": row["attempt_count"],
                "error_code": row["last_error_code"], "prerequisites": prerequisites,
                "blockers": blockers, "blocked": queued and bool(blockers),
                "ready": queued and not blockers and row["available_at"] <= _now(),
                "available_at": row["available_at"],
                "last_attempt": None if attempt is None else dict(attempt)}

    def requeue_failed(self, *, part_ids: Iterable[int] | None = None,
                       job_ids: Iterable[str] | None = None, kinds: Iterable[JobKind] | None = None,
                       automatic: bool = False) -> int:
        """Legacy manual requeue; automatic callers must use the guarded use case.

        This compatibility method has no external control evidence. New CLI
        and controllers use services.workflow_retry.guarded_retry_failed.
        """
        if automatic:
            raise ValueError("automatic retry requires the guarded retry service and control state")
        ids = None if part_ids is None else tuple(dict.fromkeys(int(part_id) for part_id in part_ids))
        jobs = None if job_ids is None else tuple(dict.fromkeys(job_ids))
        selected = None if kinds is None else tuple(dict.fromkeys(k.value for k in kinds))
        if ids == () or jobs == () or selected == ():
            return 0
        now = _now()
        where = "status = 'failed'"
        values: list[Any] = [now, now]
        if ids is not None:
            where += " AND video_part_id IN (" + ",".join("?" for _ in ids) + ")"
            values.extend(ids)
        for column, selector in (("job_id", jobs), ("kind", selected)):
            if selector is not None:
                where += f" AND {column} IN (" + ",".join("?" for _ in selector) + ")"
                values.extend(selector)
        with self.connection:
            for row in self.connection.execute("SELECT job_id, status FROM workflow_jobs WHERE " + where, values[2:]).fetchall():
                validate_job_transition(str(row["status"]), "queued")
            cursor = self.connection.execute(
                """UPDATE workflow_jobs
                   SET status = 'queued', available_at = ?, lease_owner = NULL,
                       lease_expires_at = NULL, last_error_code = NULL, updated_at = ?
                   WHERE """
                + where,
                values,
            )
        return int(cursor.rowcount)

    def request_publication(
        self,
        *,
        video_part_id: int,
        transcript_id: int,
        source_job: WorkflowJob | None = None,
        force: bool = False,
    ) -> tuple[str, bool]:
        """Queue publication for a part after a transcript version is durable.

        Publication is keyed by part, rather than by transcript version.  The
        publication worker chooses the best stored version when it runs, so a
        later local-ASR result cannot overwrite a preferred subtitle merely
        because it completed later.  A changed requested transcript reopens a
        completed publication job to let a later, better source be projected.
        """
        with (self.owned_transaction(source_job) if source_job is not None else self._write_transaction()):
            return self.enqueue_publication(video_part_id=video_part_id, transcript_id=transcript_id, force=force)

    def enqueue_publication(self, *, video_part_id: int, transcript_id: int, force: bool = False,
                            preserve_publication: Mapping[str, Any] | None = None) -> tuple[str, bool]:
        """Persist a request inside the application's existing write transaction."""
        if not self.connection.in_transaction:
            raise RuntimeError("publication enqueue requires a write transaction")
        self.require_cancellation_contract()
        now = _now()
        payload = {"schema_version": 1, "transcript_id": int(transcript_id), "video_part_id": int(video_part_id)}
        if preserve_publication is not None:
            payload.update(preserve_publication=dict(preserve_publication),
                           publication_request_id=str(uuid4()))
        validate_payload(JobKind.PUBLISH, payload, part_id=video_part_id)
        dedupe_key = f"publish:{video_part_id}"
        row = self.connection.execute(
            "SELECT job_id, payload_json, status FROM workflow_jobs WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        if row is None:
            job_id, created = self._ensure_job(
                kind=JobKind.PUBLISH,
                video_part_id=video_part_id,
                profile_id=None,
                policy_key=None,
                payload=payload,
                dedupe_key=dedupe_key,
            )
            return job_id, created
        if preserve_publication is not None and row["status"] == "running":
            raise ValueError("publication refresh requires an idle publication job; wait for the current attempt and plan again")
        previous = json.loads(str(row["payload_json"]))
        if row["status"] == "cancelled":
            return str(row["job_id"]), False
        if force or int(previous.get("transcript_id", -1)) != transcript_id or preserve_publication is not None:
            if row["status"] == "running":
                # Keep the current lease intact.  _terminal sees the newer
                # request and returns this part to the queue once this
                # attempt records its result.
                self.connection.execute(
                    "UPDATE workflow_jobs SET payload_json = ?, updated_at = ? WHERE job_id = ?",
                    (_json(payload), now, row["job_id"]),
                )
            else:
                self.connection.execute(
                    """UPDATE workflow_jobs
                       SET payload_json = ?, status = 'queued', available_at = ?,
                           lease_owner = NULL, lease_expires_at = NULL,
                           last_error_code = NULL, updated_at = ?
                       WHERE job_id = ?""",
                    (_json(payload), now, now, row["job_id"]),
                )
        return str(row["job_id"]), False

    def profile(self, profile_id: int) -> AsrProfile:
        row = self.connection.execute(
            "SELECT * FROM workflow_asr_profiles WHERE profile_id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown ASR profile")
        snapshot = self.connection.execute(
            "SELECT config_json FROM workflow_asr_profile_configs WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        if snapshot is not None:
            from bili_asr.profile_codec import decode_profile
            values = decode_profile(snapshot["config_json"])
            profile = AsrProfile(profile_key=str(row["profile_key"]), **values)
            if hashlib.sha256(profile.canonical().encode("utf-8")).hexdigest() != row["config_sha256"]:
                raise ValueError("ASR profile snapshot hash mismatch")
            return profile
        # Pre-v2 profiles used one revision for BOTH checkpoints. Preserve that
        # execution contract and never rewrite their existing identity/digest.
        legacy = {name: row[name] for name in (
            "model_name", "model_revision", "aligner_name", "device", "language"
        )}
        legacy_json = json.dumps(legacy, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if hashlib.sha256(legacy_json.encode("utf-8")).hexdigest() != row["config_sha256"]:
            raise ValueError("ASR profile snapshot missing or legacy hash mismatch")
        return AsrProfile(
            profile_key=str(row["profile_key"]),
            model_name=str(row["model_name"]),
            model_revision=str(row["model_revision"]),
            aligner_revision=str(row["model_revision"]) or None,
            aligner_name=str(row["aligner_name"]),
            device=str(row["device"]),
            language=row["language"],
        )

    def profile_digest(self, profile_id: int) -> str:
        row = self.connection.execute(
            "SELECT config_sha256 FROM workflow_asr_profiles WHERE profile_id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown ASR profile")
        return str(row["config_sha256"])

    def _ensure_job(
        self,
        *,
        kind: JobKind,
        video_part_id: int,
        profile_id: int | None,
        policy_key: str | None,
        payload: Mapping[str, Any],
        dedupe_key: str,
    ) -> tuple[str, bool]:
        validate_payload(kind, payload, part_id=video_part_id, profile_id=profile_id)
        now = _now()
        job_id = str(uuid4())
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO workflow_jobs(
                job_id, kind, video_part_id, profile_id, policy_key, dedupe_key,
                payload_json, status, available_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
            """,
            (job_id, kind.value, video_part_id, profile_id, policy_key, dedupe_key, _json({"schema_version": 1, **payload}), now, now, now),
        )
        if cursor.rowcount:
            return job_id, True
        row = self.connection.execute(
            "SELECT job_id FROM workflow_jobs WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        return str(row["job_id"]), False

    def _terminal(
        self,
        job_id: str,
        *,
        worker_id: str,
        outcome: str,
        result: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        retry_at: int | None = None,
        expected_attempt_count: int | None = None,
    ) -> None:
        with self.commit_guard.transaction():
            now = _now()
            job = self.connection.execute(
                "SELECT status, lease_owner, lease_expires_at, kind, payload_json, attempt_count "
                "FROM workflow_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if job is not None and job["status"] == "cancelled":
                raise JobCancelledError("job was cancelled")
            if (
                job is None
                or job["status"] != "running"
                or job["lease_owner"] != worker_id
            ):
                raise LeaseLostError("job is not leased by this worker")
            if expected_attempt_count is not None and job["attempt_count"] != expected_attempt_count:
                raise LeaseLostError("job lease was lost")
            deadline = job["lease_expires_at"]
            self.commit_guard.assert_unexpired(deadline)
            current_payload = json.loads(str(job["payload_json"]))
            request_changed_while_running = (
                outcome == "succeeded"
                and job["kind"] == JobKind.PUBLISH.value
                and result is not None
                and (int(current_payload.get("transcript_id", -1))
                     != int(result.get("requested_transcript_id", -1))
                     or current_payload.get("publication_request_id")
                     != result.get("publication_request_id"))
            )
            target_status = "queued" if retry_at is not None or request_changed_while_running else outcome
            self._assert_job_transition(job_id, target_status)
            self.connection.execute(
                    """UPDATE workflow_jobs
                   SET status = ?, available_at = ?, lease_owner = NULL, lease_expires_at = NULL,
                       last_error_code = ?, updated_at = ? WHERE job_id = ?""",
                (
                    target_status,
                    retry_at if retry_at is not None else now,
                    error_code,
                    now,
                    job_id,
                ),
            )
            cursor = self.connection.execute(
                """UPDATE workflow_attempts
                   SET finished_at = ?, outcome = ?, error_code = ?, result_json = ?
                   WHERE job_id = ? AND worker_id = ? AND outcome = 'running'""",
                (now, outcome, error_code, _json(result or {}), job_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("running attempt is missing")
            self.commit_guard.assert_unexpired(deadline)
            if retry_at is not None:
                raise ValueError("inline retry requires guarded retry; record failure then requeue explicitly")

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> WorkflowJob:
        kind = JobKind(str(row["kind"]))
        part_id = int(row["video_part_id"]) if row["video_part_id"] is not None else None
        profile_id = int(row["profile_id"]) if row["profile_id"] is not None else None
        payload = json.loads(str(row["payload_json"]))
        validate_payload(kind, payload, part_id=part_id, profile_id=profile_id)
        return WorkflowJob(
            job_id=str(row["job_id"]),
            kind=kind,
            video_part_id=part_id,
            profile_id=profile_id,
            policy_key=row["policy_key"],
            payload=payload,
            status=JobStatus(str(row["status"])),
            attempt_count=int(row["attempt_count"]),
            lease_owner=row["lease_owner"],
        )
