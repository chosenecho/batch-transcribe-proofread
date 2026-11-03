from __future__ import annotations

from contextlib import closing
import hashlib
import json
import sqlite3

import pytest

from bili_asr.archive_session import ArchiveSession, ArchiveAccessMode
from bili_asr.platform_identity import ContentRef
from bili_asr.services.archive_migration import initialize_archive, migrate_archive, check_migrated_archive, ArchiveMigrationError
from bili_asr.services.archive_snapshot import save_snapshot, check_snapshot, restore_snapshot
from bili_asr.source_identity import artifact_stem, youtube_ref
from bili_asr.storage.archive_contracts import bootstrap_contract, require_universal_contract, runtime_contract, UNIVERSAL_V2
from bili_asr.storage.database import connect_database, require_archive_schema
from bili_asr.storage.migration_source import legacy_source_contract, table_fingerprint
from bili_asr.storage.sources import SourceRepository, SourceVideoMetadata
from bili_asr.storage.workflow_selection import resolve_workflow_selection
from tests.fixtures.frozen_migration_archive import frozen_archive


def test_universal_identity_is_not_fake_bilibili_and_has_case_safe_artifact_keys(tmp_path):
    root = tmp_path / "target"
    initialize_archive(root)
    with ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session:
        repository = SourceRepository(session.connection)
        with session.connection:
            first = repository.upsert_video(SourceVideoMetadata(ContentRef("youtube", "abcdefghijk"), "English", 1000))
            second = repository.upsert_video(SourceVideoMetadata(ContentRef("youtube", "Abcdefghijk"), "Other", 1000))
        assert first != second
        part = repository.part(first)
        assert part["bvid"] is None and part["cid"] is None
        assert session.connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0
        assert part["platform"] == "youtube"
        assert artifact_stem(repository.part(first)["content_ref"]).casefold() != artifact_stem(repository.part(second)["content_ref"]).casefold()
        selection = resolve_workflow_selection(session.connection, part_ids=[first, second])
        assert len(selection.targets) == 2 and all(target.work_id.startswith("youtube:") for target in selection.targets)
        with pytest.raises(sqlite3.IntegrityError, match="source part identity"):
            session.connection.execute("UPDATE video_parts SET page_index=1 WHERE video_part_id=?", (first,))
        session.connection.rollback()
    before = (root / "archive.db").read_bytes()
    with ArchiveSession(root, mode=ArchiveAccessMode.READ) as session:
        assert runtime_contract(session.connection) == UNIVERSAL_V2
        assert len(SourceRepository(session.connection).part(first)) > 10
        with pytest.raises(sqlite3.OperationalError):
            session.connection.execute("CREATE TABLE side_effect(x)")
    assert (root / "archive.db").read_bytes() == before


def test_frozen_legacy_migration_preserves_every_original_typed_row_and_file(tmp_path):
    source, target = tmp_path / "old", tmp_path / "new"
    expected = frozen_archive(source)
    original = (source / "archive.db").read_bytes()
    dry = migrate_archive(source, target, dry_run=True)
    assert not dry["conversion_performed"] and not target.exists()
    report = migrate_archive(source, target, expected_fingerprint=dry["source"]["fingerprint"])
    assert report["conversion_performed"] and report["recovery_changes"] == []
    assert (source / "archive.db").read_bytes() == original
    with closing(connect_database(target / "archive.db", readonly=True)) as connection:
        require_universal_contract(connection)
        for before in expected["tables"]:
            columns = legacy_source_contract()["objects"][before["name"]]["columns"]
            fingerprint = table_fingerprint(connection, before["name"], columns)
            assert fingerprint.row_count == before["row_count"]
            assert fingerprint.sha256 == before["sha256"], before["name"]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert set(row[0] for row in connection.execute("SELECT version FROM editorial_input_versions")) == {1}
        assert set(row[0] for row in connection.execute("SELECT version FROM publication_content_versions")) == {1}
        from bili_asr.publication import verify_release
        for state in ("current", "withdrawn", "superseded"):
            release, edition, document = verify_release(connection, expected["ids"][f"release_{state}"], (target,))
            assert hashlib.sha256(document).hexdigest() == release["artifact_sha256"]
    for path, digest in expected["files"].items():
        assert hashlib.sha256(target.joinpath(*path.split("/")).read_bytes()).hexdigest() == digest
    again = migrate_archive(source, target)
    assert again["reused"] and not again["conversion_performed"]
    assert again["imported_baseline_matches"]
    assert (source / "archive.db").read_bytes() == original


