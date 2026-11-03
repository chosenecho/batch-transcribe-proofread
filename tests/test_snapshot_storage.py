"""Database consistency and recovery contracts for portable archive snapshots."""

from __future__ import annotations

from contextlib import closing
import hashlib
from importlib import resources
import json
from pathlib import Path
import sqlite3
import time

import pytest

from bili_asr.storage import SchemaContractError, open_database
from bili_asr.storage import snapshots
from bili_asr.storage.snapshots import (
    SnapshotDatabaseError,
    create_database_snapshot,
    recover_interrupted_jobs,
    required_artifacts,
    validate_snapshot_database,
)
from tests.support.storage_schema import _insert_user_video_part, _write_pre_iteration_database


@pytest.fixture
def database_path(tmp_root):
    path = Path(tmp_root) / "archive.db"
    with closing(open_database(path)) as connection:
        _insert_user_video_part(connection)
        connection.commit()
    return path


def _job(connection, job_id, status="queued", kind="audio"):
    connection.execute(
        "INSERT INTO workflow_jobs(job_id, kind, dedupe_key, payload_json, status, "
        "available_at, created_at, updated_at) VALUES (?, ?, ?, '{}', ?, 1, 1, 1)",
        (job_id, kind, job_id, status),
    )


def _attempt(connection, attempt_id, job_id, outcome, result=None):
    connection.execute(
        "INSERT INTO workflow_attempts(attempt_id, job_id, worker_id, started_at, "
        "finished_at, outcome, result_json) VALUES (?, ?, 'old-device', 1, ?, ?, ?)",
        (attempt_id, job_id, None if outcome == "running" else 2, outcome,
         None if result is None else json.dumps(result)),
    )


def test_current_contract_accepts_fts_extras_without_writing_source(database_path):
    original = validate_snapshot_database(database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("CREATE VIRTUAL TABLE search_cache USING fts5(text)")
        connection.commit()
    before = database_path.read_bytes()
    assert validate_snapshot_database(database_path, original) == original
    assert database_path.read_bytes() == before
    assert original.startswith("sha256:") and len(original) == 71


@pytest.mark.parametrize("columns, compatible", [
    (("credential_verified", "absence_verified"), True),
    (("absence_verified", "credential_verified"), False),
])
def test_historical_credential_layout_agrees_with_runtime_contract(tmp_root, columns, compatible):
    path = Path(tmp_root) / "older-additive.db"
    package = resources.files("bili_asr.storage")
    script = package.joinpath("schema-transcripts.sql").read_text("utf-8")
    script = "\n".join(
        line for line in script.splitlines()
        if not line.lstrip().startswith(("credential_verified INTEGER", "absence_verified INTEGER"))
    )
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(package.joinpath("schema.sql").read_text("utf-8"))
        connection.executescript(script)
        connection.executescript(package.joinpath("schema-workflow.sql").read_text("utf-8"))
        connection.executescript(package.joinpath("schema-editorial.sql").read_text("utf-8"))
        for column in columns:
            connection.execute(
                f"ALTER TABLE acquisition_attempts ADD COLUMN {column} INTEGER NOT NULL "
                f"DEFAULT 0 CHECK ({column} IN (0, 1))"
            )
        connection.commit()
    before = path.read_bytes()
    if compatible:
        assert validate_snapshot_database(path).startswith("sha256:")
        with closing(open_database(path)):
            pass
    else:
        with pytest.raises(SnapshotDatabaseError, match="acquisition_attempts"):
            validate_snapshot_database(path)
        with pytest.raises(SchemaContractError, match="acquisition_attempts"):
            open_database(path)
    assert path.read_bytes() == before


def test_legacy_contract_is_rejected_without_schema_changes(tmp_root):
    path = Path(tmp_root) / "legacy.db"
    _write_pre_iteration_database(str(path))
    before = path.read_bytes()
    with pytest.raises(SnapshotDatabaseError, match="contract"):
        validate_snapshot_database(path)
    assert path.read_bytes() == before


def test_matching_columns_with_stale_job_check_is_rejected(database_path):
    with closing(sqlite3.connect(database_path)) as connection:
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'workflow_jobs'").fetchone()[0]
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? WHERE name = 'workflow_jobs'",
            (sql.replace(", 'proofread', 'render_document'", ""),),
        )
        connection.commit()
    with pytest.raises(SnapshotDatabaseError, match="workflow_jobs"):
        validate_snapshot_database(database_path)


