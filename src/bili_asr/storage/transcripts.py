"""Transcripts implementation."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Callable

import bili_asr.storage.database as _module_storage_database
from bili_asr.storage.models import (
    ALLOWED_CAPTION_SOURCE_KINDS,
    ALLOWED_LOCAL_TRANSCRIPT_SOURCE_KINDS,
    ALLOWED_SOURCE_KINDS,
    MAX_TIMELINE_MS,
    AcquisitionRunRecord,
    TranscriptRecord,
    TranscriptSegmentRecord,
    TranscriptWriteResult,
    _choice,
    _error_code,
    _integer,
    _text,
)


def _language_code(value: object) -> str:
    """Return the trimmed caption language code ``value`` carries.

    The stored language is the upstream ``lan`` the gateway already normalized,
    trimmed and non-empty.  Trimming happens here as well so ``' zh-CN '`` and
    ``'zh-CN'`` name one transcript identity.
    """
    return _text(value, "language").strip()


def _segment_content_sha256(triples: list[list[int | str]]) -> str:
    """Digest the canonical segment JSON exactly as the contract defines it."""
    canonical = json.dumps(triples, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TranscriptRepository:
    """Repository for acquired transcripts and their acquisition process records.

    Commit boundaries per public method:

    - ``start_acquisition_run`` commits its own insert so a part that fails
      later can roll back without losing the run parent.
    - ``finish_acquisition_run`` commits its own terminal transition.
    - ``record_acquired_transcript`` owns one transaction: it validates the
      part and the arguments, computes the content hash, appends a version with
      its segments only when the content is new, writes the attempt row with
      the resulting ``'stored'``/``'unchanged'`` outcome, and commits — or
      rolls back wholly, so a version is never stored without the attempt
      evidence that produced it.
    - ``record_subtitle_attempt`` owns one transaction for one ``'no-subtitle'``
      or ``'failed'`` attempt and commits it.
    - ``read_transcript``, ``list_transcript_versions``,
      ``list_pending_subtitle_parts``, ``count_pending_subtitle_parts``,
      ``list_selected_parts``, ``list_stored_transcripts`` and
      ``read_video_pubdates`` — the class's reads of the ``videos`` table, which
      :class:`MetadataRepository` owns — never write and never commit: they
      return the stored rows as they are — a typed ``TranscriptRecord`` for one
      stored version, ``sqlite3.Row`` view data otherwise.

    Versions are immutable: no method rewrites or deletes a transcript row, a
    segment row, or an attempt row, and no method recomputes the outcome of a
    run that already finished.  Attempt evidence is append-only per
    ``(run_id, video_part_id)`` and is never a terminal per-part state, so a
    part recorded without a caption stays re-attemptable in a later run.

    Do not compose ``start_acquisition_run``, ``finish_acquisition_run``,
    ``record_acquired_transcript`` or ``record_subtitle_attempt`` inside a
    :meth:`MetadataRepository.transaction` group: each commits independently
    and would commit the enclosing group's earlier writes.

    The connection must come with ``row_factory = sqlite3.Row`` and
    ``PRAGMA foreign_keys`` enabled — exactly the state :func:`open_database`
    establishes — and must carry the transcript-schema contract: the
    constructor rejects anything else, so a caller that skipped
    :func:`require_subtitle_schema` still meets the bounded rebuild error
    instead of a raw ``sqlite3.OperationalError`` from its first query.
    """

    def __init__(
        self, connection: sqlite3.Connection, *,
        write_guard: Callable[[], None] | None = None,
        write_transaction: Callable[[], AbstractContextManager[None]] | None = None,
    ):
        if write_guard is not None and write_transaction is not None:
            raise ValueError("transcript writes need one ownership guard")
        _module_storage_database._validate_connection(connection)
        _module_storage_database.require_subtitle_schema(connection)
        self.connection = connection
        self._write_guard = write_guard
        self._write_transaction = write_transaction

    @contextmanager
    def _result_transaction(self):
        # Workflow callers supply the shared JobCommitGuard's whole context,
        # so every authoritative result uses its entry and pre-commit fence.
        if self._write_transaction is not None:
            with self._write_transaction():
                yield
            return
        with _module_storage_database._transaction(self.connection):
            if self._write_guard is not None:
                if self.connection.in_transaction:
                    raise RuntimeError("guarded transcript transaction cannot be nested")
                self.connection.execute("BEGIN IMMEDIATE")
                self._write_guard()
            yield
            # Retain the callback API for existing library callers, including
            # refusal when a large segment write outlives its entry check.
            if self._write_guard is not None:
                self._write_guard()

    def start_acquisition_run(self, run: AcquisitionRunRecord) -> None:
        """Insert one new acquisition run.

        ``run_id`` is the primary key and is never reused: a duplicate raises
        ``sqlite3.IntegrityError``.  The run is committed on its own so a
        failed part can roll back without deleting its parent.
        """
        if not isinstance(run, AcquisitionRunRecord):
            raise TypeError("run must be an AcquisitionRunRecord")
        with self._result_transaction():
            self._insert_acquisition_run(run)

    def _insert_acquisition_run(self, run: AcquisitionRunRecord) -> None:
        self.connection.execute(
            """
            INSERT INTO acquisition_runs(
                run_id, kind, selector_kind, selector_target, requested_limit,
                credential_present, started_at, finished_at, outcome
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.kind,
                run.selector_kind,
                run.selector_target,
                run.requested_limit,
                int(run.credential_present),
                run.started_at,
                run.finished_at,
                run.outcome,
            ),
        )
        # A run is a lifecycle parent for attempt transactions. Commit its
        # start independently so a failed part can roll back without it.

    def finish_acquisition_run(
        self, run_id: str, finished_at: int, *, outcome: str | None = None
    ) -> str:
        """Finish a run with its outcome and finish timestamp; return the outcome.

        The outcome is the explicit ``outcome`` when given, otherwise it is
        derived from the run's attempt rows: ``'failed'`` when every attempt of
        a non-empty set failed, ``'partial'`` when failed and non-failed
        attempts coexist, and ``'complete'`` otherwise — including a run with
        no attempts, which means the bounded work set was empty and nothing
        failed.  The ordering baseline is the run's stored ``started_at``, not
        a caller-supplied clock.  Re-finishing is rejected: a run whose stored
        outcome is already terminal raises ``sqlite3.IntegrityError`` and keeps
        both its outcome and its ``finished_at``.

        ``run_id`` is validated by the same helper every other identifier in
        this class goes through, so a malformed one is answered with the same
        bounded message its siblings produce.
        """
        run_id = _text(run_id, "run_id")
        _integer(finished_at, "finished_at", minimum=0)
        if outcome is not None:
            _choice(outcome, "outcome", _module_storage_database._TERMINAL_ACQUISITION_OUTCOMES)
        run_row = self.connection.execute(
            "SELECT started_at, outcome FROM acquisition_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run_row is None:
            raise sqlite3.IntegrityError(f"unknown run_id: {run_id}")
        if run_row["outcome"] != "running":
            raise sqlite3.IntegrityError(
                f"run {run_id} already finished with outcome {run_row['outcome']}"
            )
        if finished_at < int(run_row["started_at"]):
            raise ValueError("finished_at must not precede started_at")
        resolved = (
            outcome if outcome is not None else self._run_outcome_from_attempts(run_id)
        )
        # Failure cleanup stays possible after cancellation; success is fenced.
        context = self._result_transaction() if resolved != "failed" else _module_storage_database._transaction(self.connection)
        with context:
            self.connection.execute(
                "UPDATE acquisition_runs SET finished_at = ?, outcome = ? "
                "WHERE run_id = ? AND outcome = 'running'", (finished_at, resolved, run_id),
            )
            if self.connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise sqlite3.IntegrityError(f"run {run_id} is no longer running")
        return resolved

    def record_acquired_transcript(
        self,
        *,
        run_id: str,
        video_part_id: int,
        source_kind: str,
        language: str,
        segments: tuple[TranscriptSegmentRecord, ...],
        started_at: int,
        finished_at: int,
        created_at: int,
    ) -> TranscriptWriteResult:
        """Store one acquired caption body as a transcript version, idempotently.

        One transaction: the part and the arguments are validated, the content
        hash is computed over the canonical segment JSON, and then either
        nothing is written — when a version of ``(video_part_id, source_kind,
        language)`` already carries that hash, in which case the attempt is
        recorded ``'unchanged'`` and points at the version the operator already
        holds — or the next version is appended with its segments and the
        attempt is recorded ``'stored'``.  Either way the attempt row is
        written last and the transaction is committed; any failure — a write
        violation as much as an attempt row the key ``(run_id,
        video_part_id)`` already holds — rolls the whole call back, so no
        version is stored without its attempt evidence.

        Normalization at this boundary, not in the caller: the text of every
        segment is stored and hashed trimmed, the language is stored trimmed,
        and a millisecond value above :data:`MAX_TIMELINE_MS` is rejected with
        ``ValueError`` rather than reaching SQLite as an unrepresentable
        integer.  ``source_kind`` is one of the two caption kinds; an empty
        ``segments`` tuple, a non-positive ``video_part_id``, and an
        ``end_ms``/``start_ms`` violation are rejected with ``ValueError``.
        The run's outcome is left alone: a finished run is never recomputed.
        """
        run_id = _text(run_id, "run_id")
        video_part_id = _integer(video_part_id, "video_part_id", minimum=1)
        source_kind = _choice(
            source_kind, "source_kind", ALLOWED_CAPTION_SOURCE_KINDS
        )
        language = _language_code(language)
        segment_records = tuple(segments)
        if not segment_records:
            raise ValueError("a transcript requires at least one segment")
        started_at = _integer(started_at, "started_at", minimum=0)
        finished_at = _integer(finished_at, "finished_at", minimum=0)
        created_at = _integer(created_at, "created_at", minimum=0)
        if finished_at < started_at:
            raise ValueError("finished_at must not precede started_at")
        canonical = self._canonical_segments(segment_records)
        content_sha256 = _segment_content_sha256(canonical)

        with self._result_transaction():
            self._require_video_part(video_part_id)
            self._require_acquisition_run(run_id)
            existing_row = self.connection.execute(
                """
                SELECT transcript_id, version
                FROM transcripts
                WHERE video_part_id = ? AND source_kind = ? AND language = ?
                  AND content_sha256 = ?
                """,
                (video_part_id, source_kind, language, content_sha256),
            ).fetchone()
            if existing_row is None:
                version = self._next_transcript_version(
                    video_part_id, source_kind, language
                )
                cursor = self.connection.execute(
                    """
                    INSERT INTO transcripts(
                        video_part_id, source_kind, language, model_id, version,
                        content_sha256, created_at
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        video_part_id,
                        source_kind,
                        language,
                        version,
                        content_sha256,
                        created_at,
                    ),
                )
                transcript_id = int(cursor.lastrowid)
                self.connection.executemany(
                    """
                    INSERT INTO transcript_segments(
                        transcript_id, ordinal, start_ms, end_ms, text
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (transcript_id, ordinal, start_ms, end_ms, text)
                        for ordinal, (start_ms, end_ms, text) in enumerate(canonical)
                    ],
                )
                outcome = "stored"
            else:
                transcript_id = int(existing_row["transcript_id"])
                version = int(existing_row["version"])
                outcome = "unchanged"
            self.connection.execute(
                """
                INSERT INTO acquisition_attempts(
                    run_id, video_part_id, outcome, error_code, transcript_id,
                    started_at, finished_at
                ) VALUES (?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    run_id,
                    video_part_id,
                    outcome,
                    transcript_id,
                    started_at,
                    finished_at,
                ),
            )

        return TranscriptWriteResult(
            outcome=outcome,
            transcript_id=transcript_id,
            version=version,
            content_sha256=content_sha256,
        )

    def record_local_transcript(
        self,
        *,
        run_id: str,
        video_part_id: int,
        language: str,
        segments: tuple[TranscriptSegmentRecord, ...],
        model_name: str,
        model_revision: str | None,
        started_at: int,
        finished_at: int,
        created_at: int,
        coverage: Mapping[str, Any] | None = None,
        asr_evidence: Mapping[str, Any] | None = None,
    ) -> TranscriptWriteResult:
        """Store one locally-produced transcript body as a transcript version.

        The explicitly-named sibling of :meth:`record_acquired_transcript` for
        the ``'asr-local'`` identity.  The caption writer keeps validating
        against :data:`ALLOWED_CAPTION_SOURCE_KINDS`; this entry point validates
        ``source_kind`` against ``{'asr-local'}`` alone, so the caption guard is
        provably unchanged for every existing caller (plan 20260929-asr-local-
        transcript-storage, Task 1 — option (a), a second named entry point).

        The write mirrors the caption path one transaction: the model identity
        is upserted into ``asr_models`` first (the ``transcripts.model_id``
        foreign key demands a row; nothing else inserts one), the content hash
        decides ``'stored'`` vs ``'unchanged'``, and the attempt row is written
        last and committed with the version, so a version is never stored
        without its attempt evidence.  ``model_revision`` is the caller's
        provenance string, defaulting to ``""`` when the runner names none.
        """

        run_id = _text(run_id, "run_id")
        video_part_id = _integer(video_part_id, "video_part_id", minimum=1)
        language = _language_code(language)
        model_name = _text(model_name, "model_name")
        revision = _text(model_revision, "model_revision") if model_revision else ""
        segment_records = tuple(segments)
        if not segment_records:
            raise ValueError("a transcript requires at least one segment")
        started_at = _integer(started_at, "started_at", minimum=0)
        finished_at = _integer(finished_at, "finished_at", minimum=0)
        created_at = _integer(created_at, "created_at", minimum=0)
        if finished_at < started_at:
            raise ValueError("finished_at must not precede started_at")
        source_kind = _choice(
            "asr-local", "source_kind", ALLOWED_LOCAL_TRANSCRIPT_SOURCE_KINDS
        )
        canonical = self._canonical_segments(segment_records)
        content_sha256 = _segment_content_sha256(canonical)
        coverage_row = None
        if coverage is not None:
            required = ("decoded_s", "produced_s", "coverage", "coverage_min", "coverage_short")
            if any(key not in coverage for key in required):
                raise ValueError("coverage evidence is incomplete")
            values = {key: coverage[key] for key in required}
            if any(isinstance(values[key], bool) for key in required[:-1]):
                raise ValueError("coverage evidence values must be numeric")
            if not isinstance(values["coverage_short"], bool):
                raise ValueError("coverage_short must be boolean")
            try:
                decoded_s = float(values["decoded_s"])
                produced_s = float(values["produced_s"])
                ratio = float(values["coverage"])
                coverage_min = float(values["coverage_min"])
                short = int(values["coverage_short"])
            except (TypeError, ValueError, OverflowError):
                raise ValueError("coverage evidence values are invalid") from None
            if (not all(math.isfinite(value) for value in (decoded_s, produced_s, ratio, coverage_min))
                    or decoded_s <= 0 or produced_s < 0 or produced_s > decoded_s
                    or ratio < 0 or ratio > 1 or coverage_min <= 0 or coverage_min > 1
                    or short not in (0, 1) or abs(ratio - produced_s / decoded_s) > 1e-6
                    or short != int(ratio < coverage_min)):
                raise ValueError("coverage evidence values are inconsistent")
            coverage_row = (decoded_s, produced_s, ratio, coverage_min, short)

        evidence_json = None
        if asr_evidence is not None:
            from bili_asr.contracts.registry import ASR_EVIDENCE_VERSION
            if asr_evidence.get("schema_version") != ASR_EVIDENCE_VERSION:
                raise ValueError("unsupported ASR evidence schema")
            evidence_json = json.dumps(dict(asr_evidence), ensure_ascii=False, sort_keys=True, allow_nan=False)

        with self._result_transaction():
            self._require_video_part(video_part_id)
            self._require_acquisition_run(run_id)
            model_row = self.connection.execute(
                "SELECT model_id FROM asr_models WHERE model_name = ? AND revision = ?",
                (model_name, revision),
            ).fetchone()
            if model_row is None:
                model_cursor = self.connection.execute(
                    "INSERT INTO asr_models(model_name, revision, created_at) "
                    "VALUES (?, ?, ?)",
                    (model_name, revision, created_at),
                )
                model_id = int(model_cursor.lastrowid)
            else:
                model_id = int(model_row["model_id"])
            existing_row = self.connection.execute(
                """
                SELECT transcript_id, version
                FROM transcripts
                WHERE video_part_id = ? AND source_kind = ? AND language = ?
                  AND content_sha256 = ?
                """,
                (video_part_id, source_kind, language, content_sha256),
            ).fetchone()
            if existing_row is None:
                version = self._next_transcript_version(
                    video_part_id, source_kind, language
                )
                cursor = self.connection.execute(
                    """
                    INSERT INTO transcripts(
                        video_part_id, source_kind, language, model_id, version,
                        content_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        video_part_id,
                        source_kind,
                        language,
                        model_id,
                        version,
                        content_sha256,
                        created_at,
                    ),
                )
                transcript_id = int(cursor.lastrowid)
                self.connection.executemany(
                    """
                    INSERT INTO transcript_segments(
                        transcript_id, ordinal, start_ms, end_ms, text
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (transcript_id, ordinal, start_ms, end_ms, text)
                        for ordinal, (start_ms, end_ms, text) in enumerate(canonical)
                    ],
                )
                outcome = "stored"
            else:
                transcript_id = int(existing_row["transcript_id"])
                version = int(existing_row["version"])
                outcome = "unchanged"
            self.connection.execute(
                """
                INSERT INTO acquisition_attempts(
                    run_id, video_part_id, outcome, error_code, transcript_id,
                    started_at, finished_at
                ) VALUES (?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    run_id,
                    video_part_id,
                    outcome,
                    transcript_id,
                    started_at,
                    finished_at,
                ),
            )
            if coverage_row is not None:
                self.connection.execute(
                    """
                    INSERT INTO transcript_coverage_attestations(
                        run_id, video_part_id, transcript_id, decoded_s, produced_s,
                        coverage, coverage_min, coverage_short
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, video_part_id, transcript_id, *coverage_row),
                )
            if evidence_json is not None:
                self.connection.execute(
                    "INSERT INTO transcript_asr_evidence "
                    "(run_id, video_part_id, transcript_id, schema_version, evidence_json) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (run_id, video_part_id, transcript_id, evidence_json),
                )

        return TranscriptWriteResult(
            outcome=outcome,
            transcript_id=transcript_id,
            version=version,
            content_sha256=content_sha256,
        )

    def read_transcript_coverage(self, run_id: str, video_part_id: int) -> dict[str, Any] | None:
        """Read the coverage evidence attached to one ASR run and part."""
        row = self.connection.execute(
            """SELECT run_id, video_part_id, transcript_id, decoded_s, produced_s,
                      coverage, coverage_min, coverage_short
                 FROM transcript_coverage_attestations
                WHERE run_id = ? AND video_part_id = ?""",
            (_text(run_id, "run_id"), _integer(video_part_id, "video_part_id", minimum=1)),
        ).fetchone()
        return dict(row) if row is not None else None

    def read_asr_evidence(self, run_id: str, video_part_id: int) -> dict[str, Any] | None:
        """Read exact run evidence, even when its text reused an existing version."""
        row = self.connection.execute(
            "SELECT evidence_json FROM transcript_asr_evidence WHERE run_id = ? AND video_part_id = ?",
            (_text(run_id, "run_id"), _integer(video_part_id, "video_part_id", minimum=1)),
        ).fetchone()
        return json.loads(row["evidence_json"]) if row is not None else None

    def record_subtitle_attempt(
        self,
        *,
        run_id: str,
        video_part_id: int,
        outcome: str,
        error_code: str | None,
        started_at: int,
        finished_at: int,
        credential_verified: bool = False,
        absence_verified: bool = False,
    ) -> None:
        """Record one attempt that produced no transcript.

        One transaction for one ``'no-subtitle'``/``'failed'`` attempt.  The
        attempt table's CHECK matrix is enforced here as well: a ``'failed'``
        attempt requires a bounded ``error_code``, a ``'no-subtitle'`` attempt
        carries either no code (upstream listed nothing) or exactly
        ``'not_found'`` (upstream signalled "not visible"), and neither outcome
        may reference a transcript.  The attempt row is therefore the whole
        evidence — append-only per ``(run_id, video_part_id)`` and never a
        terminal per-part state, so a part recorded here is re-attemptable in a
        later run. ``credential_verified`` attests a successful login check
        for this individual empty observation. Unchecked callers remain
        unverified even when their run carried a cookie. ``absence_verified``
        attests a definite not-found response from the listing itself; historical
        not-found codes without this evidence remain unverified.
        """
        run_id = _text(run_id, "run_id")
        video_part_id = _integer(video_part_id, "video_part_id", minimum=1)
        outcome = _choice(outcome, "outcome", _module_storage_database._NO_TRANSCRIPT_ATTEMPT_OUTCOMES)
        error_code = _error_code(error_code)
        if not isinstance(credential_verified, bool):
            raise TypeError("credential_verified must be a bool")
        if credential_verified and (outcome != "no-subtitle" or error_code is not None):
            raise ValueError("credential_verified applies to an empty subtitle observation")
        if not isinstance(absence_verified, bool):
            raise TypeError("absence_verified must be a bool")
        if absence_verified and (outcome != "no-subtitle" or error_code != "not_found"):
            raise ValueError("absence_verified applies to a listing not_found observation")
        if outcome == "failed" and error_code is None:
            raise ValueError("a failed attempt requires a bounded error_code")
        if outcome == "no-subtitle" and error_code not in (None, "not_found"):
            raise ValueError(
                "a no-subtitle attempt carries no error_code or not_found"
            )
        started_at = _integer(started_at, "started_at", minimum=0)
        finished_at = _integer(finished_at, "finished_at", minimum=0)
        if finished_at < started_at:
            raise ValueError("finished_at must not precede started_at")

        with self._result_transaction():
            self._require_video_part(video_part_id)
            self._require_acquisition_run(run_id)
            self.connection.execute(
                """
                INSERT INTO acquisition_attempts(
                    run_id, video_part_id, outcome, error_code, transcript_id,
                    started_at, finished_at, credential_verified, absence_verified
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)
                """,
                (run_id, video_part_id, outcome, error_code, started_at, finished_at,
                 int(credential_verified), int(absence_verified)),
            )

    def record_audio_attempt(
        self,
        *,
        run_id: str,
        video_part_id: int,
        error_code: str,
        started_at: int,
        finished_at: int,
    ) -> None:
        """Record a failed audio acquisition attempt for queue rotation.

        Audio failures are negative evidence only: successful audio is
        represented by ``part_audio_objects``.  Keeping the failed attempt
        under an ``audio`` run lets the queue order retries by recency without
        treating a failed download as usable media.
        """
        run_id = _text(run_id, "run_id")
        video_part_id = _integer(video_part_id, "video_part_id", minimum=1)
        error_code = _error_code(error_code)
        if error_code is None:
            raise ValueError("an audio failure requires a bounded error_code")
        started_at = _integer(started_at, "started_at", minimum=0)
        finished_at = _integer(finished_at, "finished_at", minimum=0)
        if finished_at < started_at:
            raise ValueError("finished_at must not precede started_at")
        with _module_storage_database._transaction(self.connection):
            self._require_video_part(video_part_id)
            run = self.connection.execute(
                "SELECT kind FROM acquisition_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise sqlite3.IntegrityError(f"unknown run_id: {run_id}")
            if run["kind"] != "audio":
                raise ValueError("audio attempts require an audio acquisition run")
            self.connection.execute(
                """
                INSERT INTO acquisition_attempts(
                    run_id, video_part_id, outcome, error_code, transcript_id,
                    started_at, finished_at
                ) VALUES (?, ?, 'failed', ?, NULL, ?, ?)
                """,
                (run_id, video_part_id, error_code, started_at, finished_at),
            )

    def record_asr_failure(
        self,
        *,
        run_id: str,
        video_part_id: int,
        error_code: str,
        started_at: int,
        finished_at: int,
    ) -> None:
        """Record a bounded failure for an ASR part during supervisor recovery.

        A timeout can terminate the worker before the normal per-part write-back
        reaches its ``finally`` block.  This method is deliberately idempotent:
        a successful attempt already written by the worker wins, while an
        unfinished part receives one retryable failed attempt.
        """

        run_id = _text(run_id, "run_id")
        video_part_id = _integer(video_part_id, "video_part_id", minimum=1)
        error_code = _error_code(error_code)
        if error_code is None:
            raise ValueError("an ASR failure requires a bounded error_code")
        started_at = _integer(started_at, "started_at", minimum=0)
        finished_at = _integer(finished_at, "finished_at", minimum=0)
        if finished_at < started_at:
            raise ValueError("finished_at must not precede started_at")

        with _module_storage_database._transaction(self.connection):
            self._require_video_part(video_part_id)
            run = self.connection.execute(
                "SELECT kind, outcome FROM acquisition_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise sqlite3.IntegrityError(f"unknown run_id: {run_id}")
            if run["kind"] != "asr":
                raise ValueError("ASR failures require an ASR acquisition run")
            if run["outcome"] != "running":
                return
            existing = self.connection.execute(
                "SELECT 1 FROM acquisition_attempts WHERE run_id = ? AND video_part_id = ?",
                (run_id, video_part_id),
            ).fetchone()
            if existing is None:
                self.connection.execute(
                    """
                    INSERT INTO acquisition_attempts(
                        run_id, video_part_id, outcome, error_code, transcript_id,
                        started_at, finished_at
                    ) VALUES (?, ?, 'failed', ?, NULL, ?, ?)
                    """,
                    (run_id, video_part_id, error_code, started_at, finished_at),
                )

    def read_transcript(
        self,
        video_part_id: int,
        source_kind: str,
        language: str,
        version: int | None = None,
    ) -> TranscriptRecord | None:
        """Read one stored transcript version with its segment timeline.

        ``version=None`` reads the latest version of the identity; an explicit
        ``version`` reads that one, which stays readable after a newer version
        is written.  ``None`` means the archive holds no such version.  The
        language is trimmed exactly as the write path trims it, so the identity
        a caller names here is the identity the store holds, and ``source_kind``
        is validated against the whole vocabulary the column's CHECK accepts —
        the ``asr-local`` reservation simply has no rows yet.  Read-only: no
        write, no commit.
        """
        video_part_id = _integer(video_part_id, "video_part_id", minimum=1)
        source_kind = _choice(source_kind, "source_kind", ALLOWED_SOURCE_KINDS)
        language = _language_code(language)
        if version is None:
            query = (
                "SELECT * FROM transcripts WHERE video_part_id = ? "
                "AND source_kind = ? AND language = ? ORDER BY version DESC LIMIT 1"
            )
            parameters: tuple[object, ...] = (video_part_id, source_kind, language)
        else:
            query = (
                "SELECT * FROM transcripts WHERE video_part_id = ? "
                "AND source_kind = ? AND language = ? AND version = ?"
            )
            parameters = (
                video_part_id,
                source_kind,
                language,
                _integer(version, "version", minimum=1),
            )
        row = self.connection.execute(query, parameters).fetchone()
        if row is None:
            return None
        transcript_id = int(row["transcript_id"])
        return TranscriptRecord(
            transcript_id=transcript_id,
            video_part_id=int(row["video_part_id"]),
            source_kind=str(row["source_kind"]),
            language=str(row["language"]),
            model_id=None if row["model_id"] is None else int(row["model_id"]),
            version=int(row["version"]),
            content_sha256=str(row["content_sha256"]),
            created_at=int(row["created_at"]),
            segments=self._stored_segments(transcript_id),
        )

    def list_transcript_versions(
        self, video_part_id: int, source_kind: str, language: str
    ) -> list[sqlite3.Row]:
        """List one transcript identity's stored versions, oldest first.

        Rows come straight from ``transcripts`` and carry every stored column;
        an identity the archive does not hold yields an empty list.  Read-only.
        """
        video_part_id = _integer(video_part_id, "video_part_id", minimum=1)
        source_kind = _choice(source_kind, "source_kind", ALLOWED_SOURCE_KINDS)
        language = _language_code(language)
        return list(
            self.connection.execute(
                """
                SELECT * FROM transcripts
                WHERE video_part_id = ? AND source_kind = ? AND language = ?
                ORDER BY version
                """,
                (video_part_id, source_kind, language),
            ).fetchall()
        )

    def list_pending_subtitle_parts(
        self, limit: int | None = None
    ) -> list[sqlite3.Row]:
        """Return the captionless parts in the locked work order.

        Rows come straight from the ``v_pending_subtitles`` view, which carries
        the newest attempt's evidence for every part that holds no transcript.
        The repository — not the view — imposes the order
        ``attempted ASC, last_attempt_at ASC, bvid ASC, page_index ASC``, so
        never-attempted parts come before previously attempted ones and the
        oldest attempt comes first: successive bounded runs rotate through the
        captionless backlog instead of re-attempting the same head.  The key
        list stays verbatim even though ``attempted`` is implied by
        ``last_attempt_at IS NULL`` (which SQLite sorts first): it is the locked
        contract the CLI reads, not a query to be shortened.  Read-only.
        """
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise TypeError("limit must be an integer or None")
            if limit < 1:
                raise ValueError("limit must be a positive integer")
            query = (
                "SELECT * FROM v_pending_subtitles "
                "ORDER BY attempted ASC, last_attempt_at ASC, bvid ASC, page_index ASC "
                "LIMIT ?"
            )
            return list(self.connection.execute(query, (limit,)).fetchall())
        return list(
            self.connection.execute(
                "SELECT * FROM v_pending_subtitles "
                "ORDER BY attempted ASC, last_attempt_at ASC, bvid ASC, page_index ASC"
            ).fetchall()
        )

    def read_video_pubdates(self, bvids: Sequence[str]) -> dict[str, int]:
        """Return the stored publication second of each named video.

        ``videos.pubdate`` is the store's own fact: a derived manifest row
        carries it rather than inventing a date.  An empty input answers ``{}``
        without executing anything — ``IN ()`` is a syntax error, not a read —
        and a bvid the archive does not hold simply has no entry, so every
        lookup the caller makes for a part it just read stays answered.  A
        repeated bvid is answered once.  The keys are read in chunks of at most
        ``_PUBDATE_CHUNK`` parameters, because the caller hands over a whole
        queue: the store bounds how many distinct bvids there are, the driver
        bounds one statement, and only the first bound is this module's to
        assume.  Read-only.
        """
        if not bvids:
            return {}
        keys = tuple(dict.fromkeys(_text(bvid, "bvid") for bvid in bvids))
        pubdates: dict[str, int] = {}
        for start in range(0, len(keys), _module_storage_database._PUBDATE_CHUNK):
            chunk = keys[start : start + _module_storage_database._PUBDATE_CHUNK]
            placeholders = ", ".join("?" * len(chunk))
            pubdates.update(
                {
                    str(row["bvid"]): int(row["pubdate"])
                    for row in self.connection.execute(
                        f"SELECT bvid, pubdate FROM videos "
                        f"WHERE bvid IN ({placeholders})",
                        chunk,
                    ).fetchall()
                }
            )
        return pubdates

    def count_pending_subtitle_parts(self) -> int:
        """Count the parts the pending relation holds. Read-only."""
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM v_pending_subtitles"
            ).fetchone()[0]
        )

    def list_selected_parts(
        self, bvid: str, page_index: int | None = None
    ) -> list[sqlite3.Row]:
        """Return the stored parts of one explicit selection, or no rows.

        Rows come straight from the ``v_video_parts`` view — the view carries the
        ``work_id``, the user and video context, but not the ``bvid`` itself, so
        the selector joins the part's own row to reach it — ordered by
        ``page_index``.  An unknown ``bvid`` — or an unknown ``bvid:pN`` —
        yields an empty list rather than an invented row, the honest answer the
        caller reports as a usage error.  An explicit selection is not filtered
        by ``processing_status``: explicit means explicit, and the evidence a
        run writes then records what upstream really returned.  Read-only.
        """
        bvid = _text(bvid, "bvid")
        if page_index is None:
            query = (
                "SELECT vvp.* FROM v_video_parts AS vvp "
                "JOIN video_parts AS vp ON vp.video_part_id = vvp.video_part_id "
                "WHERE vp.bvid = ? ORDER BY vvp.page_index"
            )
            parameters: tuple[object, ...] = (bvid,)
        else:
            query = (
                "SELECT vvp.* FROM v_video_parts AS vvp "
                "JOIN video_parts AS vp ON vp.video_part_id = vvp.video_part_id "
                "WHERE vp.bvid = ? AND vvp.page_index = ?"
            )
            parameters = (bvid, _integer(page_index, "page_index", minimum=0))
        return list(self.connection.execute(query, parameters).fetchall())

    def list_stored_transcripts(
        self, bvid: str | None = None, page_index: int | None = None,
        *, limit_parts: int | None = None,
        after_part: tuple[str, int] | None = None,
    ) -> list[sqlite3.Row]:
        """Return one row per stored transcript version with its part context.

        The relation is over ``transcripts``, not over parts: a part holding
        several stored versions appears once per version, and every row repeats
        its part's columns, its video's ``pubdate`` and its video's own
        ``title`` — the collection the part belongs to, carried as
        ``video_title`` beside the part's own ``part_title``.  The two are
        independent facts and the join is what keeps them apart without a second
        query.  Membership is the join to ``transcripts`` and nothing else: no
        ``processing_status`` predicate narrows it, so a part whose status is
        ``gone`` is a row here when the store holds its text.

        ``bvid`` and ``page_index`` each add one predicate when they are given
        and neither narrows the read when it is absent.  A selector naming no
        stored part — an unknown bvid, or a stored part holding no transcript —
        yields no row rather than an invented one.  The order ``bvid,
        page_index, source_kind, language, version DESC`` lives in this query,
        not in the caller, and it is deterministic row-for-row.  Read-only: no
        write, no commit. ``limit_parts`` selects the first stored parts in
        that order before reading their versions. Every version of a selected
        part remains available to the caller's winner selection. ``after_part``
        advances past a previously selected ``(bvid, page_index)`` without an
        offset or splitting one part's versions across batches.
        """
        where_clauses: list[str] = []
        parameters: list[object] = []
        if bvid is not None:
            where_clauses.append("vp.bvid = ?")
            parameters.append(_text(bvid, "bvid"))
        if page_index is not None:
            where_clauses.append("vp.page_index = ?")
            parameters.append(_integer(page_index, "page_index", minimum=0))
        if after_part is not None:
            if not isinstance(after_part, tuple) or len(after_part) != 2:
                raise TypeError("after_part must be a (bvid, page_index) tuple")
            after_bvid = _text(after_part[0], "after_part bvid")
            after_page_index = _integer(
                after_part[1], "after_part page_index", minimum=0
            )
            where_clauses.append("(vp.bvid, vp.page_index) > (?, ?)")
            parameters.extend((after_bvid, after_page_index))
        prefix = ""
        if limit_parts is not None:
            limit_parts = _integer(limit_parts, "limit_parts", minimum=1)
            selected_where = where_clauses + [
                "EXISTS (SELECT 1 FROM transcripts AS stored "
                "WHERE stored.video_part_id = vp.video_part_id)"
            ]
            prefix = (
                "WITH selected_parts AS MATERIALIZED ("
                "SELECT vp.video_part_id FROM video_parts AS vp WHERE "
                + " AND ".join(selected_where)
                + " ORDER BY vp.bvid ASC, vp.page_index ASC LIMIT ?) "
            )
            parameters.append(limit_parts)
        relation = (
            "FROM transcripts AS t "
            "JOIN video_parts AS vp ON vp.video_part_id = t.video_part_id "
        )
        if limit_parts is not None:
            # Keep the selected part ids as the outer loop. An ordinary JOIN
            # may scan the whole transcript relation before testing membership.
            relation = (
                "FROM selected_parts AS selected "
                "CROSS JOIN video_parts AS vp "
                "ON vp.video_part_id = selected.video_part_id "
                "CROSS JOIN transcripts AS t ON t.video_part_id = vp.video_part_id "
            )
        query = (
            "SELECT vp.video_part_id, vp.bvid, vp.page_index, vp.cid, "
            "vp.title AS part_title, vp.duration_ms, vd.pubdate, "
            "vd.title AS video_title, "
            "t.transcript_id, t.source_kind, t.language, t.model_id, "
            "t.version, t.content_sha256, t.created_at "
            + relation +
            "JOIN videos AS vd ON vd.bvid = vp.bvid"
        )
        if limit_parts is None and where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
        query += (
            " ORDER BY vp.bvid ASC, vp.page_index ASC, t.source_kind ASC, "
            "t.language ASC, t.version DESC"
        )
        return list(self.connection.execute(prefix + query, parameters).fetchall())

    def _stored_segments(
        self, transcript_id: int
    ) -> tuple[TranscriptSegmentRecord, ...]:
        """Return one version's segments in ordinal order, verbatim as stored."""
        return tuple(
            TranscriptSegmentRecord(
                start_ms=int(row["start_ms"]),
                end_ms=int(row["end_ms"]),
                text=str(row["text"]),
            )
            for row in self.connection.execute(
                """
                SELECT start_ms, end_ms, text FROM transcript_segments
                WHERE transcript_id = ? ORDER BY ordinal
                """,
                (transcript_id,),
            ).fetchall()
        )

    def _require_video_part(self, video_part_id: int) -> None:
        """Require that ``video_part_id`` names a part the archive already holds.

        Bounded, named evidence instead of the raw foreign-key message: both
        write paths reference the part, so an unknown one fails the whole call
        before anything is written.
        """
        row = self.connection.execute(
            "SELECT 1 FROM video_parts WHERE video_part_id = ?", (video_part_id,)
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError(f"unknown video_part_id: {video_part_id}")

    def _require_acquisition_run(self, run_id: str) -> None:
        """Require that ``run_id`` names an acquisition run.

        The attempt row that references the run is written last, so an unknown
        run leaves no transcript version or segment behind.
        """
        row = self.connection.execute(
            "SELECT 1 FROM acquisition_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError(f"unknown run_id: {run_id}")

    def _canonical_segments(
        self, segments: tuple[TranscriptSegmentRecord, ...]
    ) -> list[list[int | str]]:
        """Return the ordered ``[start_ms, end_ms, text]`` triples to store.

        The storage boundary re-validates the timeline instead of trusting the
        caller: a segment below zero, an ``end_ms`` that does not exceed its
        ``start_ms``, a millisecond value above :data:`MAX_TIMELINE_MS`, or
        text that is empty once trimmed raises ``ValueError``.  The text of
        each triple is its trimmed form — what the row stores and what the
        content hash covers — and the ordinal is the position, so upstream
        order is preserved verbatim, overlaps included.
        """
        canonical: list[list[int | str]] = []
        for index, segment in enumerate(segments):
            if not isinstance(segment, TranscriptSegmentRecord):
                raise TypeError(
                    f"segments[{index}] must be a TranscriptSegmentRecord"
                )
            start_ms = _integer(
                segment.start_ms,
                f"segments[{index}].start_ms",
                minimum=0,
                maximum=MAX_TIMELINE_MS,
            )
            end_ms = _integer(
                segment.end_ms,
                f"segments[{index}].end_ms",
                minimum=1,
                maximum=MAX_TIMELINE_MS,
            )
            if end_ms <= start_ms:
                raise ValueError(
                    f"segments[{index}].end_ms must be greater than start_ms"
                )
            text = segment.text.strip()
            if not text:
                raise ValueError(f"segments[{index}].text must not be empty")
            canonical.append([start_ms, end_ms, text])
        return canonical

    def _next_transcript_version(
        self, video_part_id: int, source_kind: str, language: str
    ) -> int:
        """Return the next version number of one transcript identity."""
        row = self.connection.execute(
            """
            SELECT COALESCE(MAX(version), 0) + 1 AS next_version
            FROM transcripts
            WHERE video_part_id = ? AND source_kind = ? AND language = ?
            """,
            (video_part_id, source_kind, language),
        ).fetchone()
        return int(row["next_version"])

    def _run_outcome_from_attempts(self, run_id: str) -> str:
        """Derive a run's outcome from the attempt rows it holds."""
        counts = {
            str(row["outcome"]): int(row["attempts"])
            for row in self.connection.execute(
                """
                SELECT outcome, COUNT(*) AS attempts
                FROM acquisition_attempts
                WHERE run_id = ?
                GROUP BY outcome
                """,
                (run_id,),
            ).fetchall()
        }
        attempts = sum(counts.values())
        failed = counts.get("failed", 0)
        if failed and failed == attempts:
            return "failed"
        if failed:
            return "partial"
        return "complete"
