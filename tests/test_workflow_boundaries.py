"""Atomic commands, pure planning and short fenced artifact commits."""

import json

import pytest

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.editorial import EditorialConfig
from bili_asr.editorial_runtime import EditorialWorkflowHandlers
from bili_asr.manuscript_files import atomic_write_artifact
from bili_asr.services.workflow_application import WorkflowApplication
from bili_asr.storage import open_database
from bili_asr.storage.editorial import EditorialRepository
from bili_asr.storage.job_commit import JobCommitGuard
from bili_asr.storage.workflow import WorkflowRepository
from bili_asr.workflow import WorkflowExecutor
from bili_asr.workflow_models import AsrPolicy, AsrProfile, JobKind, LeaseLostError
from bili_asr.workflow_payloads import validate_payload
from bili_asr.workflow_planning import PlanningPart, plan_producers
from tests.test_ai_editorial import FakeClient, insert_record, record
from tests.test_workflow_control_plane import _seed_part


@pytest.fixture
def database(tmp_path):
    connection = open_database(tmp_path)
    _seed_part(connection)
    try:
        yield connection
    finally:
        connection.close()


def test_plan_always_includes_audio_and_asr_for_every_part():
    specs = plan_producers([PlanningPart(1, 9), PlanningPart(2), PlanningPart(3)],
        policy=AsrPolicy.ALL, profile_id=7, profile_digest="frozen",
        editorial_config=EditorialConfig().to_dict())
    assert [s.kind for s in specs] == [
        JobKind.SUBTITLE, JobKind.AUDIO, JobKind.ASR, JobKind.PROOFREAD, JobKind.RENDER_DOCUMENT,
        JobKind.SUBTITLE, JobKind.AUDIO, JobKind.ASR, JobKind.PROOFREAD, JobKind.RENDER_DOCUMENT,
        JobKind.SUBTITLE, JobKind.AUDIO, JobKind.ASR, JobKind.PROOFREAD, JobKind.RENDER_DOCUMENT,
    ]
    asr = specs[2]
    assert asr.prerequisites == ("audio:1",)
    assert asr.payload["reference_transcript_id"] == 9
    assert asr.dedupe_key == "asr:1:frozen"
    proof_payload, proof_key = specs[3].materialize({"asr:1": "actual-asr-id"})
    assert proof_payload["asr_job_id"] == "actual-asr-id" and proof_key.startswith("proofread:1:")
    render_payload, render_key = specs[4].materialize({"proofread:1": "actual-proof-id"})
    assert render_payload["proofread_job_id"] == "actual-proof-id"
    assert render_key == "render:actual-proof-id:ai-draft-v1"


@pytest.mark.parametrize("invalid", ["unknown_part", "editorial"])
def test_profile_and_whole_plan_rollback_together(database, invalid):
    workflow = WorkflowRepository(database)
    with pytest.raises(ValueError):
        workflow.plan_with_profile(part_ids=[1, 999] if invalid == "unknown_part" else [1],
            policy=AsrPolicy.ALL,
            profile=AsrProfile("atomic", "offline-model", device="cpu"),
            editorial_config={"context_tokens": -1} if invalid == "editorial" else None)
    assert database.execute("SELECT COUNT(*) FROM workflow_asr_profiles").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM workflow_asr_profile_configs").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM workflow_jobs").fetchone()[0] == 0
    assert not database.in_transaction


