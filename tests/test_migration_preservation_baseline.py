"""Validate representative legacy data with current readers before conversion."""

from __future__ import annotations

import json

import pytest

from bili_asr import archive
from bili_asr.publication import get_ai_artifacts, get_edition, verify_release
from bili_asr.publication_export import export_publications, export_publication_drafts
from bili_asr.services.archive_snapshot import check_snapshot, restore_snapshot, save_snapshot
from bili_asr.storage import TranscriptRepository, open_database
from bili_asr.storage.workflow import WorkflowRepository
from tests.fixtures.migration_archive import build_migration_archive, capture_table_rows


pytestmark = pytest.mark.usefixtures('enable_content_publication_for_tests')


@pytest.mark.parametrize("separate_artifacts", [False, True])
def test_legacy_fixture_preserves_history_and_both_heads(tmp_path, separate_artifacts):
    fixture = build_migration_archive(tmp_path / "source", tmp_path / "media" if separate_artifacts else None)
    ids = fixture.ids
    connection = open_database(fixture.archive_root)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert capture_table_rows(connection) == fixture.table_rows
        for name, status in (("superseded", "superseded"), ("withdrawn", "withdrawn"), ("current", "published")):
            release, edition, document = verify_release(connection, ids[f"release_{name}"], fixture.artifact_roots)
            assert release["status"] == status
            assert document == fixture.files[release["relative_path"]]
            assert get_ai_artifacts(connection, edition["revision_id"], fixture.artifact_roots)
        current = get_edition(connection, ids["edition_current"])
        assert current["current_release_id"] == ids["release_current"]
        assert current["review_status"] == "changes-requested"
        assert current["edition_id"] != ids["edition_published"]
        assert current["content"]["tags"] == ["源标签"]
        unpublished = get_edition(connection, ids["edition_unpublished"])
        assert unpublished["current_release_id"] is None and unpublished["review_status"] == "pending-review"
        repository = TranscriptRepository(connection)
        assert [row["version"] for row in repository.list_transcript_versions(ids["published_part"], "asr-local", "zh-CN")] == [1, 2]
        pending = connection.execute("SELECT * FROM v_pending_subtitles WHERE video_part_id=?", (ids["cancelled_part"],)).fetchone()
        assert pending["last_attempt_error_code"] == "rate_limited"
        assert ids["blocked_job"] in WorkflowRepository(connection).blocked_by_cancelled()
        assert connection.execute("SELECT outcome FROM workflow_attempts WHERE job_id=?", (ids["cancelled_job"],)).fetchone()[0] == "cancelled"
        assert connection.execute("SELECT COUNT(*) FROM audio_objects").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM part_audio_objects").fetchone()[0] == 2
        assert {row[0] for row in connection.execute("SELECT state FROM video_tag_observations")} == {"success_nonempty", "unavailable"}
        bundle = json.loads(connection.execute("SELECT artifact_json FROM workflow_publications").fetchone()[0])
        assert archive.archive_bundle_complete(fixture.artifact_roots[0], bundle, require_readable=True)
        assert fixture.frozen_json and fixture.not_covered
        # Existing legacy releases remain intact but have no public permission.
        assert export_publications(connection, artifact_roots=fixture.artifact_roots, output=tmp_path / "public") == 0
        assert export_publication_drafts(connection, artifact_roots=fixture.artifact_roots, output=tmp_path / "drafts") == 0
        public = json.loads((tmp_path / "public/catalog.json").read_text(encoding="utf-8"))
        assert public["articles"] == []
        drafts = json.loads((tmp_path / "drafts/catalog.json").read_text(encoding="utf-8"))
        assert drafts["articles"] == []
        assert capture_table_rows(connection) == fixture.table_rows
    finally:
        connection.close()


@pytest.mark.parametrize("separate_artifacts", [False, True])
def test_snapshot_roundtrip_keeps_all_rows_frozen_strings_and_files(tmp_path, separate_artifacts):
    fixture = build_migration_archive(tmp_path / "source", tmp_path / "media" if separate_artifacts else None)
    snapshot = tmp_path / "baseline.zip"
    save_snapshot(fixture.archive_root, snapshot, artifact_root=fixture.artifact_roots[0])
    assert check_snapshot(snapshot)["valid"]
    restored = tmp_path / "restored"
    restore_snapshot(snapshot, restored)
    connection = open_database(restored)
    try:
        assert capture_table_rows(connection) == fixture.table_rows
        for key, value in fixture.frozen_json.items():
            table, rowid, column = key.split(":")
            assert connection.execute(f'SELECT "{column}" FROM "{table}" WHERE rowid=?', (int(rowid),)).fetchone()[0] == value
        for key, expected in fixture.files.items():
            assert (restored / key).read_bytes() == expected
        for status in ("superseded", "withdrawn", "current"):
            verify_release(connection, fixture.ids[f"release_{status}"], (restored,))
        assert fixture.ids["blocked_job"] in WorkflowRepository(connection).blocked_by_cancelled()
    finally:
        connection.close()
