"""Offline source inspection must preserve evidence and reject unknown contracts."""

from __future__ import annotations

from contextlib import closing
from importlib import resources
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

import pytest

from bili_asr.storage import database
from bili_asr.storage.migration_source import MigrationSourceError, inspect_migration_source


@pytest.fixture
def source_database(tmp_path: Path) -> Path:
    path = tmp_path / "archive.db"
    package = resources.files("bili_asr.storage")
    objects = json.loads(package.joinpath("migration-source-bilibili-v1.json").read_text(
        encoding="utf-8"
    ))["objects"]
    with closing(sqlite3.connect(path)) as connection:
        for kind in ("table", "index", "view", "trigger"):
            for shape in objects.values():
                if shape["kind"] == kind:
                    connection.execute(shape["sql"])
        connection.execute("INSERT INTO manuscript_contract VALUES (1)")
        connection.execute("INSERT INTO bilibili_users VALUES (7, '作者', 1, 2)")
        connection.execute("INSERT INTO videos VALUES ('BVlegacy', NULL, 7, '标题', 3, 4, 5)")
        connection.execute(
            "INSERT INTO video_parts VALUES (41, 'BVlegacy', 0, 91, '分 P', 1000, 'metadata_collected', 6, 7)"
        )
        connection.commit()
    return path


def _fingerprints(path: Path):
    return {item.name: item for item in inspect_migration_source(path).table_fingerprints}


def test_fixed_contract_reads_without_bootstrap_or_any_source_file_change(source_database, monkeypatch):
    def refuse_runtime_schema(*args, **kwargs):
        raise AssertionError("future runtime schema must not be used for the legacy reader")

    monkeypatch.setattr(database, "initialize_schema", refuse_runtime_schema)
    monkeypatch.setattr(database, "open_database", refuse_runtime_schema)
    monkeypatch.setattr(database, "_schema_scripts", refuse_runtime_schema)
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in source_database.parent.iterdir()}
    report = inspect_migration_source(source_database)
    after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in source_database.parent.iterdir()}
    assert after == before
    assert report.source_revision == "9b289570494b5e8f7cc564a7eaa5b2eb2c28c3ad"
    assert report.contract_id == "bilibili-archive-v1:sha256:07711441cb6b380fb1524b903aa540568c522c4ef6e49bf9451b3b758f6b1f16"
    assert len(report.table_fingerprints) == 38
    assert _fingerprints(source_database)["video_parts"].row_count == 1
    assert report.derived_objects == ()


@pytest.mark.parametrize("statement, match", [
    ("DROP VIEW v_pending_subtitles", "missing v_pending_subtitles"),
    ("ALTER TABLE videos ADD COLUMN platform TEXT", "incompatible at videos"),
    ("CREATE TABLE private_notes (secret TEXT)", "unsupported object private_notes"),
    ("CREATE TRIGGER extra_job_trigger AFTER UPDATE ON workflow_jobs BEGIN SELECT 1; END", "extra_job_trigger"),
    ("CREATE VIRTUAL TABLE unknown_cache USING fts5(text)", "unsupported object unknown_cache"),
])
def test_unsupported_schema_is_refused_without_source_writes(source_database, statement, match):
    with closing(sqlite3.connect(source_database)) as connection:
        connection.execute(statement)
        connection.commit()
    before = source_database.read_bytes()
    with pytest.raises(MigrationSourceError, match=match):
        inspect_migration_source(source_database)
    assert source_database.read_bytes() == before


def test_same_columns_but_relaxed_data_constraint_is_not_a_supported_contract(source_database):
    with closing(sqlite3.connect(source_database)) as connection:
        sql = connection.execute("SELECT sql FROM sqlite_schema WHERE name = 'video_parts'").fetchone()[0]
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_schema SET sql = ? WHERE name = 'video_parts'",
            (sql.replace("CHECK (duration_ms > 0)", "CHECK (duration_ms >= 0)"),),
        )
        connection.commit()
    with pytest.raises(MigrationSourceError, match="incompatible at video_parts"):
        inspect_migration_source(source_database)


def test_corrupt_and_foreign_key_invalid_sources_are_refused(tmp_path, source_database):
    damaged = tmp_path / "damaged.db"
    damaged.write_bytes(b"SQLite format 3\x00" + b"\x00" * 150)
    with pytest.raises(MigrationSourceError, match="cannot read"):
        inspect_migration_source(damaged)
    with closing(sqlite3.connect(source_database)) as connection:
        connection.execute("UPDATE videos SET mid = 123456")
        connection.commit()
    with pytest.raises(MigrationSourceError, match="foreign-key"):
        inspect_migration_source(source_database)


