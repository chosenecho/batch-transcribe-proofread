"""New provider content crosses real workflow, approval, release and export."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json

import pytest

from bili_asr.archive_session import ArchiveSession, ArchiveAccessMode
from bili_asr.editorial import EditorialConfig
from bili_asr.platform_identity import ContentRef
from bili_asr.publication import create_edition, edit_edition, get_edition, publish_edition, verify_release
from bili_asr.publication_export import export_publications, export_publication_drafts, export_editorial
from bili_asr.services.archive_migration import initialize_archive
from bili_asr.services.archive_snapshot import save_snapshot, check_snapshot, restore_snapshot
from bili_asr.storage.archive_contracts import frozen_version
from bili_asr.storage.sources import SourceRepository, SourceVideoMetadata
from tests.test_ai_editorial import FakeClient, insert_record, record, runtime
from tests.support.content_permissions import grant_test_permission
from tests.test_publication import approve
from tests.test_workflow_control_plane import _seed_part


@pytest.mark.parametrize("platform", ["bilibili", "youtube"])
def test_universal_chain_keeps_frozen_source_and_exports_explicit_version(tmp_path, platform):
    root = tmp_path / "archive"
    initialize_archive(root)
    with ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session:
        connection = session.connection
        if platform == "bilibili":
            _seed_part(connection)
            part_id = 1
        else:
            with connection:
                part_id = SourceRepository(connection).upsert_video(SourceVideoMetadata(
                    ContentRef("youtube", "dQw4w9WgXcQ", 0), "English source", 2000,
                    "UCcreator", "Creator", 1234567890, "en", 2))
        transcript = replace(record(("This is a complete source sentence.",)), video_part_id=part_id, language="en")
        insert_record(connection, transcript)
        workflow, repository, _handlers, executor = runtime(connection, root, FakeClient())
        prepared = repository.prepare(transcript.transcript_id, None, EditorialConfig())
        assert frozen_version(connection, "input", prepared["input_id"]) == 2
        assert "Do not translate" in prepared["snapshot"]["system_prompt"]
        proof_id, *_ = workflow.request_editorial(video_part_id=part_id, input_id=prepared["input_id"])
        summary = executor.run()
        assert summary.succeeded == 2 and summary.failed == 0
        revision = repository.revision_for_job(proof_id)
        edition = create_edition(connection, revision_id=revision, artifact_roots=(root,), actor="editor")
        assert edition["content_version"] == 2
        assert edition["content"]["source"]["platform"] == platform
        original = edition["content"]["source"]["metadata"]
        assert "sourcePublishedAt" in original and "pubdateUnix" in original
        edited = edit_edition(connection, edition_id=edition["edition_id"], markdown_text="Reviewed English prose.", actor="editor", note="clarify")
        assert edited["content"]["source"]["metadata"] == original
        from bili_asr.publication_tags import sync_source_tags
        if platform == "bilibili":
            with connection:
                connection.execute("INSERT INTO video_tag_observations VALUES ('BVtest','success_empty',1,NULL,NULL)")
            edited = sync_source_tags(connection, edition_id=edited["edition_id"], actor="editor", note="refresh tags")
            assert edited["content_version"] == 2 and edited["content"]["source"]["metadata"] == original
        else:
            with pytest.raises(ValueError, match="verified tag observation"):
                sync_source_tags(connection, edition_id=edited["edition_id"], actor="editor", note="refresh tags")
        grant_test_permission(connection, edited, (root,))
        assert export_publication_drafts(connection, artifact_roots=(root,), output=tmp_path / "drafts") == 1
        draft_catalog = json.loads((tmp_path / "drafts" / "catalog.json").read_text(encoding="utf-8"))
        assert draft_catalog["schemaVersion"] == 3
        export_editorial(connection, revision_id=revision, edition_id=edited["edition_id"], artifact_roots=(root,), output=tmp_path / "review")
        approve(connection, edited)
        release = publish_edition(connection, edition_id=edited["edition_id"], artifact_roots=(root,), write_root=root, actor="publisher")
        assert release["template_version"] == "publish-v2"
        data = verify_release(connection, release["release_id"], (root,))[2]
        assert hashlib.sha256(data).hexdigest() == release["artifact_sha256"]
        with connection:
            if platform == "bilibili":
                connection.execute("UPDATE videos SET pubdate=2345678901,title='new title' WHERE bvid='BVtest'")
            else:
                connection.execute("UPDATE source_videos SET published_at=2345678901,title='new title' WHERE external_id='dQw4w9WgXcQ'")
        assert get_edition(connection, edited["edition_id"])["content"]["source"]["metadata"] == original
        assert export_publications(connection, artifact_roots=(root,), output=tmp_path / "public") == 1
        catalog = json.loads((tmp_path / "public" / "catalog.json").read_text(encoding="utf-8"))
        assert catalog["schemaVersion"] == 3
        entry = catalog["articles"][0]
        assert entry["platform"] == platform and entry["sourceMetadata"] == original
        assert entry["contentSha256"] == edited["content_sha256"]
        if platform == "youtube":
            assert "bvid" not in entry and "cid" not in entry
        from bili_asr.cli import main
        assert main(["publication", "show", "--archive-root", str(root), "--edition-id", edited["edition_id"], "--format", "json"]) == 0
    snapshot = tmp_path / "snapshot.zip"
    save_snapshot(root, snapshot)
    assert check_snapshot(snapshot)["valid"]
    restored = tmp_path / "restored"
    restore_snapshot(snapshot, restored)
    with ArchiveSession(restored, mode=ArchiveAccessMode.READ) as session:
        assert verify_release(session.connection, release["release_id"], (restored,))[2] == data


def test_mixed_historical_and_new_release_catalog_preserves_old_bytes(tmp_path):
    from tests.fixtures.frozen_migration_archive import frozen_archive
    from bili_asr.services.archive_migration import migrate_archive
    source, target = tmp_path / "old", tmp_path / "mixed"
    frozen_archive(source)
    migrate_archive(source, target)
    with ArchiveSession(target, mode=ArchiveAccessMode.WRITE) as session:
        connection = session.connection
        for row in connection.execute("SELECT edition_id FROM publication_editions"):
            grant_test_permission(connection, get_edition(connection, row[0]), (target,))
        historical = [dict(row) for row in connection.execute("SELECT * FROM publication_releases WHERE status='published'")]
        before = {row["release_id"]: verify_release(connection, row["release_id"], (target,))[2] for row in historical}
        with connection:
            part_id = SourceRepository(connection).upsert_video(SourceVideoMetadata(ContentRef("youtube", "dQw4w9WgXcQ", 0), "New source", 2000, published_at=1234567890))
        transcript_id = connection.execute("SELECT COALESCE(MAX(transcript_id),0)+1 FROM transcripts").fetchone()[0]
        insert_record(connection, replace(record(("English prose.",), transcript_id=transcript_id), video_part_id=part_id, language="en"))
        workflow, repository, _handlers, executor = runtime(connection, target, FakeClient())
        prepared = repository.prepare(transcript_id, None, EditorialConfig())
        proof_id, *_ = workflow.request_editorial(video_part_id=part_id, input_id=prepared["input_id"])
        assert executor.run().failed == 0
        edition = create_edition(connection, revision_id=repository.revision_for_job(proof_id), artifact_roots=(target,), actor="editor")
        approve(connection, edition)
        publish_edition(connection, edition_id=edition["edition_id"], artifact_roots=(target,), write_root=target, actor="publisher")
        export_publications(connection, artifact_roots=(target,), output=tmp_path / "catalog")
        catalog = json.loads((tmp_path / "catalog" / "catalog.json").read_text(encoding="utf-8"))
        assert catalog["schemaVersion"] == 3
        assert any("bvid" in entry for entry in catalog["articles"])
        assert any(entry.get("platform") == "youtube" for entry in catalog["articles"])
        for release_id, content in before.items():
            assert verify_release(connection, release_id, (target,))[2] == content
    snapshot = tmp_path / "mixed.zip"
    save_snapshot(target, snapshot)
    assert check_snapshot(snapshot)["valid"]
