"""Explicit catalog v2 installation with a verified, independent database backup."""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

from bili_asr.archive_maintenance import archive_access
from bili_asr.archive_session import ArchiveAccessMode, open_archive_connection
from bili_asr.artifact_inventory import require_no_links
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.storage.extension_contracts import artifact_catalog_version, require_artifact_catalog
from bili_asr.storage.schema_contract import schema_resource
from bili_asr.storage.snapshots import validate_snapshot_database


def migration_statements():
    statement = ""
    for line in schema_resource("schema-artifact-storage-v2-upgrade.sql").splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement
            statement = ""
    if statement.strip():
        raise ValueError("incomplete catalog migration")


def upgrade_catalog_s3(roots: ArtifactRoots, backup: Path) -> dict:
    """Only DDL/marker rows change. Keep the backup even on a failed upgrade.

    Before v2 writes, rollback can use this backup. After new transfers, retain
    v2 or restore a complete older state; never drop new rows to downgrade.
    """
    backup = Path(os.path.abspath(backup))
    require_no_links(backup)
    if backup.exists() or not backup.parent.is_dir():
        raise ValueError("catalog backup requires a new file in an existing directory")
    if any(backup.is_relative_to(base) for base in roots.read_bases()):
        raise ValueError("catalog backup must be outside archive and artifact roots")
    with archive_access(roots.archive_root, exclusive=True, create_root=False), closing(
        open_archive_connection(roots.archive_root, mode=ArchiveAccessMode.WRITE, artifact_roots=roots)
    ) as connection:
        require_artifact_catalog(connection, required=True)
        if artifact_catalog_version(connection) == 2:
            return {"operation": "artifact-storage-v2", "reused": True, "changed": False}
        if connection.in_transaction:
            raise ValueError("catalog upgrade requires its own transaction")
        with backup.open("xb"):
            pass
        with closing(sqlite3.connect(backup)) as preserved:
            connection.backup(preserved)
        source_contract = validate_snapshot_database(backup)
        with backup.open("r+b") as stream:
            os.fsync(stream.fileno())
        from bili_asr.storage_targets import sync_directory
        sync_directory(backup.parent)
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                for statement in migration_statements():
                    connection.execute(statement)
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("catalog migration foreign-key violation")
                require_artifact_catalog(connection, required=True)
        finally:
            connection.execute("PRAGMA foreign_keys=ON")
        contract = validate_snapshot_database(roots.archive_root / "archive.db")
        return {"operation": "artifact-storage-v2", "reused": False, "changed": True,
                "backup_contract": source_contract, "database_contract": contract,
                "backup_retained": True, "artifacts_changed": False, "automatic_source_release": False}