def test_contract_identity_row_is_required(source_database):
    with closing(sqlite3.connect(source_database)) as connection:
        connection.execute("DELETE FROM manuscript_contract")
        connection.commit()
    with pytest.raises(MigrationSourceError, match="contract identity"):
        inspect_migration_source(source_database)


@pytest.mark.parametrize("tokenizer", ["unicode61", "trigram"])
def test_known_search_cache_is_explicitly_rebuildable_and_not_authoritative(source_database, tokenizer):
    with closing(sqlite3.connect(source_database)) as connection:
        connection.executescript(
            "CREATE VIRTUAL TABLE transcript_fts USING fts5("
            "block_key UNINDEXED, bvid UNINDEXED, page_index UNINDEXED, "
            "start_ms UNINDEXED, end_ms UNINDEXED, pubdate UNINDEXED, "
            f"text, bigram, source UNINDEXED, tokenize='{tokenizer}');"
            "CREATE TABLE transcript_fts_index_meta (key TEXT PRIMARY KEY, value TEXT);"
        )
        connection.execute("INSERT INTO transcript_fts(text) VALUES ('搜索缓存')")
        connection.commit()
    before = source_database.read_bytes()
    report = inspect_migration_source(source_database)
    assert set(report.derived_objects) == {
        "transcript_fts", "transcript_fts_config", "transcript_fts_content",
        "transcript_fts_data", "transcript_fts_docsize", "transcript_fts_idx", "transcript_fts_index_meta",
    }
    assert all(not item.name.startswith("transcript_fts") for item in report.table_fingerprints)
    assert source_database.read_bytes() == before
    with closing(sqlite3.connect(source_database)) as connection:
        connection.execute("DROP TABLE transcript_fts_index_meta")
        connection.commit()
    with pytest.raises(MigrationSourceError, match="incomplete derived search"):
        inspect_migration_source(source_database)


def test_editorial_replay_group_is_exact_and_keeps_frozen_authority(source_database):
    from bili_asr.storage.editorial_replay import install_indexes, objects
    before = inspect_migration_source(source_database)
    with closing(sqlite3.connect(source_database)) as connection:
        assert install_indexes(connection)["installed"]
    indexed = inspect_migration_source(source_database)
    assert indexed.table_fingerprints == before.table_fingerprints
    assert set(indexed.derived_objects) == set(objects())
    with closing(sqlite3.connect(source_database)) as connection:
        name = next(iter(objects()))
        connection.execute(f"DROP INDEX {name}")
        connection.commit()
    with pytest.raises(MigrationSourceError, match="incomplete derived"):
        inspect_migration_source(source_database)


def test_authoritative_table_cannot_hide_behind_known_search_index_name(source_database):
    with closing(sqlite3.connect(source_database)) as connection:
        connection.execute("CREATE TABLE transcript_fts_index_meta (key TEXT PRIMARY KEY, value TEXT, private_history TEXT)")
        connection.commit()
    with pytest.raises(MigrationSourceError, match="incomplete derived search"):
        inspect_migration_source(source_database)


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_uncheckpointed_source_is_refused_without_creating_shared_memory(source_database, suffix):
    companion = source_database.with_name(source_database.name + suffix)
    companion.write_bytes(b"pending transaction evidence")
    before = {path.name: path.read_bytes() for path in source_database.parent.iterdir()}
    with pytest.raises(MigrationSourceError, match="stop writers and checkpoint"):
        inspect_migration_source(source_database)
    assert {path.name: path.read_bytes() for path in source_database.parent.iterdir()} == before


