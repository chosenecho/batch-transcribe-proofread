"""Validate immutable editorial revision evidence without publication state."""
from __future__ import annotations

import json
import re
import sqlite3

from bili_asr.canonical_json import digest
from bili_asr.storage.archive_contracts import frozen_version

_IDENTITY = re.compile(r"[A-Za-z0-9_-]+\Z")


def read_revision(connection: sqlite3.Connection, revision_id: str) -> tuple[dict, dict]:
    if not isinstance(revision_id, str) or not _IDENTITY.fullmatch(revision_id):
        raise ValueError("publication-revision: invalid revision ID")
    row = connection.execute(
        "SELECT r.*, i.video_part_id, i.base_transcript_id, i.reference_transcript_id, "
        "i.prepared_json FROM editorial_revisions r "
        "JOIN editorial_inputs i ON i.input_id = r.input_id WHERE r.revision_id = ?",
        (revision_id,),
    ).fetchone()
    if row is None:
        raise ValueError("publication-revision: unknown revision ID")
    try:
        prepared = json.loads(row["prepared_json"])
        snapshot = prepared["snapshot"]
        blocks = json.loads(row["blocks_json"])
        metadata = snapshot["metadata"]
        if (prepared["input_id"] != row["input_id"] or digest(snapshot) != row["input_id"]
                or digest({"input_id": row["input_id"], "blocks": blocks}) != revision_id
                or snapshot["video_part_id"] != row["video_part_id"]
                or snapshot["base"]["transcript_id"] != row["base_transcript_id"]
                or (snapshot["reference"]["transcript_id"] if snapshot["reference"] else None)
                    != row["reference_transcript_id"]):
            raise ValueError("publication-integrity: frozen revision identity mismatch")
        for transcript_id in (row["base_transcript_id"], row["reference_transcript_id"]):
            if transcript_id is None:
                continue
            transcript = connection.execute(
                "SELECT video_part_id FROM transcripts WHERE transcript_id = ?", (transcript_id,)
            ).fetchone()
            if transcript is None or transcript[0] != row["video_part_id"]:
                raise ValueError("publication-integrity: frozen transcript belongs to another part")
        job = connection.execute(
            "SELECT video_part_id FROM workflow_jobs WHERE job_id = ?", (row["job_id"],)
        ).fetchone()
        if job is None or job[0] != row["video_part_id"]:
            raise ValueError("publication-integrity: frozen revision job belongs to another part")
        from bili_asr.storage.sources import SourceRepository
        live = SourceRepository(connection).part(row["video_part_id"])
        version = frozen_version(connection, "input", row["input_id"])
        if version == 2:
            from bili_asr.source_metadata import SourceMetadataSnapshot
            ref = SourceMetadataSnapshot.from_dict(metadata["source_metadata"]).ref
            matches = live["content_ref"] == ref
        else:
            matches = (live["bvid"], live["page_index"]) == (metadata["bvid"], metadata["page_index"])
        if not matches:
            raise ValueError("publication-integrity: frozen revision belongs to another video part")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("publication-integrity: invalid frozen revision") from exc
    return dict(row), prepared
