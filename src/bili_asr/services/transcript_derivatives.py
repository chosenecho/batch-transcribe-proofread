"""Explicit inexpensive derivatives from verified stored transcript evidence."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_inventory import require_no_links
from bili_asr.cues import segments_to_vtt
from bili_asr.contracts.registry import STORED_VTT_POLICY as POLICY
from bili_asr.manuscript_files import atomic_write_artifact
from bili_asr.services.transcript_projection import writer_segments
from bili_asr.storage.editorial import EditorialRepository
from bili_asr.storage.transcripts import _segment_content_sha256

def export_transcript_vtt(archive_root: Path, *, transcript_id: int, output: Path,
                          artifact_root: Path | None = None) -> dict:
    """Export an independently named VTT, without replacing a historical bundle.

    Verification keeps original segment order and exact millisecond evidence.
    Unrepresentable cues fail before output installation; no audio/model client
    is involved. Conflicting output bytes cannot be overwritten.
    """
    if type(transcript_id) is not int or transcript_id < 1:
        raise ValueError("transcript_id must be a positive integer")
    destination = Path(output).absolute()
    require_no_links(destination)
    destination = Path(os.path.abspath(destination))
    if destination.suffix.casefold() != ".vtt":
        raise ValueError("derivative output must have .vtt extension")
    session = ArchiveSession(archive_root, mode=ArchiveAccessMode.READ)
    # ArchiveSession accepts both a root directory and an explicit DB path.
    # Protect the normalized archive scope in either spelling.
    for root in (session.archive_root, artifact_root):
        if root is not None and destination.is_relative_to(Path(os.path.abspath(root))):
            raise ValueError("derivative export must be outside archive and artifact roots")
    with session:
        record = EditorialRepository(session.connection).read_source(transcript_id)
        triples = [[item.start_ms, item.end_ms, item.text] for item in record.segments]
        if _segment_content_sha256(triples) != record.content_sha256:
            raise ValueError("stored transcript digest differs from original segment evidence")
        body = segments_to_vtt(writer_segments(record.segments)).encode("utf-8")
        report = {"policy": POLICY, "transcript_id": transcript_id, "video_part_id": record.video_part_id,
                  "source_content_sha256": record.content_sha256, "source_version": record.version,
                  "segments": len(record.segments), "bytes": len(body),
                  "sha256": hashlib.sha256(body).hexdigest(), "output": str(destination),
                  "download_calls": 0, "asr_calls": 0, "ai_calls": 0,
                  "historical_bundle_changed": False}
        atomic_write_artifact(destination.parent, destination.name, body)
        return report
