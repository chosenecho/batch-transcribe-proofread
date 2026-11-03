"""Verify frozen paired AI documents without importing publication commands."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterable

from bili_asr.services.artifact_readers import read_retained_text
from bili_asr.manuscript_templates import AI_RENDERERS, renderer_for
from bili_asr.storage.archive_contracts import frozen_version
from bili_asr.storage.publication import PublicationRepository, read_revision


def get_ai_artifacts(connection: sqlite3.Connection, revision_id: str,
                     artifact_roots: Iterable[Path]) -> dict[str, bytes]:
    PublicationRepository(connection)
    revision, prepared = read_revision(connection, revision_id)
    input_version = frozen_version(connection, "input", prepared["input_id"])
    rows = connection.execute(
        "SELECT * FROM document_artifacts WHERE revision_id = ?", (revision_id,)
    ).fetchall()
    expected = {"ai-draft.md": "ai-draft", "review.md": "review-reference"}
    if len(rows) != 2 or {r["artifact_name"] for r in rows} != expected.keys():
        raise ValueError("publication-integrity: complete paired AI artifacts are required")
    if hasattr(artifact_roots, "read_bases"):
        artifact_roots = artifact_roots.read_bases()
    roots = tuple(Path(root) for root in artifact_roots)
    if not roots:
        raise ValueError("manuscript-path: at least one artifact root is required")
    artifacts = {}
    for row in rows:
        name = row["artifact_name"]
        version = row["template_version"]
        if version != ("ai-draft-v2" if input_version == 2 else "ai-draft-v1"):
            raise ValueError("publication-integrity: AI template differs from frozen input version")
        documents = renderer_for(AI_RENDERERS, version)(prepared["snapshot"]["metadata"], prepared,
                                                       json.loads(revision["blocks_json"]), revision_id)
        path = f"documents/part-{revision['video_part_id']}/{revision_id}/{version}/{name}"
        if (row["manuscript_role"] != expected[name]
                or row["relative_path"] != path or re.fullmatch(r"[0-9a-f]{64}", row["content_sha256"]) is None
                or row["content_sha256"] != hashlib.sha256(documents[name].encode("utf-8")).hexdigest()):
            raise ValueError("publication-integrity: AI artifact identity does not match revision")
        artifacts[name] = read_retained_text(connection, path, row["content_sha256"], roots)
    return artifacts
