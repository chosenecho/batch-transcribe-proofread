"""Resolve retained audio bytes without changing acquisition or replica history."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bili_asr.artifact_inventory import stream_hash
from bili_asr.artifact_root import ArtifactRoots, iter_audio_paths
from bili_asr.path_policy import confined_audio_file
from bili_asr.storage.artifact_catalog import ArtifactCatalog, require_artifact_catalog
from bili_asr.workflow_errors import JobExecutionError


def processing_audio(connection, repository, job) -> dict:
    """Read an exact historical dependency or one unambiguous imported input."""
    from bili_asr.workflow_models import JobKind
    dependency = connection.execute(
        "SELECT 1 FROM workflow_job_dependencies d JOIN workflow_jobs j "
        "ON j.job_id=d.prerequisite_job_id WHERE d.job_id=? AND j.kind='audio'",
        (job.job_id,),
    ).fetchone()
    if dependency is not None:
        return dict(repository.dependency_result(job, JobKind.AUDIO))
    rows = connection.execute(
        "SELECT a.storage_key,a.sha256,a.byte_size,a.duration_ms FROM part_audio_objects p "
        "JOIN audio_objects a USING(audio_id) WHERE p.video_part_id=? ORDER BY a.audio_id DESC",
        (job.video_part_id,),
    ).fetchall()
    if not rows:
        raise JobExecutionError("audio_missing")
    if len({row["sha256"] for row in rows}) != 1:
        raise ValueError("ASR requires one unambiguous imported audio identity")
    return dict(rows[0])


def retained_audio(connection: sqlite3.Connection, video_part_id: int,
                   result: Mapping[str, Any] | None = None) -> dict | None:
    """A registered binding supplies exact expected bytes; absence keeps v1 behavior.

    An ASR consumer uses the succeeded dependency's identity, never the newest
    audio for that part. Acquisition reuse may select the latest bound object.
    """
    if not require_artifact_catalog(connection):
        return None
    rows = connection.execute(
        "SELECT a.audio_id,a.storage_key,a.sha256,a.byte_size,a.duration_ms,b.object_id "
        "FROM part_audio_objects p JOIN audio_objects a USING(audio_id) "
        "JOIN artifact_audio_bindings b USING(audio_id) WHERE p.video_part_id=? ORDER BY a.audio_id DESC",
        (video_part_id,)).fetchall()
    if result is None:
        return dict(rows[0]) if rows else None
    identity = result.get("object_id") or result.get("sha256")
    if identity is not None:
        for row in rows:
            if row["object_id"] == identity:
                if result.get("sha256", identity) != identity:
                    raise JobExecutionError("artifact_input_identity_mismatch", {"action": "artifacts-restore"})
                return {**dict(row), "storage_key": result.get("storage_key") or row["storage_key"]}
        # A new unbound result remains legacy-compatible; a declared stable ID
        # is stronger evidence and must never silently fall back to a filename.
        if result.get("object_id") is not None or rows:
            raise JobExecutionError("artifact_input_identity_mismatch", {"action": "artifacts-restore"})
        return None
    for row in rows:
        if row["storage_key"] == result.get("storage_key"):
            return dict(row)
    return None


def verified_local_audio(connection: sqlite3.Connection, roots: ArtifactRoots, retained: Mapping[str, Any]) -> Path:
    """Try every confined local location, accepting only the retained SHA/size.

    A bad configured-root file cannot shadow a good archive-root copy. The
    first release only uses local bytes: operators explicitly restore a known
    object before scheduling a new consumer. No remote target is probed here.
    """
    identity, size = retained["object_id"], retained["byte_size"]
    catalog = ArtifactCatalog(connection)
    keys = [retained["storage_key"]]
    for replica in catalog.replicas_for_object(identity):
        if replica["package_id"] is None and replica["target_kind"] == "local" and replica["relative_key"] not in keys:
            keys.append(replica["relative_key"])
    for base, key, _candidate in iter_audio_paths(roots, keys):
        try:
            with confined_audio_file(base, key) as safe_path, Path(safe_path).open("rb") as stream:
                if stream_hash(stream) == (size, identity):
                    return _candidate
        except OSError:
            continue
    raise JobExecutionError("artifact_input_unavailable", {
        "object_id": identity, "action": "artifacts-restore", "reason": "local-copy-unavailable"})
