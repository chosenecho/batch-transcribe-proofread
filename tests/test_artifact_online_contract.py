"""Online installation is explicit and keeps the published storage schema fixed."""
from __future__ import annotations

import sqlite3

import pytest

from bili_asr.storage.archive_contracts import _resource, bootstrap_contract
from bili_asr.storage.artifact_online import (
    install_online_in_staged_copy,
    require_artifact_online,
)
from bili_asr.storage.database import SchemaContractError, require_archive_schema
from bili_asr.storage.snapshots import _current_contract, validate_snapshot_database


@pytest.mark.parametrize("kind", ["bilibili-v1", "universal-v2"])
@pytest.mark.parametrize("imports,supplements", [(False, False), (True, False), (True, True)])
def test_explicit_online_stage_has_distinct_snapshot_contract(tmp_path, kind, imports, supplements):
    path = tmp_path / "archive.db"
    with sqlite3.connect(path) as connection:
        bootstrap_contract(connection, kind)
        if imports:
            connection.executescript(_resource("schema-preserved-body-import.sql"))
        if supplements:
            connection.executescript(_resource("schema-source-supplements.sql"))
        connection.executescript(_resource("schema-artifact-storage.sql"))
        original = _current_contract(kind, imports, supplements, True)[1]
        assert validate_snapshot_database(path) == original
        assert require_artifact_online(connection) is False
        with pytest.raises(SchemaContractError, match="separate empty target"):
            require_artifact_online(connection, required=True)
        install_online_in_staged_copy(connection)
        assert require_artifact_online(connection)
        require_archive_schema(connection)
        expected = _current_contract(kind, imports, supplements, True, True)[1]
        assert expected != original
        assert validate_snapshot_database(path) == expected
        with pytest.raises(ValueError, match="already installed"):
            install_online_in_staged_copy(connection)


def test_partial_online_extension_is_rejected_without_installing_missing_tables(tmp_path):
    with sqlite3.connect(tmp_path / "archive.db") as connection:
        bootstrap_contract(connection)
        connection.executescript(_resource("schema-artifact-storage.sql"))
        connection.execute("CREATE TABLE artifact_reservations (reservation_id TEXT PRIMARY KEY)")
        with pytest.raises(SchemaContractError, match="artifact-online-v1"):
            require_archive_schema(connection)
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_online_contract'").fetchone() is None
