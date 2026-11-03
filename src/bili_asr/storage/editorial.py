"""Editorial persistence adapter; workers consume frozen data, never latest files."""

from __future__ import annotations

import json
from contextlib import contextmanager
import sqlite3
import time
from typing import Any
from uuid import uuid4

from bili_asr.editorial import ARTIFACT_ROLES, EditorialConfig, canonical, digest, language_key, prepare_input
from bili_asr.storage.editorial_facts import template_for_input, input_for_proofread, load_input, revision
from bili_asr.storage.database import require_editorial_schema
from bili_asr.storage.transcripts import TranscriptRepository
from bili_asr.workflow_models import WorkflowJob
from bili_asr.storage.job_commit import JobCommitGuard
from bili_asr.transcript_selection import choose_transcript
from bili_asr.storage.archive_contracts import runtime_contract, UNIVERSAL_V2, register_frozen_version




class EditorialRepository:
    def __init__(self, connection: sqlite3.Connection, *, commit_guard: JobCommitGuard | None = None):
        require_editorial_schema(connection)
        self.connection = connection
        self.commit_guard = commit_guard or JobCommitGuard(connection)

    @contextmanager
    def owned_transaction(self, job: WorkflowJob):
        require_editorial_schema(self.connection)
        with self.commit_guard.owned_transaction(job):
            yield

    def read_source(self, transcript_id: int):
        row = self.connection.execute("SELECT * FROM transcripts WHERE transcript_id = ?", (transcript_id,)).fetchone()
        if row is None:
            raise ValueError("unknown transcript ID")
        return TranscriptRepository(self.connection).read_transcript(
            int(row["video_part_id"]), row["source_kind"], row["language"], int(row["version"]))

    def latest_sources(self, part_id: int) -> tuple[int, int | None]:
        base = self.connection.execute(
            "SELECT * FROM transcripts WHERE video_part_id = ? AND source_kind = 'asr-local' "
            "ORDER BY created_at DESC, transcript_id DESC LIMIT 1", (part_id,)).fetchone()
        if base is None:
            raise ValueError("part has no stored ASR transcript")
        return int(base["transcript_id"]), self.latest_reference(part_id, base["language"])

    def latest_reference(self, part_id: int, language: str) -> int | None:
        rows = self.connection.execute(
            "SELECT * FROM transcripts WHERE video_part_id = ? "
            "AND source_kind IN ('subtitle-ai', 'subtitle-cc')", (part_id,))
        reference = choose_transcript(row for row in rows if language_key(row["language"]) == language_key(language))
        return None if reference is None else int(reference["transcript_id"])

    def prepare(self, base_id: int, reference_id: int | None, config: EditorialConfig) -> dict[str, Any]:
        prepared = self.build_input(base_id, reference_id, config)
        with self.connection:
            return self.store_input(prepared)

    def build_input(self, base_id: int, reference_id: int | None, config: EditorialConfig, *,
                    reuse_existing: bool = False) -> dict[str, Any]:
        require_editorial_schema(self.connection)
        base = self.read_source(base_id)
        reference = self.read_source(reference_id) if reference_id is not None else None
        if reuse_existing:
            existing = self._reusable_input(base, reference, config)
            if existing is not None:
                return existing
        if runtime_contract(self.connection) == UNIVERSAL_V2:
            from bili_asr.editorial_input_v2 import prepare_input_v2
            from bili_asr.storage.metadata import MetadataRepository
            return prepare_input_v2(base, reference, config,
                                    source_metadata=MetadataRepository(self.connection).read_source_metadata(base.video_part_id))
        return prepare_input(base, reference, config,
                             metadata=self.metadata(base.video_part_id))

    def _reusable_input(self, base, reference, config: EditorialConfig) -> dict[str, Any] | None:
        """Prefer verified persisted work independently of the current encoding.

        Later metadata observations and a newly installed prompt do not silently
        request paid recomputation. Explicit refresh/repair still builds current
        input. Terminal job status is preserved by the original dedupe identity.
        """
        from bili_asr.editorial import segments

        def source(record):
            if record is None:
                return None
            return {"transcript_id": record.transcript_id, "content_sha256": record.content_sha256,
                    "source_kind": record.source_kind, "language": record.language, "segments": segments(record)}

        expected_base, expected_reference = source(base), source(reference)
        rows = self.connection.execute(
            "SELECT input_id FROM editorial_inputs WHERE video_part_id=? AND base_transcript_id=? "
            "AND reference_transcript_id IS ? ORDER BY created_at DESC,rowid DESC",
            (base.video_part_id, base.transcript_id, None if reference is None else reference.transcript_id))
        for row in rows:
            prepared = self.load_input(row["input_id"])
            snapshot = prepared["snapshot"]
            stored_config = snapshot.get("config")
            if not isinstance(stored_config, dict):
                raise ValueError("invalid persisted editorial configuration")
            # These scheduling limits were absent from historical frozen inputs.
            # Default replanning retains their already prepared chunks. Only this
            # comparison supplies the fixed compatibility defaults; neither the
            # historical JSON nor its identity is rewritten.
            comparison_config = dict(stored_config)
            # ``thinking_mode`` was added after the first frozen editorial
            # inputs were written.  The historical default is enabled, so
            # comparing it here must not manufacture a new input identity.
            comparison_config.setdefault("thinking_mode", "enabled")
            comparison_config.setdefault("max_chunk_segments", 256)
            comparison_config.setdefault("max_chunk_chars", 20_000)
            # New request/recovery policies are installed only by explicit
            # refresh/repair; ordinary replanning reuses historical paid work.
            comparison_config.setdefault("request_policy", config.request_policy)
            comparison_config.setdefault("temperature", None)
            comparison_config.setdefault("max_chunk_regenerations", 0 if config.request_policy == "legacy-v1" else 2)
            comparison_config.setdefault("budget_policy", config.budget_policy)
            if "budget_plan" not in snapshot and comparison_config["budget_policy"] == "legacy-v1":
                comparison_config["budget_policy"] = config.budget_policy
            comparison_config.setdefault("chunk_preset", "baseline-v1")
            if comparison_config != config.to_dict():
                continue
            if (snapshot.get("video_part_id") != base.video_part_id or snapshot.get("base") != expected_base
                    or snapshot.get("reference") != expected_reference
                    or not isinstance(snapshot.get("system_prompt"), str)
                    or snapshot.get("prompt_sha256") != digest(snapshot["system_prompt"])):
                raise ValueError("persisted editorial input does not match its transcript evidence")
            return prepared
        return None

    def store_input(self, prepared: dict[str, Any]) -> dict[str, Any]:
        """Persist an already prepared immutable input in the caller's transaction."""
        snapshot = prepared["snapshot"]
        base_id = snapshot["base"]["transcript_id"]
        reference_id = None if snapshot["reference"] is None else snapshot["reference"]["transcript_id"]
        if digest(snapshot) != prepared["input_id"]:
            raise ValueError("editorial input identity mismatch")
        self.connection.execute(
            "INSERT OR IGNORE INTO editorial_inputs VALUES (?, ?, ?, ?, ?, ?)",
            (prepared["input_id"], snapshot["video_part_id"], base_id, reference_id, canonical(prepared), int(time.time())))
        if runtime_contract(self.connection) == UNIVERSAL_V2:
            existing_version = self.connection.execute("SELECT version FROM editorial_input_versions WHERE input_id=?",
                                                       (prepared["input_id"],)).fetchone()
            version = int(existing_version[0]) if existing_version else 2
        else:
            version = 1
        register_frozen_version(self.connection, "input", prepared["input_id"], version)
        stored = self.load_input(prepared["input_id"])
        if stored != prepared:
            raise RuntimeError("input identity collision")
        return stored

    def load_input(self, input_id: str) -> dict[str, Any]:
        require_editorial_schema(self.connection)
        return load_input(self.connection, input_id)

    def bind_job_input(self, job_id: str, input_id: str) -> None:
        """Associate the immutable input inside the caller's owned transaction."""
        if not self.connection.in_transaction:
            raise RuntimeError("editorial input binding requires a write transaction")
        self.connection.execute("INSERT OR IGNORE INTO editorial_job_inputs VALUES (?, ?)", (job_id, input_id))

    def begin_call(self, job: WorkflowJob, input_id: str, chunk_id: str, request: dict[str, Any], *,
                   invocation_kind: str = "provider", recovery: dict[str, Any] | None = None,
                   replay_of_call_id: str | None = None) -> str:
        now, call_id = int(time.time()), uuid4().hex
        request_fingerprint = self.request_fingerprint(input_id, chunk_id, request)
        with self.owned_transaction(job):
            if recovery is not None and invocation_kind == "provider":
                from bili_asr.editorial_recovery import exhausted
                consumed = len(self.physical_calls(input_id, chunk_id))
                if consumed >= recovery["max_physical_calls"]:
                    raise exhausted(chunk_id, consumed, recovery["max_physical_calls"])
            attempt = self.connection.execute(
                "SELECT attempt_id FROM workflow_attempts WHERE job_id = ? AND worker_id = ? AND outcome = 'running'",
                (job.job_id, job.lease_owner)).fetchone()
            if attempt is None:
                raise RuntimeError("running editorial attempt missing")
            # Keep the fixed manuscript schema compatible with existing
            # archives.  The fingerprint is carried in the durable request
            # envelope rather than adding columns to the contract.
            stored_request = {"request": request, "request_fingerprint": request_fingerprint}
            stored_request.update(envelope_version=2, invocation_kind=invocation_kind)
            if recovery is not None:
                stored_request["recovery"] = recovery
            if replay_of_call_id is not None:
                stored_request["replay_of_call_id"] = replay_of_call_id
            self.connection.execute(
                "INSERT INTO editorial_model_calls(call_id, attempt_id, input_id, chunk_id, request_json, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (call_id, attempt["attempt_id"], input_id, chunk_id, canonical(stored_request), now))
        return call_id

    def physical_calls(self, input_id: str, chunk_id: str) -> list[dict[str, Any]]:
        """All sends, including unfinished/failed reservations, across attempts."""
        from bili_asr.storage.editorial_call_budget import PHYSICAL_CALL_SQL
        from bili_asr.storage.editorial_replay import ReplayEvidenceError, _stored_request
        rows = self.connection.execute(
            f"SELECT m.call_id,m.input_id,m.chunk_id,m.request_json,m.response_json,m.error_code,"
            f"{PHYSICAL_CALL_SQL} AS is_physical FROM editorial_model_calls m "
            "WHERE m.input_id=? AND m.chunk_id=? ORDER BY m.rowid", (input_id, chunk_id))
        result = []
        for row in rows:
            stored, _ = _stored_request(row)
            if "request" not in stored:
                # Some historical packages stored the raw HTTP body. Preserve
                # it and conservatively count its invocation as a send.
                stored = {"request": stored}
            kind = stored.get("invocation_kind", "provider")
            if kind not in {"provider", "response_replay", "local_precheck"}:
                raise ReplayEvidenceError(row["call_id"], "invocation_kind_invalid")
            if kind != "provider":
                continue
            try:
                response = None if row["response_json"] is None else json.loads(row["response_json"])
                if response is not None and not isinstance(response, dict):
                    raise TypeError()
            except (ValueError, TypeError):
                raise ReplayEvidenceError(row["call_id"], "response_json_invalid") from None
            if not row["is_physical"]:
                continue
            result.append(dict(row, stored_request=stored, response=response))
        return result

    def recovery_status_for_job(self, job_id: str) -> dict[str, Any]:
        """Read-only gate for maintenance; no budget resets or job mutation."""
        from bili_asr.editorial_recovery import provider_recovery_blocker, recoverable
        input_id = input_for_proofread(self.connection, job_id)
        if input_id is None:
            return {"eligible": False, "exhausted": False, "reason": "unfrozen_input"}
        prepared = self.load_input(input_id)
        config = EditorialConfig.from_snapshot(prepared["snapshot"]["config"])
        legacy = config.request_policy != "deepseek-v2"
        free_replay = False
        transient = False
        reference_failure = False
        for chunk in prepared["chunks"]:
            if self.chunk_result(input_id, chunk["chunk_id"]) is not None:
                continue
            calls = self.physical_calls(input_id, chunk["chunk_id"])
            blocker = provider_recovery_blocker(calls[-1] if calls else None)
            if blocker is not None:
                return {"eligible": False, "exhausted": False, "reason": blocker.error_code}
            maximum = 1 + config.max_chunk_regenerations
            if not legacy and len(calls) >= maximum:
                # A complete but unvalidated successful envelope can still be
                # replayed for free even after its final reservation was spent.
                last = calls[-1]
                if last["response"] is None or last["error_code"] is not None:
                    return {"eligible": False, "exhausted": True, "reason": "physical_budget_exhausted"}
            if not calls:
                continue
            last = calls[-1]
            diagnostic = last["stored_request"].get("validation_diagnostic")
            if legacy and last["response"] is not None:
                # Read-only validation of saved evidence proves this specific
                # failure without changing the historical request or input.
                from bili_asr.deepseek import DeepSeekError, parse_response
                from bili_asr.editorial import (
                    RevisionValidationError,
                    validate_revision,
                )
                from bili_asr.editorial_recovery import diagnostic_for_error
                try:
                    validate_revision(chunk, parse_response(last["response"]))
                except RevisionValidationError as exc:
                    diagnostic = diagnostic_for_error(exc)
                except DeepSeekError:
                    pass
            if (legacy or config.max_chunk_regenerations) and recoverable(diagnostic):
                reference_failure = True
            if last["response"] is not None and last["error_code"] is None:
                free_replay = True
            # The outer maintenance policy verifies the concrete HTTP/transport
            # failure. Here only prove that another reservation fits the frozen
            # budget; unknown sends remain consumed.
            if last["response"] is None and last["error_code"] in {
                    None, "DeepSeekTransportError", "deepseek_transport_failed", "Timeout", "ReadTimeout", "ConnectTimeout",
                    "ConnectionError", "ConnectionResetError", "RequestException",
                    "TransportError", "TimeoutError", "TimeoutException", "ConnectError",
                    "WriteTimeout", "PoolTimeout", "NetworkError", "RemoteProtocolError"}:
                transient = True
        reason = ("cross_paragraph_reference" if reference_failure else "response_replay" if free_replay
                  else "remaining_physical_budget" if transient else "no_allowlisted_failure")
        return {"eligible": reference_failure or free_replay or transient, "exhausted": False, "reason": reason}

    def mark_validation_failure(self, call_id: str, error_code: str, diagnostic: dict[str, Any] | None) -> None:
        """Reject a cached envelope without changing its completion chronology."""
        with self.connection:
            row = self.connection.execute("SELECT request_json FROM editorial_model_calls WHERE call_id=?", (call_id,)).fetchone()
            stored = json.loads(row[0])
            if diagnostic is not None:
                if "request" not in stored:
                    stored = {"request": stored}
                stored["validation_diagnostic"] = diagnostic
                self.connection.execute("UPDATE editorial_model_calls SET request_json=? WHERE call_id=?",
                                        (canonical(stored), call_id))
            self.connection.execute("UPDATE editorial_model_calls SET error_code=? WHERE call_id=?", (error_code, call_id))

    @staticmethod
    def request_fingerprint(input_id: str, chunk_id: str, request: dict[str, Any]) -> str:
        """Derive the stable identity used to avoid paid duplicate requests."""
        return digest({"input_id": input_id, "chunk_id": chunk_id, "request": request})

    def finish_call(self, call_id: str, response: dict[str, Any] | None, error_code: str | None = None, *,
                    validation_diagnostic: dict[str, Any] | None = None) -> None:
        require_editorial_schema(self.connection)
        with self.connection:
            if validation_diagnostic is not None:
                row = self.connection.execute("SELECT request_json FROM editorial_model_calls WHERE call_id=?", (call_id,)).fetchone()
                stored = json.loads(row[0])
                stored["validation_diagnostic"] = validation_diagnostic
                self.connection.execute("UPDATE editorial_model_calls SET request_json=? WHERE call_id=?", (canonical(stored), call_id))
            self.connection.execute(
                "UPDATE editorial_model_calls SET response_json = ?, error_code = ?, finished_at = ? "
                "WHERE call_id = ?",
                (None if response is None else canonical(response), error_code, int(time.time()), call_id))

    def usage_for_call(self, call_id: str) -> dict[str, Any] | None:
        """Return normalized provider usage while retaining the raw response.

        Usage fields intentionally remain derived from ``response_json`` so the
        fixed manuscript schema stays readable by existing archives.
        """
        row = self.connection.execute(
            "SELECT response_json FROM editorial_model_calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        if row is None or row["response_json"] is None:
            return None
        try:
            from bili_asr.deepseek import normalize_usage
            return normalize_usage(json.loads(row["response_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid stored editorial model response") from exc

    def completed_call_for_request(self, request_fingerprint: str) -> dict[str, Any] | None:
        """Return an accepted provider response for a stable request identity.

        A response can exist without a chunk checkpoint when a worker crashed
        between provider completion and local validation.  Runtime code may use
        this read-only lookup to validate that response before issuing a paid
        duplicate request.
        """
        require_editorial_schema(self.connection)
        found = self.completed_call_record(request_fingerprint)
        return None if found is None else found["response"]

    def completed_call_record(self, request_fingerprint: str, *, input_id: str | None = None,
                              chunk_id: str | None = None) -> dict[str, Any] | None:
        from bili_asr.storage.editorial_replay import completed_record
        return completed_record(self.connection, request_fingerprint, input_id=input_id, chunk_id=chunk_id)

    def assert_lease(self, job: WorkflowJob) -> None:
        self.commit_guard.assert_lease(job)

    def chunk_result(self, input_id: str, chunk_id: str) -> list[dict[str, Any]] | None:
        require_editorial_schema(self.connection)
        row = self.connection.execute("SELECT blocks_json FROM editorial_chunk_results WHERE input_id = ? AND chunk_id = ?",
                                      (input_id, chunk_id)).fetchone()
        return json.loads(row["blocks_json"]) if row else None

    def save_chunk(self, job: WorkflowJob, input_id: str, chunk_id: str, call_id: str, blocks: list[dict[str, Any]]) -> None:
        with self.owned_transaction(job):
            self.connection.execute("INSERT OR IGNORE INTO editorial_chunk_results VALUES (?, ?, ?, ?)",
                                    (input_id, chunk_id, call_id, canonical(blocks)))

    def commit_revision(self, job: WorkflowJob, prepared: dict[str, Any]) -> str:
        blocks = []
        for chunk in prepared["chunks"]:
            result = self.chunk_result(prepared["input_id"], chunk["chunk_id"])
            if result is None:
                raise ValueError("cannot commit an incomplete editorial revision")
            blocks.extend(result)
        expected = [s["segment_id"] for s in prepared["snapshot"]["base"]["segments"]]
        if [segment_id for b in blocks for segment_id in b["segment_ids"]] != expected:
            raise ValueError("revision does not cover the exact base segment order")
        revision_id = digest({"input_id": prepared["input_id"], "blocks": blocks})
        needs_review = any(b["issues"] for b in blocks) or prepared["snapshot"]["unassigned_reference_ids"]
        with self.owned_transaction(job):
            self.connection.execute("INSERT OR IGNORE INTO editorial_revisions VALUES (?, ?, ?, ?, ?, ?)",
                                    (revision_id, prepared["input_id"], job.job_id, canonical(blocks),
                                     "needs-review" if needs_review else "ai-unreviewed", int(time.time())))
        return revision_id

    def revision(self, revision_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        require_editorial_schema(self.connection)
        return revision(self.connection, revision_id)

    def revision_for_job(self, job_id: str) -> str:
        require_editorial_schema(self.connection)
        row = self.connection.execute("SELECT revision_id FROM editorial_revisions WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            # Different jobs may share the same immutable input/checkpoints.
            row = self.connection.execute("SELECT r.revision_id FROM editorial_revisions r JOIN editorial_job_inputs i "
                                          "ON r.input_id = i.input_id WHERE i.job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("proofreading revision not committed")
        return str(row["revision_id"])

    def metadata(self, part_id: int) -> dict[str, Any]:
        from bili_asr.storage.metadata import MetadataRepository

        snapshot = MetadataRepository(self.connection).read_source_metadata(part_id)
        return {"bvid": snapshot.ref.external_video_id, "page_index": snapshot.ref.part_index,
                "title": snapshot.title, "source_metadata": snapshot.to_dict()}

    def preflight_artifacts(self, revision_id: str, template: str, artifacts: dict[str, tuple[str, str]]) -> None:
        """Check the complete fixed pair before a renderer writes any bytes."""
        require_editorial_schema(self.connection)
        prepared, _ = self.revision(revision_id)
        expected_template = template_for_input(self.connection, prepared["input_id"])
        if template != expected_template or set(artifacts) != set(ARTIFACT_ROLES):
            raise ValueError("unsupported document artifact contract")
        part_id = prepared["snapshot"]["video_part_id"]
        prefix = f"documents/part-{part_id}/{revision_id}/{template}"
        for name, (path, sha256) in artifacts.items():
            if (path != f"{prefix}/{name}" or len(sha256) != 64
                    or any(c not in "0123456789abcdef" for c in sha256)):
                raise ValueError("invalid document artifact identity")
            row = self.connection.execute("SELECT * FROM document_artifacts WHERE revision_id = ? "
                                          "AND template_version = ? AND artifact_name = ?", (revision_id, template, name)).fetchone()
            if row and (row["relative_path"] != path or row["content_sha256"] != sha256
                        or row["manuscript_role"] != ARTIFACT_ROLES[name]):
                raise ValueError("document content drift requires a new template version")
            owner = self.connection.execute("SELECT revision_id, template_version, artifact_name "
                                            "FROM document_artifacts WHERE relative_path = ?", (path,)).fetchone()
            if owner and tuple(owner) != (revision_id, template, name):
                raise ValueError("document artifact path belongs to another identity")

    def record_artifacts(self, revision_id: str, template: str, artifacts: dict[str, tuple[str, str]]) -> None:
        # The rendering worker owns the surrounding fenced transaction.
        self.preflight_artifacts(revision_id, template, artifacts)
        for name, (path, sha256) in artifacts.items():
            self.connection.execute(
                "INSERT OR IGNORE INTO document_artifacts "
                "(revision_id, template_version, artifact_name, manuscript_role, relative_path, content_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (revision_id, template, name, ARTIFACT_ROLES[name], path, sha256))
