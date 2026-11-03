"""Concrete, per-invocation handlers for the workflow control plane.

This adapter holds only short-lived clients and model runners.  Durable state
is written exclusively through the metadata/transcript repositories and the
workflow repository; a later worker can therefore resume any job independently.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

from bili_asr import archive, asr
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.asr.session import AsrInferenceSession, InferenceRequest
from bili_asr.asr.deadlines import inference_budget
from bili_asr.exception_diagnostics import capture_failure
from bili_asr.workflow_errors import JobExecutionError
from bili_asr.formatting import duration_s_from_ms, pubdate_utc
from bili_asr.services.transcript_projection import Candidate, ordered_candidates, writer_segments
from bili_asr.services.workflow_audio_access import retained_audio, processing_audio
from bili_asr.services.workflow_audio_input import VerifiedAudioInput, verified_audio_input
from bili_asr.storage import (
    AcquisitionRunRecord,
    TranscriptRepository,
    TranscriptSegmentRecord,
)
from bili_asr.storage.sources import SourceRepository, acquisition_selector
from bili_asr.storage.workflow import WorkflowRepository
from bili_asr.transcript_selection import choose_transcript, language_family as language_key
from bili_asr.workflow import (
    LeaseRenewalFailed, WorkerDrainTimeout, WorkerInputUnavailable,
    WorkerPreparingDrain, attempt_checkpoint,
)
from bili_asr.workflow_models import JobKind, LeaseLostError, WorkflowJob
from bili_asr.workflow_payloads import decode_job_payload
from bili_asr.workflow_runtime_ports import (
    ConfigResolver,
    InferenceSession,
    JobHandler,
    RunnerFactory,
    TimeoutTranscriber,
    WorkflowAsrRunner,
)


class ArchiveWorkflowHandlers:
    """Build concrete handlers without giving them scheduling responsibilities."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        repository: WorkflowRepository,
        *,
        archive_root: str | os.PathLike[str],
        sessdata: str | None,
        artifact_roots: ArtifactRoots | None = None,
        runner_factory: RunnerFactory | None = None,
        timeout_transcriber: TimeoutTranscriber | None = None,
        inference_session: InferenceSession | None = None,
        gpu_session: str = "legacy",
        config_resolver: ConfigResolver | None = None,
        asr_prefetch: bool = False,
        asr_prefetch_bytes: int = 64 * 1024 * 1024,
        cache_root: str | None = None,
        cache_max_bytes: int = 10 * 1024**3,
    ) -> None:
        self.connection = connection
        self.repository = repository
        self.artifact_roots = artifact_roots or ArtifactRoots.of(archive_root)
        self.archive_root = self.artifact_roots.archive_root
        self.archive_root.mkdir(parents=True, exist_ok=True)
        self.sessdata = sessdata
        self.runner_factory = runner_factory or asr.ASRRunner
        self.timeout_transcriber = timeout_transcriber or asr.transcribe_with_timeout
        if gpu_session not in {"legacy", "persistent", "oneshot"}:
            raise ValueError("unknown GPU session mode")
        self.config_resolver = config_resolver or (lambda config: (config, {}))
        self.inference_session = inference_session or (
            AsrInferenceSession(persistent=gpu_session == "persistent", prefetch=asr_prefetch,
                                prefetch_bytes=asr_prefetch_bytes, cache_root=cache_root,
                                cache_max_bytes=cache_max_bytes) if gpu_session != "legacy" else None)
        self.asr_prefetch, self.asr_prefetch_bytes = asr_prefetch, asr_prefetch_bytes
        self._runners: dict[int, WorkflowAsrRunner] = {}
        self._runner_bindings: dict[int, str] = {}

    def handlers(self) -> Mapping[JobKind, JobHandler]:
        return {
            JobKind.ASR: self.local_asr,
            JobKind.PUBLISH: self.publish,
        }

    def close(self) -> None:
        for runner in self._runners.values():
            runner.release()
        self._runners.clear()
        self._runner_bindings.clear()
        if self.inference_session is not None:
            self.inference_session.close()

    def _inference_checkpoint(self, job: WorkflowJob) -> None:
        # Query authoritative cancellation before the heartbeat token, so the executor
        # distinguishes a cancelled job from a lease reclaimed by another attempt.
        self.repository.assert_lease(job)
        attempt_checkpoint(job)

    def prepare_candidate(self, job: WorkflowJob, checkpoint, *, timeout_seconds: float,
                          audio_path: str | None = None) -> None:
        """Prepare the candidate's exact GPU configuration before the executor claims it."""
        if job.kind != JobKind.ASR or self.inference_session is None:
            return
        config, binding = self.config_resolver(self.repository.profile(job.profile_id).asr_config())
        if not config.device.casefold().startswith(("cuda", "rocm")):
            return
        request = InferenceRequest(job.job_id, "", job.attempt_count,
                                   self.repository.profile_digest(job.profile_id), binding)
        self.inference_session.prepare(config, request=request, timeout_seconds=timeout_seconds,
                                       audio_path=audio_path, checkpoint=checkpoint)





    def local_asr(self, job: WorkflowJob) -> Mapping[str, Any]:
        from bili_asr.services.artifact_coordination import consumer_pin
        from contextlib import nullcontext
        decode_job_payload(job)
        self._inference_checkpoint(job)
        part = self._part(job)
        audio_result = processing_audio(self.connection, self.repository, job)
        retained = retained_audio(self.connection, job.video_part_id,
                                  audio_result)
        pin = (nullcontext() if retained is None else
               consumer_pin(self.connection, self.artifact_roots, retained["object_id"],
                            owner=f"asr:{job.job_id}:{job.attempt_count}"))
        with pin, verified_audio_input(self.connection, self.artifact_roots, int(part["video_part_id"]),
                                      audio_result, retained=retained,
                                      checkpoint=lambda: self._inference_checkpoint(job)) as audio_input:
            return self._local_asr(job, audio_input, retained)

    def _local_asr(self, job: WorkflowJob, audio_input: VerifiedAudioInput,
                   retained: Mapping[str, Any] | None) -> Mapping[str, Any]:
        decode_job_payload(job)
        self.repository.assert_lease(job)
        if job.profile_id is None:
            raise RuntimeError("missing_profile")
        part = self._part(job)
        audio_result, audio_path = audio_input.evidence, audio_input.path
        profile = self.repository.profile(job.profile_id)
        config, binding_identity = self.config_resolver(profile.asr_config())
        try:
            budget = inference_budget(config, duration_ms=audio_result.get("duration_ms"))
        except ValueError:
            raise JobExecutionError("asr_deadline_duration_unavailable", {
                "phase": "audio_prepare", "reason": "verified-duration-required",
                "profile_id": job.profile_id, "job_id": job.job_id, "attempt_count": job.attempt_count}) from None
        inference_request = InferenceRequest(job.job_id, job.lease_owner or "", job.attempt_count,
            self.repository.profile_digest(job.profile_id), binding_identity)
        referenced = job.payload.get("reference_transcript_id")
        reference_id = None if referenced is None else int(referenced)
        # A planned job freezes a caption only when it was already stored at
        # planning time. The subtitle producer is an independent, concurrently
        # created sibling, so on a fresh part that value is always None. Resolve
        # the current caption at execution instead: this is evidence for the
        # decode, not frozen input, and a missing caption must never block ASR.
        if reference_id is None:
            reference_id = self._available_caption_transcript_id(part)
        paired_text = self._paired_subtitle_text(reference_id)
        started = int(time.time())
        run_id = str(uuid4())
        selector_kind, selector_target = acquisition_selector(part)
        transcripts = TranscriptRepository(self.connection, write_transaction=lambda: self.repository.owned_transaction(job))
        transcripts.start_acquisition_run(
            AcquisitionRunRecord(
                run_id=run_id,
                kind="asr",
                selector_kind=selector_kind,
                selector_target=selector_target,
                requested_limit=1,
                credential_present=False,
                started_at=started,
            )
        )
        failure_phase = "inference"
        try:
            self.repository.assert_lease(job)
            diagnostics: dict[str, Any] = {}
            if profile.device.casefold().startswith(("cuda", "rocm")) or budget.policy == "audio-scaled":
                infer = self.timeout_transcriber if self.inference_session is None else self.inference_session.transcribe
                session_args = {} if self.inference_session is None else {
                    "request": inference_request, "checkpoint": lambda: self._inference_checkpoint(job)}
                if retained is not None:
                    session_args["artifact_context"] = {"archive_root": str(self.artifact_roots.archive_root),
                        "artifact_root": str(self.artifact_roots.artifact_root) if self.artifact_roots.configured else None,
                        "object_id": retained["object_id"]}
                segments, provenance, coverage = infer(
                    config,
                    os.fspath(audio_path),
                    paired_subtitle_text=paired_text,
                    timeout_seconds=budget.total_seconds,
                    diagnostics_sink=diagnostics,
                    **session_args,
                )
                language = asr.provenance_language(provenance)
            else:
                runner = (self._runner(job.profile_id) if not binding_identity else
                          self._runner(job.profile_id, config=config, binding_request=inference_request))
                segments = asr.two_pass_transcribe(
                    runner, os.fspath(audio_path), paired_subtitle_text=paired_text,
                    checkpoint_path=os.path.join(self.artifact_roots.write_base,
                                                 ".asr-checkpoints", f"{job.job_id}.json")
                )
                provenance = runner.provenance()
                language = asr.provenance_language(provenance)
                coverage = asr.transcribed_coverage(runner)
                diagnostics_reader = getattr(runner, "diagnostics", None)
                if callable(diagnostics_reader):
                    diagnostics = dict(diagnostics_reader())
            if budget.policy == "audio-scaled":
                diagnostics["deadline"] = budget.evidence()
            audio_input.assert_unchanged()
            self._inference_checkpoint(job)
            from bili_asr.services.asr_result import preserve_inference
            failure_phase = "result_commit"
            finished = int(time.time())
            stored, admission = preserve_inference(
                self.repository, job, run_id=run_id, segments=segments,
                language=language, model_name=profile.model_name,
                model_revision=profile.model_revision, started_at=started,
                finished_at=finished, coverage=coverage,
                asr_evidence={
                    "schema_version": 1,
                    "profile_id": job.profile_id,
                    "config_sha256": self.repository.profile_digest(job.profile_id),
                    "reference_transcript_id": reference_id,
                    "runtime_binding": dict(binding_identity),
                    "audio": dict(audio_result),
                    "provenance": provenance,
                    "diagnostics": diagnostics,
                },
            )
            # Completed inference is distinct from usable output. The original
            # evidence commits before admission can block downstream work.
            transcripts.finish_acquisition_run(run_id, int(time.time()), outcome="complete")
        except BaseException as exc:
            transcripts.finish_acquisition_run(
                run_id, int(time.time()), outcome="failed"
            )
            # Preserve reviewed errors and ownership/shutdown signals for the
            # executor. All other ordinary failures need this run's context,
            # including parent-side validation and injected runner exceptions.
            if isinstance(exc, Exception) and not isinstance(exc, (
                    JobExecutionError, LeaseLostError, LeaseRenewalFailed,
                    WorkerDrainTimeout, WorkerPreparingDrain, WorkerInputUnavailable)):
                from bili_asr.workflow_failure_diagnostics import execution_failure
                error_code, diagnostic = execution_failure(exc)
                details = {**diagnostic, **capture_failure(exc, phase=failure_phase), "run_id": run_id,
                           "profile_id": job.profile_id, "job_id": job.job_id, "attempt_count": job.attempt_count}
                raise JobExecutionError(error_code, details) from exc
            raise
        from bili_asr.storage.asr_quality import require_admission

        require_admission(admission, stage="asr_quality")
        publication_job_id, _ = self.repository.request_publication(
            video_part_id=int(part["video_part_id"]), transcript_id=stored.transcript_id, source_job=job,
        )
        return {
            "run_id": run_id,
            "transcript_id": stored.transcript_id,
            "version": stored.version,
            "source_kind": "asr-local",
            "publication_job_id": publication_job_id,
            "quality": admission,
        }

    def publish(self, job: WorkflowJob) -> Mapping[str, Any]:
        from bili_asr.services.artifact_coordination import resource_fence
        from bili_asr.storage.artifact_catalog import require_artifact_catalog
        if not require_artifact_catalog(self.connection):
            return self._publish(job)
        with resource_fence(self.artifact_roots, f"publication-part:{job.video_part_id}", exclusive=True):
            return self._publish(job)

    def _publish(self, job: WorkflowJob) -> Mapping[str, Any]:
        """Project the currently preferred stored transcript into an archive bundle."""
        decode_job_payload(job)
        self.repository.assert_lease(job)
        if job.video_part_id is None:
            raise RuntimeError("missing_video_part")
        source_part = SourceRepository(self.connection).part(job.video_part_id)
        refresh = job.payload.get("preserve_publication")
        rows = [
            {**source_part, "part_title": source_part["title"], **dict(row)}
            for row in self.connection.execute(
                """SELECT * FROM transcripts WHERE video_part_id = ?
                   ORDER BY source_kind, language, version DESC""", (job.video_part_id,),
            )
        ]
        if refresh is not None:
            rows = [row for row in rows if row["transcript_id"] == job.payload["transcript_id"]]
        preserved_raw = None
        preserved_publication = None
        if refresh is not None:
            from bili_asr.services.artifact_groups import (
                object_storage_key,
                preserve_existing_publication,
            )
            from bili_asr.services.publication_refresh import assert_publication_fence
            from bili_asr.storage.artifact_catalog import require_artifact_catalog
            require_artifact_catalog(self.connection, required=True)
            with self.repository.owned_transaction(job):
                preserved_publication = assert_publication_fence(
                    self.connection, job.video_part_id, int(job.payload["transcript_id"]), refresh)
                # Reverify the actual current bytes even when a group was captured
                # earlier. The catalog alone cannot prove the mutable slot.
                group_id = preserve_existing_publication(self.connection, self.artifact_roots, preserved_publication)
                raw_object = self.connection.execute(
                    "SELECT object_id FROM artifact_group_members WHERE group_id=? AND role='raw_path'",
                    (group_id,)).fetchone()[0]
            from bili_asr.artifact_inventory import require_regular_file
            old_raw_path = self.artifact_roots.write_base / object_storage_key(raw_object)
            require_regular_file(old_raw_path)
            encoded = old_raw_path.read_bytes()
            import hashlib
            if hashlib.sha256(encoded).hexdigest() != raw_object:
                raise ValueError("preserved transcript raw bytes changed")
            preserved_raw = json.loads(encoded)
        candidate = self._publication_candidate(rows)
        transcript_id = int(candidate.transcript["transcript_id"])
        segment_rows = self.connection.execute(
            """SELECT start_ms, end_ms, text FROM transcript_segments
               WHERE transcript_id = ? ORDER BY ordinal""",
            (transcript_id,),
        ).fetchall()
        segments = writer_segments(
            tuple(
                TranscriptSegmentRecord(
                    start_ms=int(row["start_ms"]), end_ms=int(row["end_ms"]), text=str(row["text"])
                )
                for row in segment_rows
            )
        )
        part = candidate.part
        source_kind = str(candidate.transcript["source_kind"])
        entry = {
            "bvid": part["bvid"],
            "cid": part["cid"],
            "page_index": part["page_index"],
            "work_id": candidate.work_id,
            "title": part["part_title"],
            "video_title": part["video_title"],
            "duration_s": duration_s_from_ms(int(part["duration_ms"])),
            "pubdate_str": "" if part["pubdate"] is None else pubdate_utc(int(part["pubdate"])),
        }
        if source_part["platform"] != "bilibili":
            entry.update(platform=source_part["platform"], external_video_id=source_part["external_video_id"])
        from bili_asr.storage.metadata import MetadataRepository
        metadata_repository = MetadataRepository(self.connection)
        owns_read = not self.connection.in_transaction
        if owns_read:
            self.connection.execute("BEGIN")
        try:
            values = metadata_repository.read_source_metadata_export_many([job.video_part_id])[job.video_part_id]
        finally:
            if owns_read:
                self.connection.rollback()
        metadata = values["sourceMetadata"]
        entry.update(pubdateUnix=metadata["pubdateUnix"],
                     sourcePublishedAt=metadata["sourcePublishedAt"], **values)
        asr_provenance = None
        if source_kind == "asr-local":
            model = self.connection.execute(
                """SELECT model_name, revision FROM asr_models
                   WHERE model_id = ?""",
                (candidate.transcript["model_id"],),
            ).fetchone()
            asr_provenance = {
                "model": str(model["model_name"]) if model is not None else "unknown",
                "revision": str(model["revision"]) if model is not None else "",
            }
        if preserved_raw is not None:
            # Preserve ASR provenance, coverage and character timing evidence.
            # Refreshing metadata must not replace them with today's defaults.
            asr_provenance = preserved_raw.get("provenance")
        paths = {key: os.path.relpath(path, self.artifact_roots.write_base).replace(os.sep, "/")
                 for key, path in archive.bundle_paths(self.artifact_roots.write_base, entry).items()}

        @contextmanager
        def publication_guard(invalidate):
            # Marker, final bytes and publication fact share cancellation's lock.
            # The writer invalidates the marker if this transaction cannot commit.
            with self.repository.owned_transaction(job, on_rollback=invalidate):
                if refresh is not None:
                    assert_publication_fence(self.connection, job.video_part_id, transcript_id, refresh)
                yield
                # A part owns one mutable bundle slot. Same-second republishing
                # an earlier transcript must still become the newest committed
                # publication fact, independently of its original row id.
                from bili_asr.storage.workflow_outputs import record_bundle_publication
                publication_id = record_bundle_publication(self.connection, part_id=int(part["video_part_id"]),
                                                           transcript_id=transcript_id, paths=paths)
                from bili_asr.storage.artifact_catalog import require_artifact_catalog
                if require_artifact_catalog(self.connection):
                    from bili_asr.services.artifact_groups import (
                        preserve_transcript_bundle,
                    )
                    preserve_transcript_bundle(self.connection, self.artifact_roots, publication_id, paths)

        from bili_asr.storage.artifact_catalog import require_artifact_catalog
        if require_artifact_catalog(self.connection):
            from bili_asr.services.artifact_groups import preserve_existing_publication
            previous_publication = self.connection.execute("SELECT p.* FROM workflow_publications p LEFT JOIN artifact_publication_groups g USING(publication_id) WHERE p.video_part_id=? ORDER BY p.published_at DESC,p.publication_id DESC LIMIT 1", (int(part["video_part_id"]),)).fetchone()
            if previous_publication is not None and self.connection.execute("SELECT 1 FROM artifact_publication_groups WHERE publication_id=?", (previous_publication["publication_id"],)).fetchone() is None:
                with self.repository.owned_transaction(job):
                    preserve_existing_publication(self.connection, self.artifact_roots, previous_publication)

        # ``write_archive`` performs the pure publication contract checks
        # (cue ordering, character timing shape, and serializable bundle
        # inputs) before replacing any archive files. Convert only those
        # ValueErrors into a stable workflow classification. Lease/cancel
        # failures from ``before_replace`` and filesystem failures must keep
        # their original exception type so the executor can handle them as
        # operational failures.
        from bili_asr.workflow_errors import JobExecutionError
        try:
            paths = archive.write_archive(
                self.artifact_roots.write_base,
                entry,
                segments,
                source="asr" if source_kind == "asr-local" else "subtitle",
                raw=preserved_raw,
                asr_provenance=asr_provenance,
                characters=preserved_raw.get("characters") if preserved_raw is not None else None,
                coverage=preserved_raw.get("coverage") if preserved_raw is not None else None,
                before_replace=lambda: self.repository.assert_lease(job),
                publication_guard=publication_guard,
            )
        except ValueError:
            raise JobExecutionError(
                "publication_contract_failed",
                {
                    "stage": "publication_validation",
                    "reason_code": "bundle_contract_invalid",
                    "transcript_id": transcript_id,
                },
            ) from None
        return {
            "requested_transcript_id": int(job.payload["transcript_id"]),
            "publication_request_id": job.payload.get("publication_request_id"),
            "transcript_id": transcript_id,
            "source_kind": source_kind,
            **paths,
        }

    def _runner(self, profile_id: int, *, config=None, binding_request=None) -> WorkflowAsrRunner:
        existing = self._runners.get(profile_id)
        config = config or self.repository.profile(profile_id).asr_config()
        binding_request = binding_request or InferenceRequest("", "", 0, "", {})
        binding_key = AsrInferenceSession.configuration_key(config, binding_request)
        if existing is not None and self._runner_bindings.get(profile_id) != binding_key:
            existing.release()
            existing = None
        if existing is not None:
            return existing
        runner = self.runner_factory(config)
        if self.asr_prefetch:
            configure = getattr(runner, "configure_prefetch", None)
            if callable(configure):
                configure(enabled=True, max_bytes=self.asr_prefetch_bytes)
        self._runners[profile_id] = runner
        self._runner_bindings[profile_id] = binding_key
        return runner

    def _part(self, job: WorkflowJob) -> Mapping[str, Any]:
        if job.video_part_id is None:
            raise RuntimeError("missing_video_part")
        return SourceRepository(self.connection).part(job.video_part_id)

    def _paired_subtitle_text(self, transcript_id: int | None) -> str | None:
        if transcript_id is None:
            return None
        rows = self.connection.execute(
            """
            SELECT ts.text FROM transcripts AS t
            JOIN transcript_segments AS ts ON ts.transcript_id = t.transcript_id
            WHERE t.transcript_id = ?
            ORDER BY ts.ordinal
            """,
            (transcript_id,),
        ).fetchall()
        return "".join(str(row["text"]) for row in rows) or None

    def _publication_candidate(self, rows: list[dict[str, Any]]) -> Candidate:
        """The transcript this part publishes, honouring the ASR quality gate.

        ``asr-local`` outranks captions in the shared preference order, so a
        finished decode normally becomes the published text. That rank alone
        would let an unusable or unproven decode displace a perfectly good
        caption — and, worse, fail the whole publish job. Instead: when the
        preferred candidate is ASR whose admission is not ``usable``, fall back
        to the best non-ASR candidate. Publication therefore stays as reliable
        as it was under caption-first ranking, while still preferring ASR when
        its quality supports it.

        With no usable caption to fall back to, the original verdict stands and
        the job fails as before: silently publishing a questionable decode would
        be worse than a visible, retryable failure.
        """
        candidates = ordered_candidates(rows)
        if len(candidates) != 1:
            raise RuntimeError("transcript_missing")
        candidate = candidates[0]
        if candidate.transcript["source_kind"] != "asr-local":
            return candidate
        from bili_asr.storage.asr_quality import assess_transcript, require_admission

        report = assess_transcript(self.connection, int(candidate.transcript["transcript_id"]))
        if report["status"] == "usable":
            return candidate
        captions = ordered_candidates(
            row for row in rows if row["source_kind"] != "asr-local")
        if captions:
            return captions[0]
        require_admission(report, stage="publication_quality")
        return candidate

    def _available_caption_transcript_id(self, part: Mapping[str, Any]) -> int | None:
        """The preferred stored caption for this part, or None.

        Preference is the shared ``choose_transcript`` policy — the same order
        the editorial reference uses — so the caption that informs the decode is
        the one a reader would see. Captions of another language family are not
        evidence for this part's decode: the aligner matches this audio, and a
        translation would inject wording the audio does not contain.

        This reads stored transcripts only and never calls the provider. An
        absent or language-mismatched caption yields None; empty caption text
        is handled by ``_paired_subtitle_text``.
        """
        if part.get("original_language") is not None:
            wanted = language_key(str(part["original_language"]), False)
        else:
            wanted = None
        rows = self.connection.execute(
            "SELECT transcript_id,source_kind,language,version FROM transcripts "
            "WHERE video_part_id = ? AND source_kind IN ('subtitle-ai', 'subtitle-cc')",
            (int(part["video_part_id"]),),
        ).fetchall()
        eligible = [row for row in rows
                    if wanted is None or language_key(
                        str(row["language"]), row["source_kind"] == "subtitle-ai") == wanted]
        chosen = choose_transcript(eligible)
        return None if chosen is None else int(chosen["transcript_id"])
