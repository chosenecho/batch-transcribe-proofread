"""Read-only exact-duplicate inventory for archive planning.

The first deduplication step is intentionally observational.  It reports
content that is already shared by multiple parts without changing provenance
or selecting a canonical record.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def _scalar(connection: sqlite3.Connection, query: str) -> int:
    row = connection.execute(query).fetchone()
    return 0 if row is None else int(row[0])


def build_report(connection: sqlite3.Connection, *, limit: int = 20) -> dict[str, Any]:
    """Return exact audio and transcript reuse statistics.

    ``limit`` only caps the example groups returned in the report.  Aggregate
    counts always describe the complete database.  The connection is never
    mutated, and every reported reference keeps its original part identity.
    """

    if limit < 1:
        raise ValueError("limit must be positive")

    audio_objects = _scalar(connection, "SELECT COUNT(*) FROM audio_objects")
    audio_links = _scalar(connection, "SELECT COUNT(*) FROM part_audio_objects")
    reused_audio_objects = _scalar(
        connection,
        """
        SELECT COUNT(*) FROM (
            SELECT audio_id
            FROM part_audio_objects
            GROUP BY audio_id
            HAVING COUNT(DISTINCT video_part_id) > 1
        )
        """,
    )
    reused_audio_parts = _scalar(
        connection,
        """
        SELECT COALESCE(SUM(part_count - 1), 0) FROM (
            SELECT audio_id, COUNT(DISTINCT video_part_id) AS part_count
            FROM part_audio_objects
            GROUP BY audio_id
            HAVING part_count > 1
        )
        """,
    )
    audio_groups = [
        {
            "audio_id": int(row["audio_id"]),
            "sha256": str(row["sha256"]),
            "parts": int(row["parts"]),
        }
        for row in connection.execute(
            """
            SELECT ao.audio_id, ao.sha256,
                   COUNT(DISTINCT pao.video_part_id) AS parts
            FROM audio_objects AS ao
            JOIN part_audio_objects AS pao ON pao.audio_id = ao.audio_id
            GROUP BY ao.audio_id, ao.sha256
            HAVING parts > 1
            ORDER BY parts DESC, ao.audio_id
            LIMIT ?
            """,
            (limit,),
        )
    ]

    transcript_total = _scalar(connection, "SELECT COUNT(*) FROM transcripts")
    duplicate_transcript_groups = _scalar(
        connection,
        """
        SELECT COUNT(*) FROM (
            SELECT content_sha256, source_kind, language
            FROM transcripts
            GROUP BY content_sha256, source_kind, language
            HAVING COUNT(DISTINCT video_part_id) > 1
        )
        """,
    )
    duplicate_transcript_rows = _scalar(
        connection,
        """
        SELECT COALESCE(SUM(row_count), 0) FROM (
            SELECT COUNT(*) AS row_count
            FROM transcripts
            GROUP BY content_sha256, source_kind, language
            HAVING COUNT(DISTINCT video_part_id) > 1
        )
        """,
    )
    transcript_groups = [
        {
            "content_sha256": str(row["content_sha256"]),
            "source_kind": str(row["source_kind"]),
            "language": str(row["language"]),
            "parts": int(row["parts"]),
            "rows": int(row["rows"]),
        }
        for row in connection.execute(
            """
            SELECT content_sha256, source_kind, language,
                   COUNT(DISTINCT video_part_id) AS parts,
                   COUNT(*) AS rows
            FROM transcripts
            GROUP BY content_sha256, source_kind, language
            HAVING parts > 1
            ORDER BY parts DESC, rows DESC, content_sha256
            LIMIT ?
            """,
            (limit,),
        )
    ]

    return {
        "audio": {
            "objects": audio_objects,
            "links": audio_links,
            "reused_objects": reused_audio_objects,
            "reused_parts": reused_audio_parts,
            "groups": audio_groups,
        },
        "transcripts": {
            "total": transcript_total,
            "cross_part_groups": duplicate_transcript_groups,
            "cross_part_rows": duplicate_transcript_rows,
            "groups": transcript_groups,
        },
    }


def format_text(report: dict[str, Any]) -> str:
    """Render a compact operator-facing report."""

    audio = report["audio"]
    transcripts = report["transcripts"]
    lines = [
        "audio:",
        f"  objects={audio['objects']} links={audio['links']} "
        f"reused_objects={audio['reused_objects']} reused_parts={audio['reused_parts']}",
        "transcripts:",
        f"  total={transcripts['total']} cross_part_groups={transcripts['cross_part_groups']} "
        f"cross_part_rows={transcripts['cross_part_rows']}",
    ]
    for item in audio["groups"]:
        lines.append(
            f"audio-reuse: sha256={item['sha256']} parts={item['parts']} "
            f"audio_id={item['audio_id']}"
        )
    for item in transcripts["groups"]:
        lines.append(
            f"transcript-reuse: source={item['source_kind']} language={item['language']} "
            f"sha256={item['content_sha256']} parts={item['parts']} rows={item['rows']}"
        )
    return "\n".join(lines)
