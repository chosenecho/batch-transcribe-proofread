"""Derived SQL and unknown objects remain explicit upgrade evidence boundaries."""
from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing

import pytest

from bili_asr.search_index.store import TranscriptSearchIndex
from bili_asr.services.archive_migration import initialize_archive
from bili_asr.storage.upgrade_evidence import source_evidence


@pytest.mark.parametrize("damage,reason", [("definition", "unregistered derived search index definition"),
                                            ("partial", "incomplete derived search index"),
                                            ("unknown", "unregistered source schema object")])
def test_source_evidence_rejects_derived_impostors_and_unknown_tables_without_rewriting(tmp_path, damage, reason):
    root = tmp_path / "archive"
    initialize_archive(root)
    database = root / "archive.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        TranscriptSearchIndex(root)._ensure_schema(connection)
    assert source_evidence(database)["contracts"] == ["universal-v2"]
    with closing(sqlite3.connect(database)) as connection, connection:
        if damage in {"definition", "partial"}:
            connection.execute("DROP TABLE transcript_fts_index_meta")
        if damage == "definition":
            connection.execute("CREATE TABLE transcript_fts_index_meta(key TEXT PRIMARY KEY,value BLOB)")
        elif damage == "unknown":
            connection.execute("CREATE TABLE operator_unregistered_state(secret TEXT)")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match=reason):
        source_evidence(database)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
