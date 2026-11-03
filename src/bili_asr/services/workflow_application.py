"""Workflow use cases and per-invocation adapter composition."""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bili_asr.archive_session import ArchiveSession
from bili_asr.editorial import EditorialConfig
from bili_asr.storage.editorial import EditorialRepository
from bili_asr.storage.workflow import WorkflowRepository
from bili_asr.storage.workflow_selection import WorkflowSelection, resolve_workflow_selection
from bili_asr.transcript_selection import choose_transcript
from bili_asr.workflow import ExecutionSummary, WorkflowExecutor
from bili_asr.workflow_models import AsrPolicy, AsrProfile, JobKind, WorkflowPlan
from bili_asr.workflow_roles import WORKER_ROLES, PROCESSING_KINDS


@dataclass(frozen=True)
class PlannedWorkflow:
    selection: WorkflowSelection
    profile_id: int
    jobs: WorkflowPlan


class WorkflowApplication:
    def __init__(self, session: ArchiveSession):
        self.session = session
        self.repository = WorkflowRepository(session.connection)

    def plan(self, *, part_ids: Iterable[int] | None, bvids: Iterable[str] | None,
             page_index: int | None, policy: AsrPolicy, profile: AsrProfile,
             editorial_config: Mapping[str, Any] | None) -> PlannedWorkflow:
        selection = resolve_workflow_selection(self.session.connection, part_ids=part_ids,
                                               bvids=bvids, page_index=page_index)
        for part_id in selection.part_ids:
            if self.session.connection.execute(
                "SELECT 1 FROM part_audio_objects WHERE video_part_id=?", (part_id,)
            ).fetchone() is None:
                raise ValueError(f"part {part_id} has no imported audio; use input import first")
        profile_id, jobs = self.repository.plan_with_profile(
            part_ids=selection.part_ids, policy=policy, profile=profile,
            editorial_config=editorial_config)
        return PlannedWorkflow(selection, profile_id, jobs)

    def publish(self, part_ids: Iterable[int], *, preserve_transcript: bool = False,
                dry_run: bool = False) -> list[dict[str, Any]]:
        if dry_run and not preserve_transcript:
            raise ValueError("publication dry-run requires --preserve-transcript")
        selection = resolve_workflow_selection(self.session.connection, part_ids=part_ids)
        if preserve_transcript:
            return self._refresh_publications(selection, dry_run=dry_run)
        selected = []
        for target in selection.targets:
            # The processing unit ID is authoritative for every platform. BVID is
            # a compatibility field and is absent for a YouTube unit.
            candidate = choose_transcript(self.session.connection.execute(
                "SELECT transcript_id,source_kind,language,version FROM transcripts WHERE video_part_id=?",
                (target.video_part_id,)).fetchall())
            if candidate is None:
                raise ValueError(f"no stored transcript for {target.work_id}")
            selected.append((target, int(candidate["transcript_id"])))
        with self.repository.commit_guard.transaction():
            return [self._request_publication(target.work_id, target.video_part_id, transcript_id)
                    for target, transcript_id in selected]

    def _refresh_publications(self, selection: WorkflowSelection, *, dry_run: bool) -> list[dict[str, Any]]:
        from bili_asr.services.publication_refresh import latest_publication, publication_fence
        from bili_asr.storage.artifact_catalog import require_artifact_catalog
        require_artifact_catalog(self.session.connection, required=True)
        # One write reservation validates every selected target before queueing.
        # Dry-run uses the caller's read-only connection and never installs extensions.
        def prepare():
            selected = []
            for target in selection.targets:
                publication = latest_publication(self.session.connection, target.video_part_id)
                if publication is None:
                    raise ValueError(f"no published bundle for {target.work_id}")
                selected.append((target, publication, publication_fence(self.session.connection, publication)))
            return selected
        if dry_run:
            return [{"work_id": target.work_id, "transcript_id": publication["transcript_id"],
                     "preserve_publication": fence, "status": "planned"}
                    for target, publication, fence in prepare()]
        with self.repository.commit_guard.transaction():
            results = []
            for target, publication, fence in prepare():
                job_id, created = self.repository.enqueue_publication(
                    video_part_id=target.video_part_id, transcript_id=publication["transcript_id"],
                    force=True, preserve_publication=fence)
                results.append({"work_id": target.work_id, "transcript_id": publication["transcript_id"],
                                "job_id": job_id, "created": int(created),
                                "status": self.repository.explain_job(job_id)["status"]})
            return results

    def _request_publication(self, work_id: str, part_id: int, transcript_id: int) -> dict[str, Any]:
        job_id, created = self.repository.enqueue_publication(
            video_part_id=part_id, transcript_id=transcript_id, force=True)
        return {"work_id": work_id, "transcript_id": transcript_id, "job_id": job_id,
                "status": self.repository.explain_job(job_id)["status"], "created": int(created)}

    def proofread(self, *, part_id: int | None, base_transcript_id: int | None,
                  reference_transcript_id: int | None, no_reference: bool,
                  config: EditorialConfig, refresh_input: bool = False) -> dict[str, Any]:
        editorial = EditorialRepository(self.session.connection, commit_guard=self.repository.commit_guard)
        if base_transcript_id is not None:
            base = editorial.read_source(base_transcript_id)
            part_id = base.video_part_id
            default_reference = editorial.latest_reference(part_id, base.language) if base.source_kind == "asr-local" else None
        else:
            if part_id is None:
                raise ValueError("proofreading requires a video part or base transcript")
            base_transcript_id, default_reference = editorial.latest_sources(part_id)
        reference_id = None if no_reference else (
            reference_transcript_id if reference_transcript_id is not None else default_reference)
        prepared = editorial.build_input(base_transcript_id, reference_id, config, reuse_existing=not refresh_input)
        with self.repository.commit_guard.transaction():
            editorial.store_input(prepared)
            proof_id, render_id, _, _ = self.repository.enqueue_editorial(
                video_part_id=part_id, input_id=prepared["input_id"])
        return {"input_id": prepared["input_id"], "chunks": len(prepared["chunks"]),
                "job_id": proof_id, "render_job_id": render_id}

    def render(self, revision_id: str, template_version: str | None = None) -> str:
        prepared, _ = EditorialRepository(self.session.connection).revision(revision_id)
        job_id, _ = self.repository.request_document(
            video_part_id=prepared["snapshot"]["video_part_id"], revision_id=revision_id,
            template_version=template_version)
        return job_id

    def proofread_many(self, part_ids: Iterable[int], *, config: EditorialConfig,
                       no_reference: bool = False, refresh_input: bool = False) -> list[dict]:
        """Freeze every selected input and enqueue the batch in one transaction."""
        ids = tuple(dict.fromkeys(part_ids))
        if not ids or any(type(part_id) is not int or part_id <= 0 for part_id in ids):
            raise ValueError("select positive part IDs")
        editorial = EditorialRepository(self.session.connection, commit_guard=self.repository.commit_guard)
        results = []
        with self.repository.commit_guard.transaction():
            for part_id in ids:
                base, reference = editorial.latest_sources(part_id)
                prepared = editorial.build_input(base, None if no_reference else reference, config,
                                                  reuse_existing=not refresh_input)
                editorial.store_input(prepared)
                proof_id, render_id, _, _ = self.repository.enqueue_editorial(
                    video_part_id=part_id, input_id=prepared["input_id"])
                results.append({"part_id": part_id, "input_id": prepared["input_id"],
                                "chunks": len(prepared["chunks"]), "job_id": proof_id,
                                "render_job_id": render_id})
        return results

    def run(self, *, worker_id: str, sessdata: str | None,
            only_editorial: bool = False, limit: int | None = None,
            kinds: tuple[JobKind, ...] | None = None, role: str | None = None,
            drain_file: str | None = None, drain_timeout_seconds: float | None = 60,
            gpu_session: str = "persistent", config_resolver=None,
            asr_prefetch: bool = False, asr_prefetch_bytes: int = 64 * 1024 * 1024,
            poll_interval_seconds: float = 0, shutdown_event: threading.Event | None = None,
            source_registry=None, warmup_audio: str | None = None,
            warmup_timeout_seconds: float = 300, cache_root: str | None = None,
            cache_max_bytes: int = 10 * 1024**3, storage_targets: dict[str, Path] | None = None) -> ExecutionSummary:
        from contextlib import ExitStack, contextmanager

        from bili_asr.editorial_runtime import EditorialWorkflowHandlers
        from bili_asr.workflow_runtime import ArchiveWorkflowHandlers

        if role is not None and role not in WORKER_ROLES:
            raise ValueError("unknown worker role")
        if sum((bool(only_editorial), kinds is not None, role is not None)) > 1:
            raise ValueError("choose only one of only-editorial, role or kind")
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be finite and nonnegative")
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("limit must be positive")
        if gpu_session == "legacy" and (warmup_audio is not None or cache_root is not None):
            raise ValueError("explicit warmup/cache options require a supervised inference session")
        if not math.isfinite(warmup_timeout_seconds) or warmup_timeout_seconds <= 0:
            raise ValueError("warmup timeout must be finite and positive")
        if warmup_audio is not None:
            sentinel = Path(warmup_audio)
            if not sentinel.is_absolute() or not sentinel.is_file():
                raise ValueError("warmup audio must be an existing absolute file")
        selected = WORKER_ROLES[role] if role is not None else (
            WORKER_ROLES["editorial"] if only_editorial else (kinds or PROCESSING_KINDS))
        if any(kind not in PROCESSING_KINDS for kind in selected):
            raise ValueError("collection jobs are not available in this processing repository")
        registered = {}
        asr_completed = False
        with ExitStack() as resources:
            if self.repository.has_manuscript_contract() or only_editorial:
                editorial = EditorialWorkflowHandlers(
                    EditorialRepository(self.session.connection, commit_guard=self.repository.commit_guard),
                    self.repository, archive_root=Path(self.session.archive_root),
                    artifact_roots=self.session.artifact_roots)
                resources.callback(editorial.close)
                registered.update(editorial.handlers())
            if not only_editorial:
                archive = ArchiveWorkflowHandlers(
                    self.session.connection, self.repository, archive_root=self.session.archive_root,
                    sessdata=sessdata, artifact_roots=self.session.artifact_roots,
                    gpu_session=gpu_session, config_resolver=config_resolver,
                    cache_root=cache_root, cache_max_bytes=cache_max_bytes,
                    asr_prefetch=asr_prefetch, asr_prefetch_bytes=asr_prefetch_bytes)
                resources.callback(archive.close)
                registered.update(archive.handlers())
            stop = shutdown_event or threading.Event()
            from bili_asr.workflow_shutdown import WorkerShutdown
            shutdown = resources.enter_context(WorkerShutdown(
                stop, drain_file=drain_file, timeout=drain_timeout_seconds).install())
            from bili_asr.services.worker_health import WorkerHealth
            health = resources.enter_context(WorkerHealth(
                Path(self.session.archive_root), role=role or ("editorial" if only_editorial else "mixed"),
                stop=stop))
            @contextmanager
            def attempt_scope():
                with health.attempt(), shutdown.attempt():
                    yield
            def prepare(candidate, checkpoint):
                if candidate.profile_id is not None:
                    health.profile(self.repository.profile_digest(candidate.profile_id))
                if not only_editorial:
                    archive.prepare_candidate(candidate, checkpoint, timeout_seconds=warmup_timeout_seconds,
                                              audio_path=warmup_audio)
            def on_success(job, _result):
                nonlocal asr_completed
                asr_completed = asr_completed or job.kind is JobKind.ASR
            from bili_asr.services.artifact_consumer import candidate_artifact_access
            from bili_asr.storage.artifact_online import require_artifact_online
            access = (lambda candidate: candidate_artifact_access(self.session.connection,
                self.repository, self.session.artifact_roots, candidate, storage_targets=storage_targets or {})) if (
                    not only_editorial and require_artifact_online(self.session.connection)) else None
            executor = WorkflowExecutor(
                self.repository, worker_id=worker_id, handlers=registered,
                kinds=tuple(registered) if selected is None else tuple(dict.fromkeys(selected)),
                drain_requested=shutdown.requested,
                drain_timeout_seconds=drain_timeout_seconds,
                drain_checkpoint=shutdown.checkpoint, attempt_scope=attempt_scope,
                prepare_candidate=prepare if not only_editorial and gpu_session != "legacy" else None,
                candidate_access=access, on_success=on_success)
            succeeded = failed = cancelled = blocked = 0
            final_summary = None
            while True:
                remaining = None if limit is None else limit - succeeded - failed - cancelled
                summary = executor.run(limit=remaining)
                succeeded += summary.succeeded
                failed += summary.failed
                cancelled += summary.cancelled
                blocked = summary.blocked
                if (not poll_interval_seconds or executor.drain_requested()
                        or (limit is not None and succeeded + failed + cancelled >= limit)):
                    final_summary = ExecutionSummary(succeeded, failed, succeeded + failed + cancelled == 0, cancelled, blocked)
                    break
                stop.wait(poll_interval_seconds)
        if final_summary is None:
            raise RuntimeError("workflow execution ended without a summary")
        if asr_completed:
            from bili_asr.services.artifact_policy_configuration import read_policy
            policy_table = self.session.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifact_policies'"
            ).fetchone()
            policy = (read_policy(self.session.connection, "default", default_version=1)
                      if policy_table is not None else None)
            if policy is not None and policy.get("version") == 2 and policy.get("mode") != "off":
                from bili_asr.services.artifact_policy import run_policy_once
                run_policy_once(self.session.artifact_roots, policy_id="default",
                                storage_targets=storage_targets or {},
                                connection=self.session.connection)
        return final_summary
