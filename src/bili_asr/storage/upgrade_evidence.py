"""Read-only contract and row evidence for explicit offline upgrades."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from bili_asr.contracts.registry import (
    BILIBILI_V1,
    IMPORT_EXTENSION,
    SOURCE_SUPPLEMENT_POLICY,
)
from bili_asr.sql_identity import schema_sql_sha256
from bili_asr.storage.archive_contracts import runtime_contract
from bili_asr.storage.import_origins import require_import_extension
from bili_asr.storage.migration_source import (
    inspect_migration_source,
    legacy_source_contract,
    typed_table_fingerprint,
)
from bili_asr.storage.snapshots import (
    _current_contract,
    _schema,
    validate_snapshot_database,
)
from bili_asr.storage.source_supplements import require_supplement_extension


def readonly(database: Path):
    connection = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def source_evidence(database: Path) -> dict:
    """Reject unknown schema objects and unfinished executions before copying."""
    with closing(readonly(database)) as connection:
        kind = runtime_contract(connection)
        from bili_asr.storage.artifact_catalog import EXTENSION as ARTIFACT_EXTENSION
        from bili_asr.storage.artifact_catalog import require_artifact_catalog
        from bili_asr.storage.artifact_online import EXTENSION as ONLINE_EXTENSION
        from bili_asr.storage.artifact_online import require_artifact_online
        artifacts = require_artifact_catalog(connection)
        online = require_artifact_online(connection)
        if kind == BILIBILI_V1:
            if artifacts:
                raise ValueError("artifact-extended legacy source requires an explicitly registered conversion")
            inspect_migration_source(database)
            required = {name: value for name, value in legacy_source_contract()["objects"].items()}
            extensions = ()
        else:
            imports = require_import_extension(connection)
            supplements = require_supplement_extension(connection)
            from bili_asr.storage.source_observations import require_observations
            observations = require_observations(connection)
            validate_snapshot_database(database)
            required, _ = _current_contract(kind, imports, supplements, artifacts, online, observations=observations)
            extensions = (((IMPORT_EXTENSION,) if imports else ()) + ((SOURCE_SUPPLEMENT_POLICY,) if supplements else ())
                          + ((ARTIFACT_EXTENSION,) if artifacts else ()) + ((ONLINE_EXTENSION,) if online else ())
                          + (("source-observations/v1",) if observations else ()))
        actual = _schema(connection)
        extras = set(actual) - set(required)
        # Search indexes are derived but must still match a registered definition.
        for group in legacy_source_contract()["derived_groups"]:
            names = set(group["objects"])
            if not names & extras:
                continue
            if not names <= extras:
                raise ValueError("incomplete derived search index")
            for name, variants in group["objects"].items():
                row = connection.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
                if {"kind": row[0], "sql_sha256": schema_sql_sha256(row[1])} not in variants:
                    raise ValueError("unregistered derived search index definition")
            extras -= names
        if extras:
            raise ValueError("unregistered source schema object: " + min(extras))
        for table, condition in (
            ("workflow_jobs", "status='running'"), ("workflow_attempts", "outcome='running'"),
            ("ingestion_runs", "outcome='running'"), ("acquisition_runs", "outcome='running'"),
            ("editorial_model_calls", "finished_at IS NULL"),
        ):
            if connection.execute(f"SELECT 1 FROM {table} WHERE {condition} LIMIT 1").fetchone():
                raise ValueError("unfinished execution requires explicit recovery before upgrade: " + table)
        tables = []
        for name, definition in sorted(required.items()):
            if definition["kind"] != "table":
                continue
            columns = [row[1] for row in connection.execute('PRAGMA table_info("' + name.replace('"', '""') + '")')]
            tables.append({**asdict(typed_table_fingerprint(connection, name, columns)), "columns": columns})
        return {"contracts": [kind, *extensions], "tables": tables,
                "job_states": dict(connection.execute("SELECT status,count(*) FROM workflow_jobs GROUP BY status"))}


def verify_preserved_tables(database: Path, tables: list[dict]) -> None:
    with closing(readonly(database)) as connection:
        for expected in tables:
            actual = asdict(typed_table_fingerprint(connection, expected["name"], expected["columns"]))
            if actual != {key: value for key, value in expected.items() if key != "columns"}:
                raise ValueError("upgrade changed protected authority rows: " + expected["name"])
