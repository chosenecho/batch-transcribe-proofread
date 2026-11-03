"""Preserve explicit restore semantics at manuscript/publication read boundaries."""
from __future__ import annotations

from bili_asr.manuscript_files import read_artifact


def read_retained_text(connection, relative_path, sha256, roots, *, max_bytes=None):
    """Read current local bytes, or identify the exact immutable group to restore."""
    try:
        bounds = {} if max_bytes is None else {"max_bytes": max_bytes}
        installed = connection.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_online_contract'").fetchone()
        if installed:
            from pathlib import Path

            from bili_asr.artifact_root import ArtifactRoots
            from bili_asr.services.artifact_coordination import object_fence
            database = next(row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main")
            with object_fence(ArtifactRoots.of(Path(database).parent), sha256, exclusive=False):
                return read_artifact(relative_path, sha256, roots, **bounds)
        return read_artifact(relative_path, sha256, roots, **bounds)
    except ValueError as error:
        if "artifact is missing:" not in str(error):
            raise
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_group_paths'").fetchone():
            raise
        groups = [row[0] for row in connection.execute(
            "SELECT p.group_id FROM artifact_group_paths p JOIN artifact_group_members m USING(group_id,role) "
            "WHERE p.relative_key=? AND m.object_id=? ORDER BY p.group_id", (relative_path, sha256))]
        if not groups:
            raise
        raise ValueError(f"artifact_restore_required: retained object {sha256}; "
                         f"run artifacts restore-group --group-id {groups[0]} with its storage target binding") from error
