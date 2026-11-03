"""Source tag names frozen at edition creation or explicit audited refresh."""
from __future__ import annotations

from copy import deepcopy
import sqlite3

from bili_asr.storage.publication import PublicationRepository, read_edition
from bili_asr.publication_content import normalize_actor, normalize_text, normalize_content
from bili_asr.publication_models_v2 import normalize_content_v2


def source_tags(connection: sqlite3.Connection, bvid: str) -> list[str]:
    rows = connection.execute("SELECT tag_name FROM video_tags WHERE bvid=? ORDER BY tag_id", (bvid,))
    return list(dict.fromkeys(row[0] for row in rows))


def tag_coverage(connection: sqlite3.Connection, bvid: str) -> str:
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='video_tag_observations'").fetchone()
    if exists:
        row = connection.execute("SELECT state FROM video_tag_observations WHERE bvid=?", (bvid,)).fetchone()
        if row:
            return row[0]
    return "stored_nonempty" if source_tags(connection, bvid) else "not_attempted"


def sync_source_tags(connection: sqlite3.Connection, *, edition_id: str, actor: str, note: str) -> dict:
    """Create a pending-review edition with source tags and the exact parent CAS."""
    actor, note = normalize_actor(actor), normalize_text(note, "note", nonempty=True)
    repository = PublicationRepository(connection)
    with repository.transaction():
        parent = read_edition(connection, edition_id)
        version = parent["content_version"]
        source = parent["content"]["source"]
        if version == 2:
            if source["platform"] != "bilibili":
                raise ValueError("publication-tags: source has no verified tag observation to sync")
            bvid = source["externalVideoId"]
        else:
            bvid = source["bvid"]
        coverage = tag_coverage(connection, bvid)
        if coverage in {"not_attempted", "unavailable"}:
            raise ValueError("publication-tags: source tag coverage incomplete; run fetch-tags before syncing")
        content = deepcopy(parent["content"])
        content["tags"] = source_tags(connection, bvid)
        return repository.insert_edition(
            part_id=parent["video_part_id"], revision_id=parent["revision_id"],
            content=(normalize_content_v2 if version == 2 else normalize_content)(content), parent_edition_id=edition_id,
            actor=actor, note="Source video_tags sync: " + note, event_type="edited", content_version=version,
        )
