"""Late scheduling failure cannot leave partially frozen editorial facts."""
from types import SimpleNamespace

import pytest

from bili_asr.editorial import EditorialConfig
from bili_asr.services.editorial_application import freeze_job_input
from bili_asr.storage.editorial import EditorialRepository
from bili_asr.storage.workflow import JobKind
from bili_asr.storage.workflow_outputs import record_audio, record_bundle_publication
from tests.test_ai_editorial import database as database
from tests.test_upgrade_result_reuse import _automatic_editorial


def test_freeze_and_render_binding_roll_back_together_on_late_failure(database, monkeypatch):
    workflow, proof_id, _, _ = _automatic_editorial(SimpleNamespace(connection=database), EditorialConfig(), 1)
    job = workflow.claim("transaction-test", kinds=(JobKind.PROOFREAD,))
    assert job.job_id == proof_id
    repository = EditorialRepository(database, commit_guard=workflow.commit_guard)
    before_jobs = [tuple(row) for row in database.execute("SELECT * FROM workflow_jobs ORDER BY rowid")]
    before_attempts = [tuple(row) for row in database.execute("SELECT * FROM workflow_attempts ORDER BY rowid")]
    original_bind = workflow.bind_editorial_render

    def fail_after_binding(proof_id, template):
        original_bind(proof_id, template)
        raise RuntimeError("injected late render failure")

    monkeypatch.setattr(workflow, "bind_editorial_render", fail_after_binding)
    with pytest.raises(RuntimeError, match="late render failure"):
        freeze_job_input(repository, workflow, job)
    assert database.execute("SELECT COUNT(*) FROM editorial_inputs").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM editorial_job_inputs").fetchone()[0] == 0
    assert [tuple(row) for row in database.execute("SELECT * FROM workflow_jobs ORDER BY rowid")] == before_jobs
    assert [tuple(row) for row in database.execute("SELECT * FROM workflow_attempts ORDER BY rowid")] == before_attempts
    assert not database.in_transaction
    monkeypatch.setattr(workflow, "bind_editorial_render", original_bind)
    prepared = freeze_job_input(repository, workflow, job)
    frozen = repository.load_input(prepared["input_id"])
    selection = prepared["source_selection"]
    assert selection["transcript_id"] == frozen["snapshot"]["base"]["transcript_id"]
    prerequisite = database.execute("SELECT attempt_id,result_json FROM workflow_attempts "
        "WHERE job_id=? AND outcome='succeeded'", (job.payload["asr_job_id"],)).fetchone()
    assert selection["attempt_id"] == prerequisite["attempt_id"]
    assert selection["asr_job_id"] == job.payload["asr_job_id"]
    # Producer selection is per-invocation evidence, outside frozen identity.
    assert frozen == {key: value for key, value in prepared.items() if key != "source_selection"}
    assert database.execute("SELECT COUNT(*) FROM editorial_job_inputs").fetchone()[0] == 1


def test_output_registration_obeys_caller_rollback(database):
    with pytest.raises(RuntimeError, match="caller rollback"):
        database.execute("BEGIN IMMEDIATE")
        try:
            record_audio(database, part_id=1, sha256="a" * 64, byte_size=12, media_format="m4a",
                         duration_ms=1000, storage_key="audio/synthetic.m4a", target_id="local")
            record_bundle_publication(database, part_id=1, transcript_id=1, paths={"synthetic": "value"})
            raise RuntimeError("caller rollback")
        finally:
            database.rollback()
    for table in ("audio_objects", "part_audio_objects", "workflow_publications"):
        assert database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 1


@pytest.mark.parametrize("operation", [
    lambda connection: record_audio(connection, part_id=1, sha256="a" * 64, byte_size=12,
        media_format="m4a", duration_ms=1000, storage_key="audio/synthetic.m4a", target_id="local"),
    lambda connection: record_bundle_publication(connection, part_id=1, transcript_id=1, paths={}),
])
def test_output_registration_requires_explicit_transaction(database, operation):
    with pytest.raises(RuntimeError, match="write transaction"):
        operation(database)
    assert not database.in_transaction