def test_semantic_table_shape_cannot_bypass_runtime_strict_ddl_contract(database_path):
    with closing(sqlite3.connect(database_path)) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'acquisition_attempts'"
        ).fetchone()[0]
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? WHERE name = 'acquisition_attempts'",
            (sql.replace("CREATE TABLE acquisition_attempts", 'CREATE TABLE "acquisition_attempts"'),),
        )
        connection.commit()
    before = database_path.read_bytes()
    with pytest.raises(SnapshotDatabaseError, match="acquisition_attempts"):
        validate_snapshot_database(database_path)
    with pytest.raises(SchemaContractError, match="acquisition_attempts"):
        open_database(database_path)
    assert database_path.read_bytes() == before


def test_foreign_key_violations_are_rejected(database_path):
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("UPDATE videos SET mid = 123456789")
        connection.commit()
    with pytest.raises(SnapshotDatabaseError, match="foreign-key"):
        validate_snapshot_database(database_path)


def test_extra_trigger_cannot_mutate_completed_history_during_recovery(database_path):
    with closing(sqlite3.connect(database_path)) as connection:
        _job(connection, "running", "running", "subtitle")
        _job(connection, "completed", "succeeded", "subtitle")
        connection.execute(
            "CREATE TRIGGER unexpected_recovery AFTER UPDATE ON workflow_jobs "
            "BEGIN DELETE FROM workflow_jobs WHERE status = 'succeeded'; END"
        )
        connection.commit()
    before = database_path.read_bytes()
    with pytest.raises(SnapshotDatabaseError, match="unsupported trigger"):
        recover_interrupted_jobs(database_path, "snapshot-trigger")
    assert database_path.read_bytes() == before


def test_contract_mismatch_is_rejected(database_path):
    with pytest.raises(SnapshotDatabaseError, match="unsupported"):
        validate_snapshot_database(database_path, "sha256:" + "0" * 64)


@pytest.mark.parametrize("table", ["workflow_asr_profile_configs", "transcript_asr_evidence"])
def test_new_asr_contract_tables_are_required(database_path, table):
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(f"DROP TABLE {table}")
        connection.commit()
    with pytest.raises(SnapshotDatabaseError, match=table):
        validate_snapshot_database(database_path)


def test_snapshot_and_recovery_preserve_asr_configuration_and_evidence(database_path):
    tables = ("workflow_asr_profile_configs", "transcript_asr_evidence")
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            "INSERT INTO workflow_asr_profiles VALUES "
            "(1, 'cpu', 'model', '', 'aligner', 'cpu', 'zh', ?, 1)", ("a" * 64,)
        )
        connection.execute(
            "INSERT INTO workflow_asr_profile_configs VALUES (1, 2, ?)",
            (json.dumps({"schema_version": 2, "model_name": "model"}),),
        )
        connection.execute(
            "INSERT INTO transcripts(transcript_id, video_part_id, source_kind, language, version, "
            "content_sha256, created_at) VALUES (1, 1, 'asr-local', 'zh', 1, ?, 1)", ("c" * 64,)
        )
        connection.execute(
            "INSERT INTO acquisition_runs VALUES ('asr-complete', 'asr', 'pending', NULL, NULL, "
            "0, 1, 2, 'complete')"
        )
        connection.execute(
            "INSERT INTO acquisition_attempts(run_id, video_part_id, outcome, transcript_id, "
            "started_at, finished_at) VALUES ('asr-complete', 1, 'stored', 1, 1, 2)"
        )
        connection.execute(
            "INSERT INTO transcript_asr_evidence VALUES ('asr-complete', 1, 1, 1, ?)",
            (json.dumps({"schema_version": 1, "source": "preserved"}),),
        )
        connection.commit()
        original = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    restored = database_path.with_name("restored-asr.db")
    create_database_snapshot(database_path, restored)
    recover_interrupted_jobs(restored, "snapshot-asr")
    with closing(sqlite3.connect(restored)) as connection:
        assert {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables} == original


