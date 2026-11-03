"""Fences for an explicit metadata refresh of the latest stored bundle.

The preferred transcript can change independently. A refresh binds the latest
publication, its stored transcript identity, and its actual ordered segments.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3


def transcript_segments_sha256(connection: sqlite3.Connection, transcript_id: int) -> str:
    rows = [list(row) for row in connection.execute(
        "SELECT ordinal,start_ms,end_ms,text FROM transcript_segments WHERE transcript_id=? ORDER BY ordinal",
        (transcript_id,))]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":"),
                                    allow_nan=False).encode("utf-8")).hexdigest()


def latest_publication(connection: sqlite3.Connection, part_id: int):
    return connection.execute(
        "SELECT p.*,t.content_sha256 FROM workflow_publications p JOIN transcripts t USING(transcript_id) "
        "WHERE p.video_part_id=? ORDER BY p.published_at DESC,p.publication_id DESC LIMIT 1",
        (part_id,)).fetchone()


def publication_fence(connection: sqlite3.Connection, publication) -> dict:
    return {"publication_id": int(publication["publication_id"]),
            "published_at": int(publication["published_at"]),
            "transcript_sha256": str(publication["content_sha256"]),
            "segments_sha256": transcript_segments_sha256(connection, int(publication["transcript_id"]))}


def assert_publication_fence(connection: sqlite3.Connection, part_id: int,
                             transcript_id: int, expected: dict):
    current = latest_publication(connection, part_id)
    if current is None or int(current["transcript_id"]) != transcript_id or publication_fence(connection, current) != expected:
        raise ValueError("publication refresh is stale; plan again against the latest bundle")
    return current