def test_explicit_bootstrap_never_upgrades_an_existing_legacy_database(tmp_path):
    source = tmp_path / "old"
    frozen_archive(source)
    before = (source / "archive.db").read_bytes()
    with closing(connect_database(source / "archive.db")) as connection:
        with pytest.raises(ValueError, match="empty"):
            bootstrap_contract(connection)
        require_archive_schema(connection)
    assert (source / "archive.db").read_bytes() == before
    with pytest.raises(ArchiveMigrationError, match="disjoint"):
        migrate_archive(source, source / "target")


def test_universal_and_legacy_snapshot_contracts_both_remain_readable(tmp_path):
    for name in ("old", "new"):
        root = tmp_path / name
        if name == "old":
            frozen_archive(root)
        else:
            initialize_archive(root)
        snapshot = tmp_path / f"{name}.zip"
        saved = save_snapshot(root, snapshot)
        assert check_snapshot(snapshot)["database_contract"] == saved["database_contract"]
        restored = tmp_path / f"{name}-restored"
        restore_snapshot(snapshot, restored)
        assert check_snapshot(snapshot)["valid"]
        with ArchiveSession(restored, mode=ArchiveAccessMode.READ) as session:
            require_archive_schema(session.connection)


@pytest.mark.parametrize("url", ["http://youtube.com/watch?v=abcdefghijk", "https://evil.test/watch?v=abcdefghijk",
                                  "https://youtube.com/playlist?list=abcdefghijk", "https://user@youtube.com/watch?v=abcdefghijk"])
def test_youtube_ingress_refuses_non_single_video_or_untrusted_urls(url):
    with pytest.raises(ValueError):
        youtube_ref(url)


@pytest.fixture
def migrated_archive(tmp_path):
    source, target = tmp_path / "old", tmp_path / "converted"
    expected = frozen_archive(source)
    report = migrate_archive(source, target)
    return source, target, expected, report


def test_migration_checker_ignores_new_provider_rows_and_reports_legal_current_deltas(migrated_archive):
    from bili_asr.publication import withdraw_release
    source, target, expected, report = migrated_archive
    source_before = (source / "archive.db").read_bytes()
    with ArchiveSession(target, mode=ArchiveAccessMode.WRITE) as session:
        connection = session.connection
        with connection:
            SourceRepository(connection).upsert_video(SourceVideoMetadata(
                ContentRef("youtube", "dQw4w9WgXcQ"), "New English source", 2000))
            connection.execute("UPDATE videos SET title='Refreshed title',updated_at=updated_at+1 WHERE bvid='BVpreserve'")
            connection.execute("UPDATE workflow_jobs SET available_at=available_at+1 WHERE job_id=?", (expected["ids"]["blocked_job"],))
            connection.execute("DELETE FROM video_tags WHERE bvid='BVpreserve'")
        withdraw_release(connection, release_id=expected["ids"]["release_current"], actor="operator", note="Current release replaced after cutover")
    before = (target / "archive.db").read_bytes()
    checked = check_migrated_archive(target)
    assert checked["valid"] and not checked["imported_baseline_matches"]
    assert checked["current_fact_deltas"] == {
        "publication_heads": 1, "publication_releases": 1, "video_tags": 1,
        "videos": 1, "workflow_jobs": 1,
    }
    assert checked["mutable_artifact_deltas"] == []
    assert (target / "archive.db").read_bytes() == before
    assert (source / "archive.db").read_bytes() == source_before
    assert report["imported_rows"]["row_count"] == sum(row["row_count"] for row in expected["tables"])
    snapshot = target.parent / "converted.zip"
    save_snapshot(target, snapshot)
    restored = target.parent / "restored"
    restore_snapshot(snapshot, restored)
    assert check_migrated_archive(restored)["current_fact_deltas"] == checked["current_fact_deltas"]


