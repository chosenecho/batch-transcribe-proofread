"""Ordinary contract validation preserves acceptance without executing archive DDL."""
import sqlite3

import pytest

from bili_asr.storage.archive_contracts import bootstrap_contract
from bili_asr.storage.database import require_archive_schema
from bili_asr.storage.schema_contract import SchemaContractError, schema_resource


@pytest.mark.parametrize("contract", ["bilibili-v1", "universal-v2"])
@pytest.mark.parametrize("scripts", [(), ("schema-artifact-storage.sql",),
    ("schema-artifact-storage.sql", "schema-artifact-online.sql"),
    ("schema-preserved-body-import.sql", "schema-source-supplements.sql")])
def test_installed_extension_matrix_is_read_only(contract, scripts):
    with sqlite3.connect(":memory:") as connection:
        bootstrap_contract(connection, contract)
        for script in scripts:
            connection.executescript(schema_resource(script))
        before = list(connection.iterdump())
        statements = []
        connection.set_trace_callback(statements.append)
        connection.execute("PRAGMA query_only=ON")
        require_archive_schema(connection)
        assert list(connection.iterdump()) == before
        assert not any(sql.lstrip().split()[0].upper() in {"CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE"}
                       for sql in statements)


@pytest.mark.parametrize("contract", ["bilibili-v1", "universal-v2"])
@pytest.mark.parametrize("corruption", ["partial", "unknown-marker", "altered-ddl"])
def test_incomplete_or_unknown_extensions_are_rejected_without_repair(contract, corruption):
    with sqlite3.connect(":memory:") as connection:
        bootstrap_contract(connection, contract)
        if corruption == "partial":
            connection.execute("CREATE TABLE manuscript_import_baselines (import_id TEXT PRIMARY KEY)")
        else:
            connection.executescript(schema_resource("schema-preserved-body-import.sql"))
            if corruption == "unknown-marker":
                trigger = connection.execute("SELECT sql FROM sqlite_master WHERE name='extensions_no_update'").fetchone()[0]
                connection.execute("DROP TRIGGER extensions_no_update")
                connection.execute("PRAGMA ignore_check_constraints=ON")
                connection.execute("UPDATE archive_contract_extensions SET name='unknown-extension'")
                connection.execute(trigger)
                connection.execute("PRAGMA ignore_check_constraints=OFF")
            else:
                connection.execute("DROP TABLE publication_import_origins")
        connection.commit()
        before = list(connection.iterdump())
        connection.execute("PRAGMA query_only=ON")
        with pytest.raises(SchemaContractError):
            require_archive_schema(connection)
        assert list(connection.iterdump()) == before
