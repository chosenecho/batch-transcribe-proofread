from __future__ import annotations
import hashlib
import os
import pytest
from bili_asr import bili_client as bc
from bili_asr.cli.main import main
from bili_asr.manifest import ManifestStore
from bili_asr.page_identity import page_identity
from bili_asr.storage import (
    AcquisitionRunRecord,
    MediaQueueRepository,
    MetadataRepository,
    TranscriptRepository,
    TranscriptSegmentRecord,
    UserRecord,
    VideoPartRecord,
    VideoRecord,
    open_database,
)
from tests.support.audio import AUDIO_BYTES, RouterTransport
from tests.support.cli_asr import _audio_transport, _patch_cli, _stub_runner_model


_MID = 23191782


def _seed_store(root):
    """One captionless part (audio queue) and one audio-backed part.

    Returns ``(audio_part, transcript_part)`` page identities; the audio part
    holds no transcript and no audio evidence (``v_missing_audio``), the
    transcript part holds archived audio and no transcript
    (``v_missing_transcript``).
    """
    connection = open_database(root)
    audio_id = page_identity("BVqueueA", 0, 9001, "p0")
    transcript_id = page_identity("BVqueueT", 0, 9002, "p0")
    try:
        metadata = MetadataRepository(connection)
        transcripts = TranscriptRepository(connection)
        with metadata.transaction():
            metadata.upsert_user(
                UserRecord(mid=_MID, display_name="未明子", created_at=1, updated_at=1)
            )
            for identity, title in (
                (audio_id, "captionless"),
                (transcript_id, "audio-backed"),
            ):
                metadata.upsert_video(
                    VideoRecord(
                        bvid=identity.bvid,
                        aid=None,
                        mid=_MID,
                        title=title,
                        pubdate=1_000,
                        created_at=2,
                        updated_at=2,
                    )
                )
                metadata.upsert_part(
                    VideoPartRecord(
                        bvid=identity.bvid,
                        page_index=identity.page_index,
                        cid=identity.cid,
                        title=f"{identity.bvid} 段",
                        duration_ms=5_000,
                        processing_status="discovered",
                        created_at=3,
                        updated_at=3,
                    )
                )
        # The audio-backed part needs positive audio evidence: the object row
        # and its part link (§4d: part_audio_objects is the sole probe).
        digest = hashlib.sha256(AUDIO_BYTES).hexdigest()
        connection.execute(
            "INSERT INTO audio_objects("
            "  audio_id, sha256, byte_size, format, duration_ms, storage_key, created_at"
            ") VALUES (1, ?, ?, 'm4a', 5000, ?, 4)",
            (digest, len(AUDIO_BYTES), f"audio/BVqueueT.p0.m4a"),
        )
        connection.execute(
            "INSERT INTO part_audio_objects("
            "  video_part_id, audio_id, acquired_at, acquisition_source"
            ") VALUES ("
            "  (SELECT video_part_id FROM video_parts WHERE bvid=? AND page_index=0),"
            "  1, 4, 'download')",
            (transcript_id.bvid,),
        )
        # The captionless part's subtitle route is exhausted (no-subtitle), so
        # it enters the audio queue rather than remaining subtitle-pending.
        # Exhaustion is attested, not inferred from one look: an empty inventory
        # with no error code is an *indefinite* negative, so it takes two
        # distinct runs to confirm — hence the two runs below.
        for run_id, started in (("run-sub-queuea", 10), ("run-sub-queuea-2", 13)):
            transcripts.start_acquisition_run(
                AcquisitionRunRecord(
                    run_id=run_id,
                    kind="subtitle",
                    selector_kind="pending",
                    selector_target=None,
                    requested_limit=None,
                    credential_present=True,
                    started_at=started,
                )
            )
            transcripts.record_subtitle_attempt(
                run_id=run_id,
                video_part_id=connection.execute(
                    "SELECT video_part_id FROM video_parts WHERE bvid=? AND page_index=0",
                    (audio_id.bvid,),
                ).fetchone()[0],
                outcome="no-subtitle",
                error_code=None,
                started_at=started + 1,
                finished_at=started + 2,
                credential_verified=True,
            )
        connection.commit()
    finally:
        connection.close()
    return audio_id, transcript_id


def _write_audio_file(root, identity):
    audio_dir = os.path.join(root, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    path = os.path.join(audio_dir, f"{identity.bvid}.p{identity.page_index}.m4a")
    with open(path, "wb") as fh:
        fh.write(AUDIO_BYTES)
