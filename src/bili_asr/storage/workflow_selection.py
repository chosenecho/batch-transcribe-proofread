"""Resolve workflow targets from stored metadata without creating jobs.

The control plane plans internal part IDs.  This boundary translates the
identifiers operators know into the same IDs and diagnoses the whole request
before the CLI registers a profile or schedules any work.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Iterable


_QUERY_CHUNK = 900


@dataclass(frozen=True)
class WorkflowTarget:
    video_part_id: int
    bvid: str | None
    page_index: int
    platform: str = "bilibili"
    external_video_id: str | None = None

    @property
    def work_id(self) -> str:
        from bili_asr.source_identity import display_work_id
        from bili_asr.platform_identity import ContentRef
        return display_work_id(ContentRef(self.platform, self.external_video_id or self.bvid, self.page_index))


@dataclass(frozen=True)
class WorkflowSelection:
    targets: tuple[WorkflowTarget, ...]
    excluded_gone: tuple[WorkflowTarget, ...] = ()

    @property
    def part_ids(self) -> tuple[int, ...]:
        return tuple(target.video_part_id for target in self.targets)


def _target(row: sqlite3.Row, connection: sqlite3.Connection) -> WorkflowTarget:
    if row["bvid"] is None:
        from bili_asr.storage.sources import SourceRepository
        part = SourceRepository(connection).part(int(row["video_part_id"]))
        return WorkflowTarget(part["video_part_id"], None, part["page_index"], part["platform"], part["external_video_id"])
    return WorkflowTarget(int(row["video_part_id"]), str(row["bvid"]), int(row["page_index"]))


def _ordered(targets: Iterable[WorkflowTarget]) -> tuple[WorkflowTarget, ...]:
    return tuple(sorted(targets, key=lambda target: (target.platform, target.external_video_id or target.bvid,
                                                   target.page_index, target.video_part_id)))


def resolve_workflow_selection(
    connection: sqlite3.Connection,
    *,
    part_ids: Iterable[int] | None = None,
    bvids: Iterable[str] | None = None,
    page_index: int | None = None,
) -> WorkflowSelection:
    """Resolve exactly one selector form, reporting every unresolved target.

    BVID selection without an index excludes parts marked gone, with an
    explicit exclusion list.  Exact part IDs and BVID/index pairs never
    silently exclude gone parts.  A single index applies to every BVID.
    All queries are read-only and chunked below SQLite's older variable limit.
    """
    ids = tuple(dict.fromkeys(part_ids or ()))
    videos = tuple(dict.fromkeys(bvids or ()))
    if bool(ids) == bool(videos):
        raise ValueError("select either --part-id or --bvid, without mixing them")
    if page_index is not None:
        if type(page_index) is not int or page_index < 0:
            raise ValueError("--page-index must be a non-negative stored index (0 is P1)")
        if not videos:
            raise ValueError("--page-index requires --bvid")
    if any(type(part_id) is not int or part_id < 1 for part_id in ids):
        raise ValueError("--part-id values must be positive integers")
    if any(not isinstance(bvid, str) or not bvid.strip() for bvid in videos):
        raise ValueError("--bvid values must be non-empty")

    rows: dict[int, sqlite3.Row] = {}
    known_videos: set[str] = set()
    values = ids or videos
    column = "video_part_id" if ids else "bvid"
    for offset in range(0, len(values), _QUERY_CHUNK):
        chunk = values[offset:offset + _QUERY_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        for row in connection.execute(
            f"SELECT video_part_id, bvid, page_index, processing_status FROM video_parts "
            f"WHERE {column} IN ({placeholders})",
            chunk,
        ):
            rows[int(row["video_part_id"])] = row
        if videos:
            known_videos.update(
                str(row["bvid"]) for row in connection.execute(
                    f"SELECT bvid FROM videos WHERE bvid IN ({placeholders})", chunk
                )
            )

    errors: list[str] = []
    selected: list[WorkflowTarget] = []
    excluded: list[WorkflowTarget] = []
    if ids:
        for part_id in sorted(ids):
            row = rows.get(part_id)
            if row is None:
                errors.append(f"unknown video_part_id={part_id}")
            elif row["processing_status"] == "gone":
                errors.append(f"video_part_id={part_id} ({_target(row, connection).work_id}) is gone")
            else:
                selected.append(_target(row, connection))
    else:
        by_video: dict[str, list[sqlite3.Row]] = {bvid: [] for bvid in videos}
        for row in rows.values():
            by_video[str(row["bvid"])].append(row)
        for bvid in sorted(videos):
            parts = by_video[bvid]
            if bvid not in known_videos:
                errors.append(f"unknown BVID={bvid}; run fetch-meta to collect its metadata")
                continue
            if not parts:
                errors.append(f"BVID={bvid} has no stored parts; run fetch-meta to collect its metadata")
                continue
            if page_index is not None:
                parts = [row for row in parts if int(row["page_index"]) == page_index]
                if not parts:
                    errors.append(f"BVID={bvid} page_index={page_index} has no stored part; "
                                  "run fetch-meta to refresh its metadata")
                    continue
                if parts[0]["processing_status"] == "gone":
                    errors.append(f"BVID={bvid} page_index={page_index} is gone")
                    continue
            active = [row for row in parts if row["processing_status"] != "gone"]
            if not active:
                errors.append(f"BVID={bvid} has no processable stored parts (all are gone)")
                continue
            selected.extend(_target(row, connection) for row in active)
            excluded.extend(_target(row, connection) for row in parts if row["processing_status"] == "gone")
    if errors:
        raise ValueError("unresolved workflow selection: " + "; ".join(errors))
    return WorkflowSelection(_ordered(selected), _ordered(excluded))
