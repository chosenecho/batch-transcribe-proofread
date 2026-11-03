from __future__ import annotations
import bili_asr.cli._shared as _module_cli__shared
from dataclasses import replace
import hashlib
import os
import sqlite3
import threading
import time
from bili_asr import archive as archive_module
from bili_asr import cli as cli_module
from bili_asr.cli.main import main
from bili_asr.config import ARCHIVE_DATABASE_NAME
from bili_asr.pipeline.locks import ARCHIVE_WRITER_LOCK
from bili_asr.manifest import ManifestStore
from bili_asr.persistence import file_lock
from bili_asr.storage import (
    AcquisitionRunRecord,
    MetadataRepository,
    TranscriptRepository,
    TranscriptSegmentRecord,
    open_database,
)
from tests.fixtures.metadata_records import (
    make_part_record,
    make_user_record,
    make_video_record,
)


FRESH_BVID = "BV1FRESH"


CHAIN_BVID = "BV1CHAIN"


DRIFT_BVID = "BV1DRIFT"


MARKER_BVID = "BV1MARKER"


GONE_BVID = "BV1GONE"


BARE_BVID = "BV1BARE"


PARTS = (
    (FRESH_BVID, 0, 3001, 12_000, "metadata_collected"),
    (CHAIN_BVID, 0, 5001, 7_000, "metadata_collected"),
    (DRIFT_BVID, 0, 6001, 8_000, "metadata_collected"),
    (MARKER_BVID, 0, 7001, 9_000, "metadata_collected"),
    (GONE_BVID, 0, 4001, 5_000, "gone"),
    (BARE_BVID, 0, 8001, 3_000, "metadata_collected"),
)


CAPTION_SEGMENTS = (
    TranscriptSegmentRecord(0, 2_500, "档案里的第一句台词"),
    TranscriptSegmentRecord(3_000, 6_000, "第二句记录在案的台词"),
)


PRODUCT_KEYS = ("srt_path", "txt_path", "md_path", "raw_path")


PUBDATE = 1_700_000_000


PUBDATE_STR = time.strftime("%Y-%m-%d", time.gmtime(PUBDATE))


def _entry(bvid: str, page_index: int, cid: int, duration_ms: int) -> dict:
    """The writer's entry for one stored part — the fields its frontmatter reads."""
    return {
        "bvid": bvid,
        "work_id": f"{bvid}:p{page_index}",
        "page_index": page_index,
        "cid": cid,
        "title": f"第{page_index + 1}集",
        "duration_s": max(1, duration_ms // 1000),
        "pubdate_str": PUBDATE_STR,
    }


def _seed_archive(root: str, parts=PARTS) -> None:
    """Create ``archive.db`` with one video per bvid and exactly these parts."""
    connection = open_database(root)
    try:
        metadata = MetadataRepository(connection)
        with metadata.transaction():
            metadata.upsert_user(make_user_record())
            for bvid in dict.fromkeys(bvid for bvid, _page, _cid, _ms, _status in parts):
                metadata.upsert_video(
                    make_video_record(bvid, aid=None, title="投影测试视频")
                )
            for bvid, page_index, cid, duration_ms, status in parts:
                metadata.upsert_part(
                    replace(
                        make_part_record(
                            bvid,
                            page_index=page_index,
                            cid=cid,
                            title=f"第{page_index + 1}集",
                            processing_status=status,
                        ),
                        duration_ms=duration_ms,
                    )
                )
    finally:
        connection.close()


def _part_id(connection, bvid: str, page_index: int) -> int:
    """One stored part's primary key, read from the store's own relation."""
    return int(
        connection.execute(
            "SELECT video_part_id FROM video_parts WHERE bvid = ? AND page_index = ?",
            (bvid, page_index),
        ).fetchone()["video_part_id"]
    )


def _store_caption(
    root: str,
    bvid: str,
    page_index: int,
    *,
    segments=CAPTION_SEGMENTS,
    version_tag: str = "v1",
) -> None:
    """Store one acquired ``subtitle-ai`` caption through the repository."""
    connection = open_database(root)
    try:
        repository = TranscriptRepository(connection)
        run_id = f"caption-{version_tag}-{bvid}-p{page_index}"
        repository.start_acquisition_run(
            AcquisitionRunRecord(
                run_id=run_id,
                kind="subtitle",
                selector_kind="pending",
                selector_target=None,
                requested_limit=None,
                credential_present=False,
                started_at=101,
            )
        )
        repository.record_acquired_transcript(
            run_id=run_id,
            video_part_id=_part_id(connection, bvid, page_index),
            source_kind="subtitle-ai",
            language="zh-CN",
            segments=segments,
            started_at=200,
            finished_at=300,
            created_at=400,
        )
    finally:
        connection.close()


def _chain_archive(root: str, bvid: str, page_index: int, cid: int) -> dict:
    """Publish one bundle the way the chain does and record its ``archived`` row.

    State (ii) of §5.5, built with the shipped writer rather than by the command
    under test: the four families, the marker, and the row that declares them.
    """
    entry = _entry(bvid, page_index, cid, 7_000)
    paths = archive_module.write_archive(
        root,
        entry,
        [{"start": 0.0, "end": 2.5, "text": "链上归档的台词"}],
        source="subtitle",
    )
    store = ManifestStore(root=root)
    store.load()
    store.upsert({**entry, **paths, "status": "archived"})
    return paths


def _exit_code(argv: list[str]) -> int:
    """Return the command's exit code, argparse usage errors included."""
    try:
        return main(argv)
    except SystemExit as exit_signal:
        return int(exit_signal.code)


def _publish(root: str, *extra: str) -> int:
    """Run the command the way an operator does, and return its exit code."""
    return _exit_code(["publish-transcripts", "--archive-root", root, *extra])


def _bundle_hashes(base: str, declared: dict[str, str]) -> dict[str, str]:
    """``path -> sha256`` for one declared bundle: the four families and the marker."""
    paths = [os.path.join(base, declared[key]) for key in PRODUCT_KEYS]
    paths.append(str(archive_module.bundle_marker_path(paths[0])))
    digests = {}
    for path in paths:
        with open(path, "rb") as handle:
            digests[os.path.relpath(path, base)] = hashlib.sha256(handle.read()).hexdigest()
    return digests


def _declared(root: str, work_id: str) -> dict[str, str]:
    """The four product paths the effective row of ``work_id`` declares."""
    row = ManifestStore(root=root).load()[work_id]
    return {key: row[key] for key in PRODUCT_KEYS}