@pytest.mark.parametrize("table,statement", [
    ("transcript_segments", "UPDATE transcript_segments SET text=text||' altered' WHERE rowid=(SELECT MIN(rowid) FROM transcript_segments)"),
    ("editorial_inputs", "UPDATE editorial_inputs SET prepared_json=replace(prepared_json, '{', '{ ') WHERE rowid=(SELECT MIN(rowid) FROM editorial_inputs)"),
    ("editorial_model_calls", "UPDATE editorial_model_calls SET response_json=replace(response_json, '{', '{ ') WHERE rowid=(SELECT MIN(rowid) FROM editorial_model_calls)"),
    ("acquisition_runs", "UPDATE acquisition_runs SET finished_at=finished_at+1 WHERE rowid=(SELECT MIN(rowid) FROM acquisition_runs)"),
    ("workflow_attempts", "UPDATE workflow_attempts SET result_json=COALESCE(result_json,'{}')||' ' WHERE rowid=(SELECT MIN(rowid) FROM workflow_attempts)"),
    ("workflow_jobs", "UPDATE workflow_jobs SET payload_json=payload_json||' ' WHERE kind='audio'"),
    ("transcript_segments", "UPDATE transcript_segments SET rowid=rowid+10000 WHERE rowid=(SELECT MIN(rowid) FROM transcript_segments)"),
    ("transcript_segments", "DELETE FROM transcript_segments WHERE rowid=(SELECT MIN(rowid) FROM transcript_segments)"),
])
def test_migration_checker_rejects_typed_frozen_row_changes(migrated_archive, table, statement):
    _source, target, _expected, _report = migrated_archive
    with closing(connect_database(target / "archive.db")) as connection:
        connection.execute(statement)
        connection.commit()
    before = (target / "archive.db").read_bytes()
    with pytest.raises(ArchiveMigrationError, match=f"imported (frozen authority changed|authority row missing): {table}"):
        check_migrated_archive(target)
    assert (target / "archive.db").read_bytes() == before


@pytest.fixture
def migrated_with_publish_request(tmp_path):
    source, target = tmp_path / "old", tmp_path / "converted"
    expected = frozen_archive(source)
    # Add one legacy control-plane request with SQL governed by the frozen
    # source contract. The golden preservation fixture is never regenerated.
    with closing(sqlite3.connect(source / "archive.db")) as connection:
        connection.execute("INSERT INTO workflow_jobs(job_id,kind,video_part_id,dedupe_key,payload_json,status,available_at,created_at,updated_at) VALUES ('old-publish','publish',1,'publish:1',?,'succeeded',100,100,100)",
                           (json.dumps({"schema_version": 1, "video_part_id": 1, "transcript_id": expected["ids"]["asr_v1"]}, separators=(",", ":"), sort_keys=True),))
        connection.commit()
    report = migrate_archive(source, target)
    return source, target, expected, report


def test_migration_checker_allows_only_known_same_part_publish_request_rebinding(migrated_with_publish_request):
    from bili_asr.storage.workflow import WorkflowRepository
    _source, target, expected, _report = migrated_with_publish_request
    with ArchiveSession(target, mode=ArchiveAccessMode.WRITE) as session:
        connection = session.connection
        original = connection.execute("SELECT payload_json FROM workflow_jobs WHERE dedupe_key='publish:1'").fetchone()[0]
        original_id = json.loads(original)["transcript_id"]
        other_id = connection.execute("SELECT transcript_id FROM transcripts WHERE video_part_id=1 AND transcript_id<>? ORDER BY transcript_id LIMIT 1", (original_id,)).fetchone()[0]
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            WorkflowRepository(connection).enqueue_publication(video_part_id=1, transcript_id=other_id)
    checked = check_migrated_archive(target)
    assert checked["valid"] and checked["current_fact_deltas"] == {"workflow_jobs": 1}
    with closing(connect_database(target / "archive.db")) as connection:
        wrong_part = expected["ids"]["draft_base"]
        connection.execute("UPDATE workflow_jobs SET payload_json=? WHERE dedupe_key='publish:1'",
                           (json.dumps({"schema_version": 1, "video_part_id": 1, "transcript_id": wrong_part}, separators=(",", ":"), sort_keys=True),))
        connection.commit()
    with pytest.raises(ArchiveMigrationError, match="publication request identity changed"):
        check_migrated_archive(target)