def test_fingerprints_keep_exact_json_text_storage_types_nulls_and_effective_rowid(source_database):
    with closing(sqlite3.connect(source_database)) as connection:
        connection.execute(
            "INSERT INTO workflow_jobs(job_id, kind, dedupe_key, payload_json, status, "
            "available_at, created_at, updated_at) VALUES ('private-job', 'audio', 'key', ?, 'cancelled', 1, 2, 3)",
            ('{ "private": "秘密", "state": 1 }',),
        )
        connection.commit()
    original = _fingerprints(source_database)
    with closing(sqlite3.connect(source_database)) as connection:
        connection.execute("UPDATE workflow_jobs SET payload_json = ?", ('{"private":"秘密","state":1}',))
        connection.commit()
    compact = _fingerprints(source_database)
    assert original["workflow_jobs"].sha256 != compact["workflow_jobs"].sha256
    assert original["videos"] == compact["videos"]
    with closing(sqlite3.connect(source_database)) as connection:
        connection.execute("UPDATE workflow_jobs SET payload_json = CAST(payload_json AS BLOB)")
        connection.commit()
    blob = _fingerprints(source_database)
    assert blob["workflow_jobs"].sha256 != compact["workflow_jobs"].sha256
    with closing(sqlite3.connect(source_database)) as connection:
        connection.execute("UPDATE workflow_jobs SET last_error_code = ''")
        connection.commit()
    empty = _fingerprints(source_database)
    assert empty["workflow_jobs"].sha256 != blob["workflow_jobs"].sha256
    with closing(sqlite3.connect(source_database)) as connection:
        connection.execute("UPDATE workflow_jobs SET rowid = 9")
        connection.commit()
    changed = _fingerprints(source_database)
    assert changed["workflow_jobs"].sha256 != empty["workflow_jobs"].sha256
    assert changed["workflow_jobs"].row_count == 1
    assert changed["workflow_jobs"].includes_rowid
    assert "秘密" not in repr(inspect_migration_source(source_database))


@pytest.mark.parametrize("expiry", [None, int(time.time()) + 3600], ids=("missing-lease", "live-lease"))
def test_live_workflow_lease_blocks_and_expired_lease_is_reported_without_recovery(source_database, expiry):
    with closing(sqlite3.connect(source_database)) as connection:
        connection.execute(
            "INSERT INTO workflow_jobs(job_id, kind, dedupe_key, payload_json, status, "
            "lease_owner, lease_expires_at, available_at, created_at, updated_at) "
            "VALUES ('running', 'audio', 'running', '{}', 'running', 'worker', ?, 1, 1, 1)",
            (expiry,),
        )
        connection.commit()
    with pytest.raises(MigrationSourceError, match="live or missing leases"):
        inspect_migration_source(source_database)
    with closing(sqlite3.connect(source_database)) as connection:
        connection.execute("UPDATE workflow_jobs SET lease_expires_at = 1")
        connection.commit()
    before = source_database.read_bytes()
    assert inspect_migration_source(source_database).expired_running_jobs == 1
    assert source_database.read_bytes() == before


def test_missing_source_is_not_created(tmp_path):
    path = tmp_path / "missing" / "archive.db"
    with pytest.raises(MigrationSourceError, match="missing"):
        inspect_migration_source(path)
    assert not path.parent.exists()


@pytest.mark.parametrize("linked_parent", [False, True])
def test_standalone_reader_refuses_database_or_parent_symlink_before_resolving(source_database, tmp_path, linked_parent):
    target = source_database.parent if linked_parent else source_database
    link = tmp_path / "database-link"
    try:
        link.symlink_to(target, target_is_directory=linked_parent)
    except OSError:
        pytest.skip("platform cannot create symlinks")
    before = source_database.read_bytes()
    source = link / "archive.db" if linked_parent else link
    with pytest.raises(MigrationSourceError, match="symlinks or junctions"):
        inspect_migration_source(source)
    assert source_database.read_bytes() == before


@pytest.mark.skipif(os.name != "nt", reason="Windows junction path semantics")
def test_standalone_reader_refuses_database_under_junction(source_database, tmp_path):
    link = tmp_path / "junction"
    try:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(source_database.parent)],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError:
        pytest.skip("platform cannot create junctions")
    try:
        before = source_database.read_bytes()
        with pytest.raises(MigrationSourceError, match="symlinks or junctions"):
            inspect_migration_source(link / "archive.db")
        assert source_database.read_bytes() == before
    finally:
        # Remove the junction entry only; never recurse into its target.
        link.rmdir()


@pytest.mark.parametrize("changed_field", ["st_dev", "st_ctime_ns"])
def test_standalone_reader_refuses_changed_device_or_ctime_even_with_same_size_and_mtime(source_database, monkeypatch, changed_field):
    original_stat = Path.stat
    reads = 0

    def changed_stat(path, *args, **kwargs):
        nonlocal reads
        info = original_stat(path, *args, **kwargs)
        if path != source_database or kwargs.get("follow_symlinks") is False:
            return info
        reads += 1
        if reads < 3:
            return info
        # Simulate replacement/changing bytes followed by restoring mtime.
        values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        values[changed_field] += 1
        return type("ChangedSourceStat", (), values)()

    monkeypatch.setattr(Path, "stat", changed_stat)
    with pytest.raises(MigrationSourceError, match="changed during inspection"):
        inspect_migration_source(source_database)