def test_backup_reads_committed_wal_content_without_changing_source(database_path):
    target = database_path.with_name("snapshot.db")
    with closing(sqlite3.connect(database_path)) as writer:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("UPDATE videos SET title = 'new committed title'")
        writer.commit()
        assert database_path.with_name("archive.db-wal").is_file()
        before = database_path.read_bytes()
        assert create_database_snapshot(database_path, target) == validate_snapshot_database(target)
        assert database_path.read_bytes() == before
        with closing(sqlite3.connect(target)) as copy:
            assert copy.execute("SELECT title FROM videos").fetchone()[0] == "new committed title"


def test_backup_refuses_existing_target_and_removes_failed_new_target(database_path):
    target = database_path.with_name("snapshot.db")
    target.write_bytes(b"keep this target")
    with pytest.raises(SnapshotDatabaseError, match="snapshot"):
        create_database_snapshot(database_path, target)
    assert target.read_bytes() == b"keep this target"
    corrupt = database_path.with_name("corrupt.db")
    corrupt.write_bytes(b"not a database")
    missing = database_path.with_name("failed-snapshot.db")
    with pytest.raises(SnapshotDatabaseError):
        create_database_snapshot(corrupt, missing)
    assert not missing.exists()


def test_backup_exclusive_source_lock_has_bounded_wait(database_path, monkeypatch):
    monkeypatch.setattr(snapshots, "_BACKUP_TIMEOUT_SECONDS", 0.15)
    target = database_path.with_name("blocked.db")
    with closing(sqlite3.connect(database_path)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(SnapshotDatabaseError, match="lock"):
            create_database_snapshot(database_path, target)
        assert time.monotonic() - started < 2
        assert not target.exists()


def test_required_artifacts_cover_dedup_audio_publications_and_documents(database_path):
    audio_hash, document_hash = "a" * 64, "b" * 64
    bundle = {key: f"transcripts/test/bundle.{extension}" for key, extension in (
        ("srt_path", "srt"), ("vtt_path", "vtt"), ("txt_path", "txt"),
        ("md_path", "md"), ("raw_path", "raw.json")
    )}
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            "INSERT INTO audio_objects(sha256, byte_size, format, duration_ms, storage_key, created_at) "
            "VALUES (?, 1, 'm4a', 1000, 'audio/original.m4a', 1)", (audio_hash,)
        )
        _job(connection, "dedup", "succeeded")
        _attempt(connection, "dedup-attempt", "dedup", "succeeded", {
            "storage_key": "audio/different-part.m4a", "sha256": audio_hash,
        })
        connection.execute(
            "INSERT INTO transcripts(transcript_id, video_part_id, source_kind, language, version, "
            "content_sha256, created_at) VALUES (1, 1, 'subtitle-ai', 'zh', 1, ?, 1)", ("c" * 64,)
        )
        connection.execute(
            "INSERT INTO workflow_publications(video_part_id, transcript_id, published_at, artifact_json) "
            "VALUES (1, 1, 1, ?)", (json.dumps(bundle),)
        )
        connection.execute(
            "INSERT INTO editorial_inputs(input_id, video_part_id, base_transcript_id, prepared_json, "
            "created_at) VALUES ('input', 1, 1, '{}', 1)"
        )
        _job(connection, "proof", "succeeded", "proofread")
        connection.execute(
            "INSERT INTO editorial_revisions VALUES ('revision', 'input', 'proof', '[]', 'ai-unreviewed', 1)"
        )
        connection.execute(
            "INSERT INTO document_artifacts VALUES ('revision', 'ai-draft-v1', 'ai-draft.md', "
            "'ai-draft', 'documents/revision/ai-draft.md', ?)", (document_hash,)
        )
        connection.commit()
    required = required_artifacts(database_path)
    assert required == {
        "audio/original.m4a": audio_hash,
        "audio/different-part.m4a": audio_hash,
        "documents/revision/ai-draft.md": document_hash,
        "transcripts/test/.bundle-ready": None,
        **{path: None for path in bundle.values()},
    }


@pytest.mark.parametrize("key", [
    "../audio/file.m4a", "audio/../file.m4a", "/audio/file.m4a", "C:/audio/file.m4a",
    "audio\\file.m4a", "audio//file.m4a", "audio/./file.m4a", "audio/CON.m4a",
    "audio/trailing.m4a ", "audio/trailing.", "audio/line\nfeed.m4a",
])
def test_nonportable_database_artifact_paths_are_rejected(database_path, key):
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            "INSERT INTO audio_objects(sha256, byte_size, format, duration_ms, storage_key, created_at) "
            "VALUES (?, 1, 'm4a', 1000, ?, 1)", ("a" * 64, key)
        )
        connection.commit()
    with pytest.raises(SnapshotDatabaseError, match="portable"):
        required_artifacts(database_path)