@pytest.mark.parametrize("kind", ["same-id-reformat", "storage-type", "extra-field", "bool-id", "schema-change"])
def test_migration_checker_rejects_other_publish_payload_changes(migrated_with_publish_request, kind):
    _source, target, _expected, _report = migrated_with_publish_request
    with closing(connect_database(target / "archive.db")) as connection:
        raw = connection.execute("SELECT payload_json FROM workflow_jobs WHERE dedupe_key='publish:1'").fetchone()[0]
        payload = json.loads(raw)
        if kind == "same-id-reformat":
            altered = raw + " "
        elif kind == "storage-type":
            altered = raw.encode("utf-8")
        else:
            if kind == "extra-field":
                payload["private"] = "extra value"
            elif kind == "bool-id":
                payload["transcript_id"] = True
            elif kind == "schema-change":
                payload["schema_version"] = 2
            altered = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        connection.execute("UPDATE workflow_jobs SET payload_json=? WHERE dedupe_key='publish:1'", (altered,))
        connection.commit()
    with pytest.raises(ArchiveMigrationError, match="publication request identity changed"):
        check_migrated_archive(target)


@pytest.mark.parametrize("artifact", ["imported-rows.jsonl", "id-mapping.jsonl", "migration-report.json", "published-file"])
def test_migration_checker_rejects_audit_and_frozen_file_tampering(migrated_archive, artifact):
    _source, target, expected, report = migrated_archive
    if artifact == "published-file":
        relative = next(path for path in expected["files"] if path.startswith("publications/"))
        path = target.joinpath(*relative.split("/"))
    else:
        path = target / "documents" / "migrations" / report["migration_id"] / artifact
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ArchiveMigrationError, match="(ledger|mapping|report|frozen source artifact).*changed"):
        check_migrated_archive(target)


def test_migration_ledger_is_required_by_snapshot_save(migrated_archive):
    from bili_asr.services.archive_snapshot import SnapshotError
    _source, target, _expected, report = migrated_archive
    path = target.joinpath(*report["imported_rows"]["path"].split("/"))
    path.unlink()
    with pytest.raises(SnapshotError, match="(missing|referenced|artifact)"):
        save_snapshot(target, target.parent / "incomplete.zip")
    assert not (target.parent / "incomplete.zip").exists()


def test_failed_migration_never_installs_partial_target_or_changes_source(tmp_path, monkeypatch):
    from bili_asr.services import archive_migration
    source, target = tmp_path / "source", tmp_path / "target"
    frozen_archive(source)
    before = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    def fail_ledger(*args, **kwargs):
        raise OSError("injected staging ledger write failure")
    monkeypatch.setattr(archive_migration, "_row_ledger", fail_ledger)
    with pytest.raises(OSError, match="injected staging"):
        migrate_archive(source, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".target.migration-stage-*"))
    assert {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("operation", ["initialize", "migrate"])
def test_installed_archive_is_reported_even_if_parent_directory_sync_fails(tmp_path, monkeypatch, operation):
    from bili_asr.services import archive_migration
    target = tmp_path / "target"
    original = archive_migration._sync_directory
    def fail_after_install(path):
        if path == tmp_path and target.exists():
            raise OSError("injected parent directory sync failure")
        original(path)
    monkeypatch.setattr(archive_migration, "_sync_directory", fail_after_install)
    if operation == "initialize":
        result = initialize_archive(target)
    else:
        source = tmp_path / "source"
        frozen_archive(source)
        result = migrate_archive(source, target)
        assert check_migrated_archive(target)["valid"]
    assert result["installed"] and result["warnings"] == ["target_parent_directory_sync_failed"]
    with ArchiveSession(target, mode=ArchiveAccessMode.READ) as session:
        assert runtime_contract(session.connection) == UNIVERSAL_V2
