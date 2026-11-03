from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from bili_asr.canonical_json import digest
from bili_asr.services.archive_migration import initialize_archive
from bili_asr.source_observations import PlatformObservation
from bili_asr.storage.database import connect_database
from bili_asr.storage.maintenance_merge import merge_database
from bili_asr.storage.source_observations import ObservationRepository


def _trigger_inventory(root):
    with closing(sqlite3.connect(root / "archive.db")) as connection:
        return dict(connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"))


def _bilibili_archive(root, *, title="Bilibili title"):
    initialize_archive(root, with_source_observations=True)
    with closing(connect_database(root / "archive.db")) as connection:
        connection.execute(
            "INSERT INTO bilibili_users(mid,display_name,created_at,updated_at) VALUES(1,'creator',1,1)")
        connection.execute(
            "INSERT INTO videos(bvid,aid,mid,title,pubdate,created_at,updated_at) "
            "VALUES('BVmerge01',1,1,?,?,1,1)", (title, 1))
        connection.execute(
            "INSERT INTO video_parts(bvid,cid,page_index,title,duration_ms,processing_status,created_at,updated_at) "
            "VALUES('BVmerge01',2,0,'part',1000,'discovered',1,1)")
        connection.commit()


def _youtube_archive(root):
    initialize_archive(root, with_source_observations=True)
    with closing(connect_database(root / "archive.db")) as connection:
        connection.execute(
            "INSERT INTO source_creators(platform,external_id,display_name,created_at,updated_at) "
            "VALUES('youtube','creator-y','YouTube creator',1,1)")
        creator_id = connection.execute(
            "SELECT creator_id FROM source_creators WHERE platform='youtube' AND external_id='creator-y'").fetchone()[0]
        connection.execute(
            "INSERT INTO source_videos(platform,external_id,creator_id,title,canonical_url,observed_at,created_at,updated_at) "
            "VALUES('youtube','video-y',?,'YouTube title','https://www.youtube.com/watch?v=video-y',1,1,1)",
            (creator_id,))
        video_id = connection.execute(
            "SELECT source_video_id FROM source_videos WHERE platform='youtube' AND external_id='video-y'").fetchone()[0]
        connection.execute(
            "INSERT INTO video_parts(source_video_id,page_index,title,duration_ms,processing_status,created_at,updated_at) "
            "VALUES(?,0,'YouTube part',1000,'discovered',1,1)", (video_id,))
        connection.commit()


def test_universal_merge_suspends_projection_writers_and_is_idempotent(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    _bilibili_archive(source)
    initialize_archive(target, with_source_observations=True)
    before = _trigger_inventory(target)
    report = merge_database(source / "archive.db", target / "archive.db")
    assert report["tables"]["videos"]["added"] == 1
    assert _trigger_inventory(target) == before
    assert "source_video_identity_immutable" in before
    second = merge_database(source / "archive.db", target / "archive.db")
    assert second["tables"]["videos"]["skipped"] == 1
    assert _trigger_inventory(target) == before


def test_universal_merge_remaps_disjoint_bilibili_and_youtube_rows(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    _bilibili_archive(source)
    _youtube_archive(target)
    report = merge_database(source / "archive.db", target / "archive.db")
    assert {item[0] for item in _rows(target, "SELECT platform FROM source_videos")} == {"bilibili", "youtube"}
    assert report["id_mappings"]["source_videos"]
    assert report["id_mappings"]["video_parts"]
    with closing(sqlite3.connect(target / "archive.db")) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    before = _rows(target, "SELECT * FROM source_videos ORDER BY source_video_id")
    again = merge_database(source / "archive.db", target / "archive.db")
    assert again["tables"]["source_videos"]["added"] == 0
    assert again["tables"]["video_parts"]["added"] == 0
    assert _rows(target, "SELECT * FROM source_videos ORDER BY source_video_id") == before


def test_universal_merge_rejects_true_natural_identity_conflict_atomically(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    _bilibili_archive(source, title="source title")
    _bilibili_archive(target, title="target title")
    before = (target / "archive.db").read_bytes()
    with pytest.raises(ValueError, match="unique identity conflict|identity conflict"):
        merge_database(source / "archive.db", target / "archive.db")
    assert (target / "archive.db").read_bytes() == before


def test_universal_merge_preserves_raw_observation_evidence(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    initialize_archive(source, with_source_observations=True)
    initialize_archive(target, with_source_observations=True)
    observation = PlatformObservation.capture(
        platform="bilibili", operation="details", endpoint="fixture/details",
        scope={"externalVideoId": "BVraw", "partIndex": 0}, extractor="test",
        extractor_version="1", payload={"title": "raw"}, observed_at=1)
    with closing(connect_database(source / "archive.db")) as connection:
        ObservationRepository(connection).append((observation,))
        connection.commit()
    merge_database(source / "archive.db", target / "archive.db")
    with closing(sqlite3.connect(target / "archive.db")) as connection:
        assert connection.execute(
            "SELECT envelope_json,envelope_sha256 FROM platform_observations WHERE observation_id=?", (observation.observation_id,)
        ).fetchone() == (observation.envelope_json, digest(observation.envelope))


def test_legacy_bilibili_part_natural_key_makes_disjoint_repeat_merge_idempotent(tmp_path):
    from bili_asr.storage.database import initialize_schema
    source, target = tmp_path / "source", tmp_path / "target"
    for index, root in enumerate((source, target), 1):
        root.mkdir()
        with closing(sqlite3.connect(root / "archive.db")) as connection:
            initialize_schema(connection)
            connection.execute("INSERT INTO bilibili_users VALUES(1,'creator',1,1)")
            connection.execute("INSERT INTO videos VALUES(?,?,1,'title',1,1,1)", (f"BVlegacy{index}", index))
            connection.execute("INSERT INTO video_parts VALUES(1,?,0,?,'part',1000,'discovered',1,1)", (f"BVlegacy{index}", index))
            connection.commit()
    first = merge_database(source / "archive.db", target / "archive.db")
    before = _rows(target, "SELECT bvid,page_index,title FROM video_parts ORDER BY video_part_id")
    second = merge_database(source / "archive.db", target / "archive.db")
    after = _rows(target, "SELECT bvid,page_index,title FROM video_parts ORDER BY video_part_id")
    assert first["tables"]["video_parts"]["added"] >= 1
    assert second["tables"]["video_parts"]["added"] == 0
    assert before == after


def _rows(root, query):
    with closing(sqlite3.connect(root / "archive.db")) as connection:
        return connection.execute(query).fetchall()


@pytest.mark.parametrize("failure", ["restore", "validation"])
def test_merge_failure_after_copy_rolls_back_rows_and_triggers(tmp_path, monkeypatch, failure):
    import bili_asr.storage.maintenance_merge as merge
    source, target = tmp_path / "source", tmp_path / "target"
    _bilibili_archive(source)
    initialize_archive(target, with_source_observations=True)
    before = (target / "archive.db").read_bytes()
    triggers = _trigger_inventory(target)
    def fail(*_args):
        raise ValueError("injected " + failure + " failure")
    monkeypatch.setattr(merge, "_restore_projection_triggers" if failure == "restore" else "_validate_staged_database", fail)
    with pytest.raises(ValueError, match="injected"):
        merge_database(source / "archive.db", target / "archive.db")
    assert (target / "archive.db").read_bytes() == before
    assert _trigger_inventory(target) == triggers
    assert not list(target.glob(".archive.merge-*.db"))


@pytest.mark.parametrize("damage", ["missing", "drift"])
def test_merge_refuses_damaged_projection_inventory_before_mutating_target(tmp_path, damage):
    source, target = tmp_path / "source", tmp_path / "target"
    _bilibili_archive(source)
    initialize_archive(target, with_source_observations=True)
    with closing(sqlite3.connect(source / "archive.db")) as connection:
        connection.execute("DROP TRIGGER source_video_from_bili_insert")
        if damage == "drift":
            connection.execute("CREATE TRIGGER source_video_from_bili_insert AFTER INSERT ON videos BEGIN SELECT 1; END")
        connection.commit()
    before = (target / "archive.db").read_bytes()
    with pytest.raises(ValueError, match="missing|incompatible"):
        merge_database(source / "archive.db", target / "archive.db")
    assert (target / "archive.db").read_bytes() == before


def test_merge_preserves_real_frozen_asr_ai_and_observation_bindings(tmp_path, enable_content_publication_for_tests):
    from tests.test_provenance_trace import manuscript
    source_parent = tmp_path / "source"
    source_parent.mkdir()
    connection, source, *_ = manuscript(source_parent, "bilibili", raw_kind="details")
    connection.close()
    target = tmp_path / "target"
    initialize_archive(target, with_source_observations=True)
    _rights_extension(target)
    evidence_tables = ("transcripts", "transcript_asr_evidence", "editorial_inputs", "editorial_model_calls",
                       "editorial_revisions", "platform_observations", "publication_editions")
    before = {table: _rows(source, f'SELECT * FROM "{table}" ORDER BY rowid') for table in evidence_tables}
    merge_database(source / "archive.db", target / "archive.db")
    for table in evidence_tables:
        assert _rows(target, f'SELECT * FROM "{table}" ORDER BY rowid') == before[table]


def test_merge_refuses_frozen_part_remap_without_changing_evidence(tmp_path, enable_content_publication_for_tests):
    from tests.test_provenance_trace import manuscript
    source_parent = tmp_path / "source"
    source_parent.mkdir()
    connection, source, *_ = manuscript(source_parent, "bilibili", raw_kind="details")
    connection.close()
    target = tmp_path / "target"
    _youtube_archive(target)
    _rights_extension(target)
    before = (target / "archive.db").read_bytes()
    with pytest.raises(ValueError, match="frozen-history.*video_parts"):
        merge_database(source / "archive.db", target / "archive.db")
    assert (target / "archive.db").read_bytes() == before


def _rights_extension(root):
    from bili_asr.storage.archive_contracts import _resource
    with closing(sqlite3.connect(root / "archive.db")) as connection:
        connection.executescript(_resource("schema-rights.sql"))
