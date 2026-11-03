"""Real planners and execution boundaries preserve costly historical results."""
from contextlib import closing
from dataclasses import replace
import json

import pytest

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.editorial import EditorialConfig
from bili_asr.editorial_runtime import EditorialWorkflowHandlers
from bili_asr.services.archive_upgrade import apply_upgrade, plan_upgrade
from bili_asr.services.workflow_application import WorkflowApplication
from bili_asr.storage.database import connect_database
from bili_asr.storage.editorial import EditorialRepository
from bili_asr.storage.workflow import WorkflowRepository
from bili_asr.workflow import WorkflowExecutor
from bili_asr.workflow_models import AsrPolicy, AsrProfile, JobKind
from tests.fixtures.frozen_migration_archive import frozen_archive


class NoPaidCalls:
    def complete(self, *args, **kwargs):
        pytest.fail("completed historical input must not call AI again")


def test_formal_upgrade_reuses_completed_ai_jobs_and_preserves_pending_work(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    expected = frozen_archive(source)
    plan = plan_upgrade(source, target, target_contracts=("universal-v2",), no_external_control_state=True)
    apply_upgrade(plan)
    with ArchiveSession(target, mode=ArchiveAccessMode.WRITE) as session:
        app = WorkflowApplication(session)
        before = [tuple(row) for row in session.connection.execute("SELECT * FROM workflow_jobs ORDER BY rowid")]
        frozen = [dict(row) for row in session.connection.execute("SELECT * FROM editorial_inputs ORDER BY rowid")]
        for row in frozen:
            config = EditorialConfig.from_snapshot(json.loads(row["prepared_json"])["snapshot"]["config"])
            for _ in range(2):
                planned = app.proofread(part_id=None, base_transcript_id=row["base_transcript_id"],
                    reference_transcript_id=row["reference_transcript_id"], no_reference=row["reference_transcript_id"] is None,
                    config=config)
                assert planned["input_id"] == row["input_id"]
        assert [tuple(row) for row in session.connection.execute("SELECT * FROM workflow_jobs ORDER BY rowid")] == before
        editorial = EditorialWorkflowHandlers(EditorialRepository(session.connection), app.repository,
                                               archive_root=target, client=NoPaidCalls())
        summary = WorkflowExecutor(app.repository, worker_id="upgrade-ai-check", handlers=editorial.handlers(),
            kinds=(JobKind.PROOFREAD, JobKind.RENDER_DOCUMENT)).run(limit=5)
        assert summary.idle and summary.succeeded == summary.failed == 0
        # Same producer profile retains jobs and cancellation dependencies.
        profile_bytes = [tuple(row) for row in session.connection.execute("SELECT * FROM workflow_asr_profile_configs")]
        producer = app.repository.plan(part_ids=(1, 3), policy=AsrPolicy.ALL, profile_id=1)
        assert producer.subtitle_jobs == producer.audio_jobs == producer.asr_jobs == 0
        assert [tuple(row) for row in session.connection.execute("SELECT * FROM workflow_asr_profile_configs")] == profile_bytes
        assert expected["ids"]["blocked_job"] in app.repository.blocked_by_cancelled()
        # The genuinely unfinished ASR job still advances through real claim/finish.
        called = []
        def unfinished(job):
            called.append(job.job_id)
            return {"transcript_id": expected["ids"]["asr_v2"]}
        def no_download(job):
            pytest.fail("completed source acquisition must not run again")
        summary = WorkflowExecutor(app.repository, worker_id="upgrade-pending-check",
            handlers={JobKind.ASR: unfinished, JobKind.AUDIO: no_download, JobKind.SUBTITLE: no_download},
            kinds=(JobKind.ASR, JobKind.AUDIO, JobKind.SUBTITLE)).run(limit=5)
        assert summary.succeeded == 1 and summary.failed == 0 and len(called) == 1
        assert app.repository.explain_job(expected["ids"]["cancelled_job"])["status"] == "cancelled"


def test_explicit_refresh_or_changed_configuration_creates_current_input(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    frozen_archive(source)
    apply_upgrade(plan_upgrade(source, target, target_contracts=("universal-v2",)))
    with ArchiveSession(target, mode=ArchiveAccessMode.WRITE) as session:
        row = session.connection.execute("SELECT * FROM editorial_inputs ORDER BY rowid LIMIT 1").fetchone()
        config = EditorialConfig.from_snapshot(json.loads(row["prepared_json"])["snapshot"]["config"])
        app = WorkflowApplication(session)
        options = dict(part_id=None, base_transcript_id=row["base_transcript_id"],
            reference_transcript_id=row["reference_transcript_id"], no_reference=row["reference_transcript_id"] is None)
        refreshed = app.proofread(**options, config=config, refresh_input=True)
        assert refreshed["input_id"] != row["input_id"]
        assert session.connection.execute("SELECT version FROM editorial_input_versions WHERE input_id=?",
                                          (refreshed["input_id"],)).fetchone()[0] == 2
        changed = app.proofread(**options, config=replace(config, max_chunk_chars=config.max_chunk_chars - 1))
        assert changed["input_id"] not in (row["input_id"], refreshed["input_id"])


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_replanning_does_not_revive_terminal_historical_editorial_job(tmp_path, status):
    source, target = tmp_path / "source", tmp_path / "target"
    frozen_archive(source)
    apply_upgrade(plan_upgrade(source, target, target_contracts=("universal-v2",)))
    with closing(connect_database(target / "archive.db")) as connection:
        with connection:
            connection.execute("UPDATE workflow_jobs SET status=? WHERE kind='proofread'", (status,))
    with ArchiveSession(target, mode=ArchiveAccessMode.WRITE) as session:
        row = session.connection.execute("SELECT * FROM editorial_inputs ORDER BY rowid LIMIT 1").fetchone()
        config = EditorialConfig.from_snapshot(json.loads(row["prepared_json"])["snapshot"]["config"])
        planned = WorkflowApplication(session).proofread(part_id=None, base_transcript_id=row["base_transcript_id"],
            reference_transcript_id=row["reference_transcript_id"], no_reference=row["reference_transcript_id"] is None, config=config)
        assert WorkflowRepository(session.connection).explain_job(planned["job_id"])["status"] == status


def test_upgrade_keeps_committed_chunks_and_only_explicit_retry_runs_missing_chunks(tmp_path):
    from tests.test_ai_editorial import FakeClient, record, insert_record
    from bili_asr.storage.transcripts import _segment_content_sha256

    source, target = tmp_path / "source", tmp_path / "target"
    frozen_archive(source)
    config = EditorialConfig(max_chunk_segments=1)
    with ArchiveSession(source, mode=ArchiveAccessMode.WRITE) as session:
        # Add a separate partial-work scenario to the materialized archive;
        # the checked-in historical package and all its existing rows stay fixed.
        additional = record(transcript_id=5)
        additional = replace(additional, version=3, content_sha256=_segment_content_sha256(
            [[s.start_ms, s.end_ms, s.text] for s in additional.segments]))
        insert_record(session.connection, additional)
        repository = EditorialRepository(session.connection)
        workflow = WorkflowRepository(session.connection)
        prepared = repository.prepare(5, None, config)
        assert len(prepared["chunks"]) > 1
        proof_id, _, _, _ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
        handlers = EditorialWorkflowHandlers(repository, workflow, archive_root=source, client=FakeClient(fail_call=2))
        first = WorkflowExecutor(workflow, worker_id="before-upgrade", handlers=handlers.handlers(),
                                 kinds=(JobKind.PROOFREAD, JobKind.RENDER_DOCUMENT)).run()
        assert first.failed == 1
        saved = [tuple(row) for row in session.connection.execute(
            "SELECT * FROM editorial_chunk_results WHERE input_id=?", (prepared["input_id"],))]
        assert len(saved) == 1
        old_calls = [tuple(row) for row in session.connection.execute("SELECT * FROM editorial_model_calls ORDER BY rowid")]
    apply_upgrade(plan_upgrade(source, target, target_contracts=("universal-v2",)))
    with ArchiveSession(target, mode=ArchiveAccessMode.WRITE) as session:
        app = WorkflowApplication(session)
        assert app.proofread(part_id=None, base_transcript_id=5, reference_transcript_id=None,
                            no_reference=True, config=config)["job_id"] == proof_id
        assert app.repository.explain_job(proof_id)["status"] == "failed"
        repository = EditorialRepository(session.connection)
        client = FakeClient()
        handlers = EditorialWorkflowHandlers(repository, app.repository, archive_root=target, client=client)
        executor = WorkflowExecutor(app.repository, worker_id="after-upgrade", handlers=handlers.handlers(),
                                    kinds=(JobKind.PROOFREAD, JobKind.RENDER_DOCUMENT))
        assert executor.run().idle and not client.requests
        app.repository.requeue_failed(part_ids=[1])
        resumed = executor.run()
        assert resumed.succeeded == 2 and resumed.failed == 0
        assert len(client.requests) == len(prepared["chunks"]) - 1
        assert [tuple(row) for row in session.connection.execute(
            "SELECT * FROM editorial_model_calls ORDER BY rowid LIMIT ?", (len(old_calls),))] == old_calls
        assert [tuple(row) for row in session.connection.execute(
            "SELECT * FROM editorial_chunk_results WHERE input_id=? ORDER BY rowid LIMIT 1",
            (prepared["input_id"],))] == saved


def _automatic_editorial(session, config, base_id):
    workflow = WorkflowRepository(session.connection)
    profile = workflow.register_profile(AsrProfile("render-binding-regression", "offline"))
    first = workflow.plan(part_ids=(1,), policy=AsrPolicy.ALL, profile_id=profile,
                          editorial_config=config.to_dict())
    assert first.proofread_jobs == first.document_jobs == 1
    assert workflow.plan(part_ids=(1,), policy=AsrPolicy.ALL, profile_id=profile,
                         editorial_config=config.to_dict()).document_jobs == 0
    audio = workflow.claim("producer", kinds=(JobKind.AUDIO,))
    if audio is not None:
        workflow.finish(audio.job_id, worker_id="producer", result={})
    while True:
        asr = workflow.claim("producer", kinds=(JobKind.ASR,))
        assert asr is not None
        run_id = f"editorial-fixture-{base_id}"
        if session.connection.execute("SELECT 1 FROM acquisition_runs WHERE run_id=?", (run_id,)).fetchone() is None:
            from tests.support.asr_quality import measured_fixture_evidence
            with session.connection:
                measured_fixture_evidence(session.connection, EditorialRepository(session.connection).read_source(base_id))
        workflow.finish(asr.job_id, worker_id="producer", result={"transcript_id": base_id, "run_id": run_id})
        if asr.profile_id == profile:
            break
    proof = session.connection.execute("SELECT job_id FROM workflow_jobs WHERE kind='proofread' "
                                      "AND json_extract(payload_json,'$.asr_job_id')=?", (asr.job_id,)).fetchone()[0]
    pending = session.connection.execute("SELECT * FROM workflow_jobs WHERE dedupe_key=?",
                                        (f"render:{proof}:auto",)).fetchone()
    assert json.loads(pending["payload_json"])["template_version"] == "auto"
    return workflow, proof, pending["job_id"], profile


@pytest.mark.parametrize("native_v2", [False, True])
def test_automatic_chain_resolves_real_frozen_input_and_explicit_render_default(tmp_path, native_v2):
    from tests.test_ai_editorial import FakeClient

    source, target = tmp_path / "source", tmp_path / "target"
    frozen_archive(source)
    apply_upgrade(plan_upgrade(source, target, target_contracts=("universal-v2",)))
    with ArchiveSession(target, mode=ArchiveAccessMode.WRITE) as session:
        historical = [tuple(row) for row in session.connection.execute("SELECT * FROM editorial_inputs ORDER BY rowid")]
        old_calls = [tuple(row) for row in session.connection.execute("SELECT * FROM editorial_model_calls ORDER BY rowid")]
        row = session.connection.execute("SELECT * FROM editorial_inputs WHERE video_part_id=1 ORDER BY rowid LIMIT 1").fetchone()
        config = EditorialConfig.from_snapshot(json.loads(row["prepared_json"])["snapshot"]["config"])
        if native_v2:
            config = replace(config, max_chunk_chars=config.max_chunk_chars - 1)
        workflow, proof_id, render_id, profile = _automatic_editorial(session, config, row["base_transcript_id"])
        if not native_v2:
            # An existing plan from the affected release guessed v2 before its
            # automatic input was bound. Replanning keeps that pending identity;
            # binding later corrects it without executing the wrong renderer.
            with session.connection:
                payload = {"schema_version": 1, "proofread_job_id": proof_id, "template_version": "ai-draft-v2"}
                session.connection.execute("UPDATE workflow_jobs SET dedupe_key=?,payload_json=? WHERE job_id=?",
                    (f"render:{proof_id}:ai-draft-v2", json.dumps(payload), render_id))
            assert workflow.plan(part_ids=(1,), policy=AsrPolicy.ALL, profile_id=profile,
                                 editorial_config=config.to_dict()).document_jobs == 0
        client = FakeClient() if native_v2 else NoPaidCalls()
        repository = EditorialRepository(session.connection)
        handlers = EditorialWorkflowHandlers(repository, workflow, archive_root=target, client=client)
        summary = WorkflowExecutor(workflow, worker_id="bind-render", handlers=handlers.handlers(),
                                   kinds=(JobKind.PROOFREAD, JobKind.RENDER_DOCUMENT)).run()
        assert summary.succeeded == 2 and summary.failed == 0
        template = "ai-draft-v2" if native_v2 else "ai-draft-v1"
        render = workflow.explain_job(render_id)
        assert render["status"] == "succeeded"
        stored = session.connection.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (render_id,)).fetchone()
        assert json.loads(stored["payload_json"])["template_version"] == template
        assert stored["dedupe_key"] == f"render:{proof_id}:{template}"
        jobs_before = [tuple(row) for row in session.connection.execute("SELECT * FROM workflow_jobs ORDER BY rowid")]
        assert workflow.plan(part_ids=(1,), policy=AsrPolicy.ALL, profile_id=profile,
                             editorial_config=config.to_dict()).document_jobs == 0
        assert [tuple(row) for row in session.connection.execute("SELECT * FROM workflow_jobs ORDER BY rowid")] == jobs_before
        revision_id = repository.revision_for_job(proof_id)
        before_mismatch = [tuple(row) for row in session.connection.execute("SELECT * FROM workflow_jobs ORDER BY rowid")]
        with pytest.raises(ValueError, match="frozen input"):
            workflow.request_document(video_part_id=1, revision_id=revision_id,
                                      template_version="ai-draft-v1" if native_v2 else "ai-draft-v2")
        assert [tuple(row) for row in session.connection.execute("SELECT * FROM workflow_jobs ORDER BY rowid")] == before_mismatch
        explicit_id = WorkflowApplication(session).render(revision_id)
        explicit_payload = session.connection.execute("SELECT payload_json FROM workflow_jobs WHERE job_id=?", (explicit_id,)).fetchone()[0]
        assert json.loads(explicit_payload)["template_version"] == template
        assert WorkflowExecutor(workflow, worker_id="explicit-render", handlers=handlers.handlers(),
                                kinds=(JobKind.RENDER_DOCUMENT,)).run().succeeded == 1
        assert [tuple(row) for row in session.connection.execute(
            "SELECT * FROM editorial_inputs ORDER BY rowid LIMIT ?", (len(historical),))] == historical
        assert [tuple(row) for row in session.connection.execute(
            "SELECT * FROM editorial_model_calls ORDER BY rowid LIMIT ?", (len(old_calls),))] == old_calls


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_binding_uses_existing_canonical_job_without_reviving_terminal_history(tmp_path, status):
    source, target = tmp_path / "source", tmp_path / "target"
    frozen_archive(source)
    apply_upgrade(plan_upgrade(source, target, target_contracts=("universal-v2", "artifact-storage-v1", "artifact-online-v1")))
    with ArchiveSession(target, mode=ArchiveAccessMode.WRITE) as session:
        row = session.connection.execute("SELECT * FROM editorial_inputs WHERE video_part_id=1 ORDER BY rowid LIMIT 1").fetchone()
        config = EditorialConfig.from_snapshot(json.loads(row["prepared_json"])["snapshot"]["config"])
        workflow, proof_id, pending_id, _ = _automatic_editorial(session, config, row["base_transcript_id"])
        with workflow._write_transaction():
            canonical_id, _ = workflow._ensure_job(kind=JobKind.RENDER_DOCUMENT, video_part_id=1,
                profile_id=None, policy_key=None, payload={"proofread_job_id": proof_id, "template_version": "ai-draft-v1"},
                dedupe_key=f"render:{proof_id}:ai-draft-v1")
            session.connection.execute("UPDATE workflow_jobs SET status=?,attempt_count=1 WHERE job_id=?", (status, canonical_id))
            downstream_id, _ = workflow._ensure_job(kind=JobKind.INDEX, video_part_id=1,
                profile_id=None, policy_key=None, payload={}, dedupe_key="index:binding-consumer")
            session.connection.execute("INSERT INTO workflow_job_dependencies VALUES (?,?)", (downstream_id, pending_id))
        canonical_before = tuple(session.connection.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (canonical_id,)).fetchone())
        with session.connection:
            session.connection.execute("INSERT INTO artifact_input_states(job_id,object_id,readiness,reason,observed_at,retry_after) "
                                       "VALUES (?,NULL,'blocked','placeholder-observation',1,2)", (pending_id,))
            session.connection.execute("INSERT INTO artifact_input_states(job_id,object_id,readiness,reason,observed_at,retry_after) "
                                       "VALUES (?,NULL,'ready','canonical-observation',3,4)", (canonical_id,))
        canonical_readiness = tuple(session.connection.execute("SELECT * FROM artifact_input_states WHERE job_id=?", (canonical_id,)).fetchone())
        handlers = EditorialWorkflowHandlers(EditorialRepository(session.connection), workflow,
                                              archive_root=target, client=NoPaidCalls())
        summary = WorkflowExecutor(workflow, worker_id="existing-canonical", handlers=handlers.handlers(),
                                   kinds=(JobKind.PROOFREAD, JobKind.RENDER_DOCUMENT)).run()
        assert summary.succeeded == 1 and summary.failed == 0
        assert tuple(session.connection.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (canonical_id,)).fetchone()) == canonical_before
        assert session.connection.execute("SELECT 1 FROM workflow_jobs WHERE job_id=?", (pending_id,)).fetchone() is None
        assert session.connection.execute("SELECT 1 FROM workflow_job_dependencies WHERE job_id=? AND prerequisite_job_id=?",
                                          (canonical_id, proof_id)).fetchone() is not None
        assert session.connection.execute("SELECT 1 FROM workflow_job_dependencies WHERE job_id=? AND prerequisite_job_id=?",
                                          (downstream_id, canonical_id)).fetchone() is not None
        assert session.connection.execute("SELECT 1 FROM artifact_input_states WHERE job_id=?", (pending_id,)).fetchone() is None
        assert tuple(session.connection.execute("SELECT * FROM artifact_input_states WHERE job_id=?", (canonical_id,)).fetchone()) == canonical_readiness
        assert not session.connection.execute("PRAGMA foreign_key_check").fetchall()