def test_conflicting_artifact_hashes_are_rejected(database_path):
    with closing(sqlite3.connect(database_path)) as connection:
        _job(connection, "audio", "succeeded")
        _attempt(connection, "audio-one", "audio", "succeeded", {
            "storage_key": "audio/one.m4a", "sha256": "a" * 64,
        })
        _attempt(connection, "audio-two", "audio", "succeeded", {
            "storage_key": "audio/one.m4a", "sha256": "b" * 64,
        })
        connection.commit()
    with pytest.raises(SnapshotDatabaseError, match="conflicting"):
        required_artifacts(database_path)


def test_incomplete_publication_reference_is_rejected(database_path):
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            "INSERT INTO transcripts(transcript_id, video_part_id, source_kind, language, version, "
            "content_sha256, created_at) VALUES (1, 1, 'subtitle-ai', 'zh', 1, ?, 1)", ("c" * 64,)
        )
        connection.execute(
            "INSERT INTO workflow_publications(video_part_id, transcript_id, published_at, artifact_json) "
            "VALUES (1, 1, 1, ?)", (json.dumps({"srt_path": "transcripts/test/bundle.srt"}),)
        )
        connection.commit()
    with pytest.raises(SnapshotDatabaseError, match="portable artifact path"):
        required_artifacts(database_path)


@pytest.mark.parametrize("replacement", [
    "transcripts/different/bundle.md", "transcripts/test/wrong.md", "documents/test/bundle.md",
])
def test_publication_paths_must_share_one_owned_bundle(database_path, replacement):
    payload = {
        "srt_path": "transcripts/test/bundle.srt", "txt_path": "transcripts/test/bundle.txt",
        "vtt_path": "transcripts/test/bundle.vtt",
        "md_path": replacement, "raw_path": "transcripts/test/bundle.raw.json",
    }
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            "INSERT INTO transcripts(transcript_id, video_part_id, source_kind, language, version, "
            "content_sha256, created_at) VALUES (1, 1, 'subtitle-ai', 'zh', 1, ?, 1)", ("c" * 64,)
        )
        connection.execute(
            "INSERT INTO workflow_publications(video_part_id, transcript_id, published_at, artifact_json) "
            "VALUES (1, 1, 1, ?)", (json.dumps(payload),)
        )
        connection.commit()
    with pytest.raises(SnapshotDatabaseError, match="complete bundle"):
        required_artifacts(database_path)


def test_unfinished_editorial_calls_are_closed_preserving_completed_results(database_path):
    with closing(sqlite3.connect(database_path)) as connection:
        _job(connection, "proofread", "running", "proofread")
        _attempt(connection, "proofread-attempt", "proofread", "running")
        connection.execute(
            "INSERT INTO transcripts(transcript_id, video_part_id, source_kind, language, version, "
            "content_sha256, created_at) VALUES (1, 1, 'subtitle-ai', 'zh', 1, ?, 1)", ("c" * 64,)
        )
        connection.execute(
            "INSERT INTO editorial_inputs(input_id, video_part_id, base_transcript_id, prepared_json, "
            "created_at) VALUES ('input', 1, 1, '{}', 1)"
        )
        connection.execute(
            "INSERT INTO editorial_model_calls(call_id, attempt_id, input_id, chunk_id, "
            "request_json, started_at) VALUES ('unfinished', 'proofread-attempt', 'input', 'c1', '{}', 1)"
        )
        connection.execute(
            "INSERT INTO editorial_model_calls(call_id, attempt_id, input_id, chunk_id, "
            "request_json, response_json, started_at, finished_at) "
            "VALUES ('complete', 'proofread-attempt', 'input', 'c2', '{}', '{\"kept\":true}', 1, 2)"
        )
        connection.commit()
        completed = connection.execute(
            "SELECT * FROM editorial_model_calls WHERE call_id = 'complete'"
        ).fetchone()
    counts = recover_interrupted_jobs(database_path, "snapshot-editorial")
    assert counts["editorial_model_calls"] == 1
    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute(
            "SELECT * FROM editorial_model_calls WHERE call_id = 'complete'"
        ).fetchone() == completed
        finished, error = connection.execute(
            "SELECT finished_at, error_code FROM editorial_model_calls WHERE call_id = 'unfinished'"
        ).fetchone()
        assert finished >= 1 and error == "snapshot_restored"


