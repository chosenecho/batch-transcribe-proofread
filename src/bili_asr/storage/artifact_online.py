"""Optional online coordination schema; runtime access never installs it."""
from __future__ import annotations
from bili_asr.storage.extension_contracts import require_artifact_online, artifact_online_objects as extension_objects

import sqlite3
from bili_asr.storage.extension_contracts import require_artifact_catalog


EXTENSION = "artifact-online-v1"
SCHEMA_RESOURCE = "schema-artifact-online.sql"






def install_online_in_staged_copy(connection: sqlite3.Connection) -> None:
    """Internal converter step; caller owns and verifies an unpublished stage."""
    from bili_asr.storage.schema_contract import schema_resource as _resource
    require_artifact_catalog(connection, required=True)
    if require_artifact_online(connection):
        raise ValueError("artifact-online-v1 is already installed")
    if connection.in_transaction:
        raise ValueError("online extension installation requires no active transaction")
    connection.executescript(_resource(SCHEMA_RESOURCE))
    require_artifact_online(connection, required=True)