def test_cancelling_unbound_render_is_preserved_after_freezing_and_replanning(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    frozen_archive(source)
    apply_upgrade(plan_upgrade(source, target, target_contracts=("universal-v2",)))
    with ArchiveSession(target, mode=ArchiveAccessMode.WRITE) as session:
        row = session.connection.execute("SELECT * FROM editorial_inputs WHERE video_part_id=1 ORDER BY rowid LIMIT 1").fetchone()
        config = EditorialConfig.from_snapshot(json.loads(row["prepared_json"])["snapshot"]["config"])
        workflow, _, render_id, profile = _automatic_editorial(session, config, row["base_transcript_id"])
        workflow.cancel(job_ids=(render_id,))
        historical = tuple(session.connection.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (render_id,)).fetchone())
        handlers = EditorialWorkflowHandlers(EditorialRepository(session.connection), workflow,
                                              archive_root=target, client=NoPaidCalls())
        assert WorkflowExecutor(workflow, worker_id="cancelled-placeholder", handlers=handlers.handlers(),
                                kinds=(JobKind.PROOFREAD, JobKind.RENDER_DOCUMENT)).run().succeeded == 1
        assert workflow.plan(part_ids=(1,), policy=AsrPolicy.ALL, profile_id=profile,
                             editorial_config=config.to_dict()).document_jobs == 0
        assert tuple(session.connection.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (render_id,)).fetchone()) == historical


