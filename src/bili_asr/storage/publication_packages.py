"""Explicit publication package extension, preserving pre-installation releases."""
import sqlite3
from typing import Any

from bili_asr.storage.publication_package_contract import POLICY, objects, require_packages


def install_packages(connection: sqlite3.Connection) -> dict[str, Any]:
    """Install explicitly under an archive maintenance lease, in one transaction."""
    if connection.in_transaction:
        raise ValueError("publication package installation needs its own transaction")
    if require_packages(connection):
        return {"policy": POLICY, "installed": True, "reused": True}
    from bili_asr.storage.database import require_manuscript_schema
    require_manuscript_schema(connection)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        for sql in objects().values():
            connection.execute(sql[1])
        connection.execute("INSERT INTO publication_package_legacy SELECT release_id FROM publication_releases")
        connection.execute("INSERT INTO publication_packages_contract VALUES (1,1)")
        require_packages(connection)
    return {"policy": POLICY, "installed": True, "reused": False}


def package_record(connection: sqlite3.Connection, release_id: str) -> dict[str, str] | None:
    mode = require_packages(connection)
    if not mode:
        return None
    row = connection.execute("SELECT relative_path,package_sha256 FROM publication_packages WHERE release_id=?",
                             (release_id,)).fetchone()
    if row is None and (mode == "embedded" or connection.execute(
        "SELECT 1 FROM publication_package_legacy WHERE release_id=?", (release_id,)).fetchone() is None):
        raise ValueError("publication-integrity: release package is missing")
    return {"relative_path": row[0], "package_sha256": row[1]} if row is not None else None
