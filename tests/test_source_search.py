"""Neutral search reads corrected source dates independently of cached text."""
from dataclasses import replace

import pytest

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.platform_identity import ContentRef
from bili_asr.search_index import MetadataSearchIndex, TranscriptSearchIndex
from bili_asr.search_index.query import search_archive
from bili_asr.services.archive_migration import initialize_archive
from bili_asr.storage.sources import SourceRepository, SourceVideoMetadata
from tests.test_ai_editorial import insert_record, record
from tests.test_workflow_control_plane import _seed_part


@pytest.mark.parametrize("platform", ["bilibili", "youtube"])
def test_corrected_publication_date_filters_existing_fts_without_reindex(tmp_path, platform):
    root = tmp_path / "archive"
    initialize_archive(root)
    with ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session:
        connection = session.connection
        with connection:
            if platform == "bilibili":
                _seed_part(connection)
                part_id = 1
                connection.execute("UPDATE videos SET title='English source',pubdate=1000 WHERE bvid='BVtest'")
            else:
                part_id = SourceRepository(connection).upsert_video(SourceVideoMetadata(
                    ContentRef("youtube", "dQw4w9WgXcQ", 0), "English source", 2000, published_at=1000))
        insert_record(connection, replace(record(("English source sentence.",)),
                                          video_part_id=part_id, language="en"))

    index = TranscriptSearchIndex(root)
    assert index.build() == 1
    with ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session:
        connection = session.connection
        with connection:
            if platform == "bilibili":
                connection.execute("UPDATE videos SET pubdate=2000 WHERE bvid='BVtest'")
            else:
                SourceRepository(connection).upsert_video(SourceVideoMetadata(
                    ContentRef("youtube", "dQw4w9WgXcQ", 0), "English source", 2000, published_at=2000))
        # This is deliberately an existing text cache containing the old date.
        assert connection.execute("SELECT pubdate FROM source_transcript_fts").fetchone()[0] == 1000

    before = (root / "archive.db").read_bytes()
    for scope in ("transcripts", "metadata", "all"):
        hits = search_archive(root, "English", scope=scope, pubdate_from=1500).hits
        assert len(hits) == (2 if scope == "all" else 1)
        assert {hit.pubdate for hit in hits} == {2000}
        assert {hit.to_dict()["pubdate"] for hit in hits} == {2000}
        assert not search_archive(root, "English", scope=scope, pubdate_to=1500).hits
    assert (root / "archive.db").read_bytes() == before
    # An ordinary incremental build also need not rewrite immutable text rows.
    assert index.build() == 0
    assert search_archive(root, "English", pubdate_from=1500).hits[0].pubdate == 2000


@pytest.mark.parametrize("index_type", [MetadataSearchIndex, TranscriptSearchIndex])
@pytest.mark.parametrize("marker", ["empty", "unsupported"])
def test_search_schema_probe_still_rejects_an_altered_explicit_marker(tmp_path, index_type, marker):
    root = tmp_path / "archive"
    initialize_archive(root)
    with ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session:
        connection = session.connection
        with connection:
            if marker == "empty":
                connection.execute("DELETE FROM archive_contract")
            else:
                connection.execute("PRAGMA ignore_check_constraints=ON")
                connection.execute("UPDATE archive_contract SET contract='unsupported-v3'")
    before = (root / "archive.db").read_bytes()
    index = index_type(root)
    search = index.search_metadata if index_type is MetadataSearchIndex else index.search_blocks
    with pytest.raises(ValueError, match="unsupported or altered archive contract marker"):
        search("needle")
    assert (root / "archive.db").read_bytes() == before
