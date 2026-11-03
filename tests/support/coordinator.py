from __future__ import annotations
import bili_asr.pipeline.models as _module_pipeline_models
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
import pytest
from bili_asr import asr as asr_mod
from bili_asr import coordinator
from bili_asr import bili_client as bc
from bili_asr.cli.main import main
from bili_asr.pipeline.attempts import AttemptLedger
from bili_asr.coordinator import RunCoordinator
from bili_asr.manifest import ManifestStore
from bili_asr.page_identity import artifact_stem, page_identity
from tests.support.audio import (
    AUDIO_BYTES,
    SPI_OK,
    STREAM_HOST,
    RouterTransport,
    nav_response,
    playurl_ok,
)
from tests.support.subtitles import SAMPLE_DOC, nav_ok, player_ok, sub_entry
import tests.support.asr_fakes as asr_fakes


def _row(identity, *, status="meta_ok", duration_s=5, title="clip"):
    return {
        "bvid": identity.bvid,
        "work_id": identity.work_id,
        "page_index": identity.page_index,
        "cid": identity.cid,
        "page_label": identity.page_label,
        "status": status,
        "title": title,
        "duration_s": duration_s,
        "pubdate": 1,
        "pubdate_str": "2026-01-02",
    }


def _patch_cli(monkeypatch, transport):
    monkeypatch.setattr(bc, "build_default_transport", lambda: transport)
    monkeypatch.setattr(bc, "default_sleeper", lambda: (lambda _s: None))
    monkeypatch.setattr('bili_asr.cli.pilot.time.sleep', lambda _s: None)


def _stub_asr(monkeypatch, calls=None):
    """A canned model set; ``calls`` collects the path of every recording the boundary opened."""

    return asr_fakes.install(monkeypatch, reads=calls)


_MID = 23191782


def _seed_part(root, bvid, page_index, cid):
    """One stored video part; returns its ``video_part_id``."""
    from bili_asr.storage import (
        MetadataRepository,
        UserRecord,
        VideoPartRecord,
        VideoRecord,
        open_database,
    )

    connection = open_database(root)
    try:
        metadata = MetadataRepository(connection)
        with metadata.transaction():
            metadata.upsert_user(
                UserRecord(mid=_MID, display_name="未明子", created_at=1, updated_at=1)
            )
            metadata.upsert_video(
                VideoRecord(
                    bvid=bvid, aid=None, mid=_MID, title="视频",
                    pubdate=1_700_000_000, created_at=2, updated_at=2,
                )
            )
            metadata.upsert_part(
                VideoPartRecord(
                    bvid=bvid, page_index=page_index, cid=cid, title="第一段",
                    duration_ms=5_000, processing_status="discovered",
                    created_at=3, updated_at=3,
                )
            )
        connection.commit()
        row = connection.execute(
            "SELECT video_part_id FROM video_parts WHERE bvid = ? AND page_index = ?",
            (bvid, page_index),
        ).fetchone()
        return int(row["video_part_id"])
    finally:
        connection.close()
