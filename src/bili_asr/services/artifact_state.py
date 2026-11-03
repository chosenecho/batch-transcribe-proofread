"""Read production, historical durability, locality and readiness independently.

Ordinary reports inspect local metadata only. Target probes read manifests and
never extract payloads or alter the archive. Verification timestamps describe
recorded evidence, not an assertion that a disconnected target is reachable.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from bili_asr.artifact_inventory import require_regular_file
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.services.artifact_access import ArtifactAccess
from bili_asr.storage.artifact_catalog import (
    require_artifact_catalog,
    storage_key_parts,
)


def _local_present(roots, replica):
    bases = {"local": roots.archive_root, "local-artifacts": roots.artifact_root}
    base = bases.get(replica["target_id"])
    if base is None:
        return False
    try:
        path = base.joinpath(*storage_key_parts(replica["relative_key"]))
        require_regular_file(path)
        return path.stat().st_size == replica["verified_byte_size"]
    except (OSError, ValueError):
        return False


def _object_state(connection, roots, object_id, *, access=None):
    replicas = [dict(row) for row in connection.execute(
        "SELECT * FROM artifact_replicas WHERE object_id=? AND presence!='released'", (object_id,))]
    local = any(row["package_id"] is None and _local_present(roots, row) for row in replicas)
    external = [row for row in replicas if row["package_id"] is not None]
    availability = "unchecked"
    if external and access is not None:
        try:
            access.locate_external(object_id)
            availability = "reachable"
        except (OSError, ValueError):
            availability = "storage_unavailable"
    return {
        "object_id": object_id,
        "durability": "verified_replica_recorded" if local or external else "no_verified_replica",
        "verified_at": max((row["verified_at"] for row in replicas), default=None),
        "locality": "local_present" if local else "offloaded" if external else "missing",
        "readiness": "local_verification_required" if local else (
            "storage_unavailable" if availability == "storage_unavailable" else
            "restore_required" if external else "restore_blocked"),
        "target_availability": availability if external else "not_applicable",
    }


def artifact_state(connection: sqlite3.Connection, roots: ArtifactRoots, *,
                   storage_targets: dict[str, Path] | None = None, check_targets: bool = False) -> dict:
    """Return observations without changing workflow status or restoring files."""
    installed = require_artifact_catalog(connection)
    if not installed:
        return {"schema": "artifact-state-v1", "catalog": "not_installed", "objects": []}
    access = ArtifactAccess(connection, roots, storage_targets or {}) if check_targets else None
    objects = [_object_state(connection, roots, row[0], access=access)
               for row in connection.execute("SELECT object_id FROM artifact_objects ORDER BY object_id")]
    return {"schema": "artifact-state-v1", "catalog": "installed", "objects": objects}


def publication_artifact_state(connection: sqlite3.Connection, roots: ArtifactRoots,
                               publication_id: int, *, local_complete: bool) -> dict | None:
    """Summarize a catalog-bound publication without probing remote storage."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_publication_groups'").fetchone():
        return None
    group = connection.execute("SELECT group_id FROM artifact_publication_groups WHERE publication_id=?", (publication_id,)).fetchone()
    if group is None:
        return None
    members = [_object_state(connection, roots, row[0]) for row in connection.execute(
        "SELECT object_id FROM artifact_group_members WHERE group_id=? ORDER BY role", (group[0],))]
    retained = len(members) == 6 and all(member["durability"] == "verified_replica_recorded" for member in members)
    return {"schema": "artifact-publication-state-v1", "group_id": group[0],
            "production": "published", "durability": "verified_group_recorded" if retained else "incomplete",
            "locality": "local_complete" if local_complete else "offloaded" if retained else "missing",
            "readiness": "ready" if local_complete else "restore_required" if retained else "restore_blocked",
            "target_availability": "unchecked", "members": members}