def test_old_wrong_template_failed_attempt_is_preserved_and_explicit_render_repairs_it(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    frozen_archive(source)
    apply_upgrade(plan_upgrade(source, target, target_contracts=("universal-v2",)))
    with ArchiveSession(target, mode=ArchiveAccessMode.WRITE) as session:
        row = session.connection.execute("SELECT * FROM editorial_inputs WHERE video_part_id=1 ORDER BY rowid LIMIT 1").fetchone()
        config = EditorialConfig.from_snapshot(json.loads(row["prepared_json"])["snapshot"]["config"])
        workflow, proof_id, render_id, profile = _automatic_editorial(session, config, row["base_transcript_id"])
        # Simulate the previous release's already-executed wrong-v2 render job.
        with session.connection:
            payload = {"schema_version": 1, "proofread_job_id": proof_id, "template_version": "ai-draft-v2"}
            session.connection.execute("UPDATE workflow_jobs SET dedupe_key=?,payload_json=?,status='failed',attempt_count=1 "
                                       "WHERE job_id=?", (f"render:{proof_id}:ai-draft-v2", json.dumps(payload), render_id))
            session.connection.execute("INSERT INTO workflow_attempts(attempt_id,job_id,worker_id,started_at,finished_at,outcome,error_code) "
                                       "VALUES ('wrong-template-history',?,'old-worker',1,2,'failed','ValueError')", (render_id,))
        job_history = tuple(session.connection.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (render_id,)).fetchone())
        attempts = [tuple(item) for item in session.connection.execute("SELECT * FROM workflow_attempts WHERE job_id=?", (render_id,))]
        repository = EditorialRepository(session.connection)
        handlers = EditorialWorkflowHandlers(repository, workflow, archive_root=target, client=NoPaidCalls())
        assert WorkflowExecutor(workflow, worker_id="reuse-proof", handlers=handlers.handlers(),
                                kinds=(JobKind.PROOFREAD,)).run().succeeded == 1
        assert workflow.plan(part_ids=(1,), policy=AsrPolicy.ALL, profile_id=profile,
                             editorial_config=config.to_dict()).document_jobs == 0
        workflow.requeue_failed(part_ids=(1,), kinds=(JobKind.RENDER_DOCUMENT,))
        retried_history = tuple(session.connection.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (render_id,)).fetchone())
        assert workflow.plan(part_ids=(1,), policy=AsrPolicy.ALL, profile_id=profile,
                             editorial_config=config.to_dict()).document_jobs == 0
        assert tuple(session.connection.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (render_id,)).fetchone()) == retried_history
        # Leave the retried historical attempt blocked to isolate the explicit
        # repair below; its identity and original evidence remain unchanged.
        with session.connection:
            session.connection.execute("UPDATE workflow_jobs SET status='failed' WHERE job_id=?", (render_id,))
        job_history = tuple(session.connection.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (render_id,)).fetchone())
        repaired = WorkflowApplication(session).render(repository.revision_for_job(proof_id))
        assert repaired != render_id
        assert WorkflowExecutor(workflow, worker_id="render-repair", handlers=handlers.handlers(),
                                kinds=(JobKind.RENDER_DOCUMENT,)).run().succeeded == 1
        assert tuple(session.connection.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (render_id,)).fetchone()) == job_history
        assert [tuple(item) for item in session.connection.execute("SELECT * FROM workflow_attempts WHERE job_id=?", (render_id,))] == attempts
