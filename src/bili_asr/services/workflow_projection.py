"""Read-only projections of the SQLite workflow data plane.

Current transcript preference and successfully published content are separate
facts. Read-side consumers expose both and use the published identity for an
existing archive, so new acquisitions cannot turn a completed publication into
ordinary backlog or change its exported text before it is published.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Mapping

from bili_asr.archive import archive_bundle_complete, bundle_relpaths_for_stem
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.archive_session import ArchiveAccessMode, ArchiveContract, open_archive_connection
from bili_asr.formatting import pubdate_utc
from bili_asr.platform_identity import ContentRef
from bili_asr.source_identity import artifact_stem, display_work_id
from bili_asr.storage.archive_contracts import UNIVERSAL_V2, runtime_contract
from bili_asr.storage.metadata import MetadataRepository
from bili_asr.transcript_selection import choose_transcript

WORKFLOW_STATUSES = frozenset({"meta_ok", "subtitle_done", "asr_done", "archived"})
_PART_READ_BATCH_SIZE = 256


def _part_batches(connection: sqlite3.Connection, *, universal: bool) -> Iterator[list[sqlite3.Row]]:
    query = (
        """SELECT video_part_id, bvid, page_index, cid, title AS part_title,
                  duration_ms, video_title, pubdate, platform, external_video_id
           FROM v_source_parts WHERE processing_status <> 'gone' AND video_part_id > ?
           ORDER BY video_part_id LIMIT ?"""
        if universal else
        """SELECT vp.video_part_id, vp.bvid, vp.page_index, vp.cid,
                  vp.title AS part_title, vp.duration_ms, v.title AS video_title,
                  v.pubdate, 'bilibili' AS platform, vp.bvid AS external_video_id
           FROM video_parts AS vp JOIN videos AS v ON v.bvid = vp.bvid
           WHERE vp.processing_status <> 'gone' AND vp.video_part_id > ?
           ORDER BY vp.video_part_id LIMIT ?"""
    )
    after_id = 0
    while True:
        rows = connection.execute(
            query,
            (after_id, _PART_READ_BATCH_SIZE),
        ).fetchall()
        if not rows:
            return
        yield rows
        after_id = int(rows[-1]["video_part_id"])


def _preferred_transcripts(
    connection: sqlite3.Connection, part_ids: tuple[int, ...],
) -> dict[int, Mapping[str, Any]]:
    winners: dict[int, Mapping[str, Any]] = {}
    marks = ",".join("?" for _ in part_ids)
    for row in connection.execute(
        f"SELECT * FROM transcripts WHERE video_part_id IN ({marks})", part_ids,
    ):
        part_id = int(row["video_part_id"])
        held = winners.get(part_id)
        winner = choose_transcript((row,) if held is None else (held, row))
        assert winner is not None
        winners[part_id] = winner
    return winners


def _latest_publications(
    connection: sqlite3.Connection, part_ids: tuple[int, ...],
) -> dict[int, sqlite3.Row]:
    marks = ",".join("?" for _ in part_ids)
    return {
        int(row["video_part_id"]): row
        for row in connection.execute(
            f"""
            WITH latest AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY video_part_id
                    ORDER BY published_at DESC, publication_id DESC
                ) AS recency
                FROM workflow_publications WHERE video_part_id IN ({marks})
            )
            SELECT p.publication_id, p.video_part_id, p.transcript_id, p.artifact_json,
                   p.published_at, t.source_kind, t.language, t.version,
                   t.transcript_id AS stored_transcript_id,
                   t.video_part_id AS transcript_part_id
            FROM latest AS p LEFT JOIN transcripts AS t ON t.transcript_id = p.transcript_id
            WHERE p.recency = 1
            """,
            part_ids,
        )
    }


def _transcript_texts(
    connection: sqlite3.Connection, transcript_ids: tuple[int, ...],
) -> dict[int, str]:
    if not transcript_ids:
        return {}
    marks = ",".join("?" for _ in transcript_ids)
    texts: dict[int, list[str]] = {}
    for row in connection.execute(
        f"""SELECT transcript_id, text FROM transcript_segments
            WHERE transcript_id IN ({marks}) ORDER BY transcript_id, ordinal""",
        transcript_ids,
    ):
        texts.setdefault(int(row["transcript_id"]), []).append(str(row["text"]))
    return {key: "\n".join(value) for key, value in texts.items()}


def _identity_fields(
    transcript: Mapping[str, Any] | None, prefix: str = "",
) -> dict[str, Any]:
    return {
        f"{prefix}source": None if transcript is None or transcript["source_kind"] is None else str(transcript["source_kind"]),
        f"{prefix}language": None if transcript is None or transcript["language"] is None else str(transcript["language"]),
        f"{prefix}version": None if transcript is None or transcript["version"] is None else int(transcript["version"]),
        f"{prefix}transcript_id": None if transcript is None else int(transcript["transcript_id"]),
    }


def workflow_records(
    archive_root: str | Path, *, with_text: bool = False,
    artifact_roots: ArtifactRoots | None = None,
    verify_artifacts: bool = True,
) -> dict[str, dict[str, Any]]:
    """Project one compatible dictionary entry per active video part.

    Existing transcript fields and optional text describe published content
    whenever a publication exists, otherwise the preferred transcript. The
    ``preferred_*`` and ``published_*`` fields make those identities explicit;
    ``publication_current`` reports whether they agree. The returned mapping
    retains the historical pubdate-descending, bvid/page order.

    Reads use bounded part batches and set queries inside one SQLite snapshot.
    Integrity/coverage pass ``verify_artifacts=False`` to retain declared
    terminal state until their own byte verification, preserving corruption
    diagnostics instead of reclassifying broken publications as backlog.
    """
    roots = artifact_roots if artifact_roots is not None else ArtifactRoots.of(archive_root)
    database = roots.archive_root / "archive.db"
    if not database.is_file():
        return {}
    connection = open_archive_connection(
        database, mode=ArchiveAccessMode.READ, contract=ArchiveContract.RUNTIME,
        artifact_roots=roots,
    )
    try:
        connection.execute("BEGIN")
        universal = runtime_contract(connection) == UNIVERSAL_V2
        metadata_repository = MetadataRepository(connection)
        records: dict[str, dict[str, Any]] = {}
        order: dict[str, tuple[int, str, str, int]] = {}
        for parts in _part_batches(connection, universal=universal):
            part_ids = tuple(int(part["video_part_id"]) for part in parts)
            source_facts = metadata_repository.read_source_metadata_export_many(
                part_ids, include_source_metadata=universal,
            )
            preferred = _preferred_transcripts(connection, part_ids)
            publications = _latest_publications(connection, part_ids)
            publication_errors = {
                part_id: ("published_transcript_missing" if row["stored_transcript_id"] is None
                          else "published_transcript_part_mismatch")
                for part_id, row in publications.items()
                if row["stored_transcript_id"] is None or row["transcript_part_id"] != part_id
            }
            transcripts = {
                part_id: publications.get(part_id, preferred.get(part_id))
                for part_id in part_ids
            }
            texts = _transcript_texts(connection, tuple(
                int(row["transcript_id"]) for part_id, row in transcripts.items()
                if row is not None and part_id not in publication_errors
            )) if with_text else {}
            for part in parts:
                part_id = int(part["video_part_id"])
                identity = ContentRef(str(part["platform"]), str(part["external_video_id"]), int(part["page_index"]))
                work_id = display_work_id(identity)
                transcript = transcripts[part_id]
                current = preferred.get(part_id)
                publication = publications.get(part_id)
                publication_error = publication_errors.get(part_id)
                entry: dict[str, Any] = {
                    "work_id": work_id, "bvid": part["bvid"],
                    "page_index": identity.part_index, "cid": part["cid"],
                    "title": str(part["part_title"]), "video_title": str(part["video_title"]),
                    "duration_s": max(0.001, int(part["duration_ms"]) / 1000),
                    "pubdate_str": "" if part["pubdate"] is None else pubdate_utc(int(part["pubdate"])),
                    **_identity_fields(transcript),
                    **_identity_fields(current, "preferred_"),
                    **_identity_fields(publication, "published_"),
                    "publication_current": (
                        publication is not None and current is not None
                        and publication_error is None
                        and publication["transcript_id"] == current["transcript_id"]
                    ),
                }
                if identity.platform != "bilibili":
                    entry.update(platform=identity.platform, external_video_id=identity.external_video_id)
                entry.update({key: value for key, value in source_facts[part_id].items()
                              if key != "sourceMetadata"})
                if universal:
                    metadata = source_facts[part_id]["sourceMetadata"]
                    entry.update(pubdateUnix=metadata["pubdateUnix"], sourcePublishedAt=metadata["sourcePublishedAt"],
                                 sourceMetadata=metadata)
                if publication_error is not None:
                    entry["publication_error"] = publication_error
                paths = bundle_relpaths_for_stem(artifact_stem(identity))
                entry.update(paths)
                if transcript is None:
                    entry["status"] = "meta_ok"
                else:
                    if with_text:
                        entry["transcript_text"] = "" if publication_error else texts.get(int(transcript["transcript_id"]), "")
                    published = publication is not None and not verify_artifacts
                    if publication is not None:
                        try:
                            stored_paths = json.loads(str(publication["artifact_json"]))
                            if not isinstance(stored_paths, dict):
                                raise ValueError("publication paths must be an object")
                            if any(not isinstance(stored_paths.get(key), str) or not stored_paths[key] for key in paths):
                                raise ValueError("publication paths must contain every bundle artifact")
                            paths = {key: stored_paths[key] for key in paths}
                            entry.update(paths)
                            from bili_asr.services.artifact_state import publication_artifact_state
                            local_complete = verify_artifacts and publication_error is None and any(
                                archive_bundle_complete(base, paths) for base in roots.read_bases())
                            storage_state = publication_artifact_state(
                                connection, roots, publication["publication_id"], local_complete=local_complete)
                            if storage_state is not None:
                                entry["artifact_state"] = storage_state
                            if verify_artifacts and publication_error is None:
                                published = local_complete or (storage_state is not None and
                                    storage_state["durability"] == "verified_group_recorded")
                        except (OSError, TypeError, ValueError):
                            # Keep the declaration when another verifier owns
                            # diagnosis, even if the recorded paths are malformed.
                            published = not verify_artifacts
                            entry.update({key: None for key in paths})
                    entry["status"] = "archived" if published else (
                        "asr_done" if transcript["source_kind"] == "asr-local" else "subtitle_done"
                    )
                records[work_id] = entry
                order[work_id] = (-int(part["pubdate"] or 0), identity.platform, identity.external_video_id, identity.part_index)
        return dict(sorted(records.items(), key=lambda item: order[item[0]]))
    finally:
        connection.close()


def workflow_has_data(archive_root: str | Path) -> bool:
    return (Path(archive_root) / "archive.db").is_file()
