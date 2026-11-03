"""Authoritative workflow output facts within the caller's fenced transaction."""
from __future__ import annotations

import json
import time

from bili_asr.storage.artifact_catalog import ArtifactCatalog, require_artifact_catalog


def record_audio(connection, *, part_id, sha256, byte_size, media_format,
                 duration_ms, storage_key, target_id, acquired_at=None):
    if not connection.in_transaction:
        raise RuntimeError("audio registration requires a write transaction")
    now = int(time.time()) if acquired_at is None else acquired_at
    connection.execute(
        "INSERT INTO audio_objects(sha256,byte_size,format,duration_ms,storage_key,created_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(sha256) DO UPDATE SET duration_ms=excluded.duration_ms",
        (sha256, byte_size, media_format, duration_ms, storage_key, now))
    audio_id = connection.execute("SELECT audio_id FROM audio_objects WHERE sha256=?", (sha256,)).fetchone()[0]
    connection.execute(
        "INSERT OR IGNORE INTO part_audio_objects(video_part_id,audio_id,acquired_at,acquisition_source) "
        "VALUES (?,?,?,'workflow')", (part_id, audio_id, now))
    if require_artifact_catalog(connection):
        catalog = ArtifactCatalog(connection)
        object_id = catalog.register_object(sha256, byte_size)
        catalog.bind_audio(audio_id, object_id)
        catalog.register_target(target_id, kind="local")
        previous = connection.execute(
            "SELECT MAX(generation) FROM artifact_replicas WHERE target_id=? AND relative_key=? AND member_key=''",
            (target_id, storage_key)).fetchone()[0]
        catalog.record_verified_replica(object_id, target_id, storage_key, sha256=sha256,
                                       byte_size=byte_size, generation=int(previous or 0) + 1)
    return audio_id


def record_bundle_publication(connection, *, part_id, transcript_id, paths, published_at=None):
    if not connection.in_transaction:
        raise RuntimeError("publication registration requires a write transaction")
    previous = connection.execute(
        "SELECT MAX(published_at) FROM workflow_publications WHERE video_part_id=?", (part_id,)).fetchone()[0]
    now = int(time.time()) if published_at is None else published_at
    timestamp = max(now, 0 if previous is None else int(previous) + 1)
    connection.execute(
        "INSERT INTO workflow_publications(video_part_id,transcript_id,published_at,artifact_json) VALUES (?,?,?,?) "
        "ON CONFLICT(video_part_id,transcript_id) DO UPDATE SET "
        "published_at=excluded.published_at,artifact_json=excluded.artifact_json",
        (part_id, transcript_id, timestamp, json.dumps(paths, ensure_ascii=False, separators=(",", ":"))))
    return connection.execute(
        "SELECT publication_id FROM workflow_publications WHERE video_part_id=? AND transcript_id=?",
        (part_id, transcript_id)).fetchone()[0]