def test_restored_recovery_preserves_terminal_history_dependencies_and_cursor(database_path):
    with closing(sqlite3.connect(database_path)) as connection:
        for status in ("queued", "running", "succeeded", "failed", "cancelled"):
            _job(connection, status, status, "subtitle")
        connection.execute(
            "UPDATE workflow_jobs SET lease_owner = 'old-device', lease_expires_at = 9999999999, "
            "available_at = 9999999999, attempt_count = 2 WHERE status = 'running'"
        )
        _attempt(connection, "running-attempt", "running", "running", {"existing": "evidence"})
        _attempt(connection, "succeeded-attempt", "succeeded", "succeeded", {"kept": True})
        _attempt(connection, "failed-attempt", "failed", "failed", {"error": "kept"})
        _attempt(connection, "cancelled-attempt", "cancelled", "cancelled", {"cancelled": "kept"})
        connection.execute("INSERT INTO workflow_job_dependencies VALUES ('queued', 'succeeded')")
        connection.execute(
            "INSERT INTO ingestion_runs VALUES ('ingestion', 23191782, 'bilibili-api-python', "
            "'17.4.2', 1, NULL, 1, NULL, 'running')"
        )
        connection.execute(
            "INSERT INTO ingestion_cursors VALUES (23191782, 9, 100, 'limited', NULL, 1)"
        )
        connection.execute(
            "INSERT INTO acquisition_runs VALUES ('acquisition', 'subtitle', 'pending', NULL, NULL, "
            "0, 1, NULL, 'running')"
        )
        connection.commit()
        original_jobs = connection.execute(
            "SELECT * FROM workflow_jobs WHERE status <> 'running' ORDER BY job_id"
        ).fetchall()
        original_attempts = connection.execute(
            "SELECT * FROM workflow_attempts WHERE outcome <> 'running' ORDER BY attempt_id"
        ).fetchall()
    before = hashlib.sha256(database_path.read_bytes()).hexdigest()
    restored = database_path.with_name("restored.db")
    create_database_snapshot(database_path, restored)
    counts = recover_interrupted_jobs(restored, "snapshot-123")
    assert counts == {
        "workflow_jobs": 1, "workflow_attempts": 1, "ingestion_runs": 1,
        "acquisition_runs": 1, "editorial_model_calls": 0,
    }
    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == before
    with closing(sqlite3.connect(restored)) as connection:
        assert connection.execute(
            "SELECT * FROM workflow_jobs WHERE job_id <> 'running' ORDER BY job_id"
        ).fetchall() == original_jobs
        assert connection.execute(
            "SELECT * FROM workflow_attempts WHERE attempt_id <> 'running-attempt' ORDER BY attempt_id"
        ).fetchall() == original_attempts
        status, owner, lease, available, attempts = connection.execute(
            "SELECT status, lease_owner, lease_expires_at, available_at, attempt_count "
            "FROM workflow_jobs WHERE job_id = 'running'"
        ).fetchone()
        assert (status, owner, lease, attempts) == ("queued", None, None, 2)
        assert available <= int(time.time())
        outcome, error, evidence = connection.execute(
            "SELECT outcome, error_code, result_json FROM workflow_attempts "
            "WHERE attempt_id = 'running-attempt'"
        ).fetchone()
        assert outcome == "failed" and error == "snapshot_restored"
        assert json.loads(evidence) == {
            "existing": "evidence",
            "snapshot_recovery": {"snapshot_id": "snapshot-123", "reason": "interrupted_by_restore"},
        }
        assert connection.execute("SELECT next_page FROM ingestion_cursors").fetchone()[0] == 9
        assert connection.execute("SELECT * FROM workflow_job_dependencies").fetchall() == [("queued", "succeeded")]
        for table in ("ingestion_runs", "acquisition_runs"):
            assert connection.execute(f"SELECT outcome, finished_at FROM {table}").fetchone()[0] == "failed"
    assert set(recover_interrupted_jobs(restored, "snapshot-123").values()) == {0}