def test_proofreading_input_and_two_jobs_rollback_as_one_command(database, tmp_path, monkeypatch):
    insert_record(database, record())
    with ArchiveSession(tmp_path, mode=ArchiveAccessMode.WRITE) as session:
        app = WorkflowApplication(session)
        enqueue = app.repository.enqueue_editorial

        def fail_after_enqueue(**kwargs):
            enqueue(**kwargs)
            raise RuntimeError("simulated command failure")

        monkeypatch.setattr(app.repository, "enqueue_editorial", fail_after_enqueue)
        with pytest.raises(RuntimeError, match="simulated"):
            app.proofread(part_id=1, base_transcript_id=None, reference_transcript_id=None,
                          no_reference=True, config=EditorialConfig())
        for table in ("editorial_inputs", "workflow_jobs", "workflow_job_dependencies"):
            assert session.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("changes", [
    {"schema_version": 2}, {"schema_version": True}, {"video_part_id": 2},
    {"video_part_id": True}, {"video_part_id": "1"},
])
def test_invalid_durable_payload_is_rejected_without_claim_or_attempt(database, changes):
    workflow = WorkflowRepository(database)
    workflow.plan_with_profile(part_ids=[1], policy=AsrPolicy.ALL,
                              profile=AsrProfile("payload", "offline-model", device="cpu"))
    row = database.execute("SELECT * FROM workflow_jobs WHERE kind = 'subtitle'").fetchone()
    payload = json.loads(row["payload_json"])
    payload.update(changes)
    with database:
        database.execute("UPDATE workflow_jobs SET payload_json = ? WHERE job_id = ?", (json.dumps(payload), row["job_id"]))
    with pytest.raises(ValueError):
        workflow.claim("worker", kinds=(JobKind.SUBTITLE,))
    state = database.execute("SELECT status,attempt_count FROM workflow_jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
    assert tuple(state) == ("queued", 0)
    assert database.execute("SELECT COUNT(*) FROM workflow_attempts").fetchone()[0] == 0


def test_legacy_payload_is_validated_without_rewriting_its_identity():
    legacy = {"video_part_id": 1, "profile_id": 2, "reference_transcript_id": None}
    decoded = validate_payload(JobKind.ASR, legacy, part_id=1, profile_id=2)
    assert decoded.schema_version == 1 and dict(decoded.values) == legacy
    assert "schema_version" not in legacy
    with pytest.raises(TypeError):
        decoded.values["profile_id"] = 3


def test_shared_guard_rechecks_expiration_before_commit(database):
    workflow = WorkflowRepository(database)
    workflow.plan_with_profile(part_ids=[1], policy=AsrPolicy.ALL,
                              profile=AsrProfile("guard", "offline-model", device="cpu"))
    job = workflow.claim("worker", kinds=(JobKind.SUBTITLE,))
    expiry = database.execute("SELECT lease_expires_at FROM workflow_jobs WHERE job_id = ?", (job.job_id,)).fetchone()[0]
    now = [expiry - 1]
    guard = JobCommitGuard(database, clock=lambda: now[0])
    with pytest.raises(LeaseLostError):
        with guard.owned_transaction(job):
            database.execute("UPDATE videos SET title = 'must roll back'")
            now[0] = expiry
    assert database.execute("SELECT title FROM videos").fetchone()[0] == "test"


def _ready_renderer(database, root):
    insert_record(database, record())
    workflow = WorkflowRepository(database)
    repository = EditorialRepository(database)
    prepared = repository.prepare(1, None, EditorialConfig())
    workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    handlers = EditorialWorkflowHandlers(repository, workflow, archive_root=root, client=FakeClient())
    assert WorkflowExecutor(workflow, worker_id="proofreader", handlers=handlers.handlers(),
                            kinds=(JobKind.PROOFREAD,)).run().succeeded == 1
    return workflow, repository, handlers, workflow.claim("renderer", kinds=(JobKind.RENDER_DOCUMENT,))


def test_document_staging_and_file_fsync_finish_before_sqlite_writer_lock(database, tmp_path, monkeypatch):
    from bili_asr import manuscript_files

    workflow, repository, handlers, job = _ready_renderer(database, tmp_path)
    fsync = manuscript_files.os.fsync
    states = []

    def observe(fd):
        states.append(database.in_transaction)
        return fsync(fd)

    monkeypatch.setattr(manuscript_files.os, "fsync", observe)
    handlers.render(job)
    assert states[:2] == [False, False]
    assert database.execute("SELECT COUNT(*) FROM document_artifacts").fetchone()[0] == 2
    assert not list((tmp_path / "documents").rglob(".manuscript-*"))


def test_failed_document_registration_leaves_retryable_unregistered_bytes(database, tmp_path, monkeypatch):
    workflow, repository, handlers, job = _ready_renderer(database, tmp_path)
    register = repository.record_artifacts

    def fail_registration(*args):
        register(*args)
        raise RuntimeError("registration failed")

    monkeypatch.setattr(repository, "record_artifacts", fail_registration)
    with pytest.raises(RuntimeError, match="registration failed"):
        handlers.render(job)
    files = {p: p.read_bytes() for p in (tmp_path / "documents").rglob("*.md")}
    assert len(files) == 2
    assert database.execute("SELECT COUNT(*) FROM document_artifacts").fetchone()[0] == 0
    assert not list((tmp_path / "documents").rglob(".manuscript-*"))
    monkeypatch.setattr(repository, "record_artifacts", register)
    handlers.render(job)
    assert files == {p: p.read_bytes() for p in files}
    assert database.execute("SELECT COUNT(*) FROM document_artifacts").fetchone()[0] == 2


def test_existing_identical_artifact_retry_requires_no_new_file_or_fsync(tmp_path, monkeypatch):
    from bili_asr import manuscript_files

    target = atomic_write_artifact(tmp_path, "documents/fixed.md", b"immutable")

    def forbid(*args, **kwargs):
        pytest.fail("retry must not prepare or fsync another file")

    monkeypatch.setattr(manuscript_files.tempfile, "mkstemp", forbid)
    monkeypatch.setattr(manuscript_files.os, "fsync", forbid)
    assert atomic_write_artifact(tmp_path, "documents/fixed.md", b"immutable") == target
    with pytest.raises(ValueError, match="different bytes"):
        atomic_write_artifact(tmp_path, "documents/fixed.md", b"conflict")


def test_explicit_publish_batch_rolls_back_every_request_on_late_failure(database, tmp_path, monkeypatch):
    insert_record(database, record(source="subtitle-cc"))
    with ArchiveSession(tmp_path, mode=ArchiveAccessMode.WRITE) as session:
        app = WorkflowApplication(session)
        enqueue = app.repository.enqueue_publication

        def fail(**kwargs):
            enqueue(**kwargs)
            raise RuntimeError("publication batch failed")

        monkeypatch.setattr(app.repository, "enqueue_publication", fail)
        with pytest.raises(RuntimeError, match="batch failed"):
            app.publish([1])
        assert session.connection.execute("SELECT COUNT(*) FROM workflow_jobs").fetchone()[0] == 0
