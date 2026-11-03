"""Neutral source facts and the Bilibili compatibility resolver."""
from __future__ import annotations

import sqlite3
import time

from bili_asr.platform_identity import ContentRef
from bili_asr.source_video import SourceVideoMetadata
from bili_asr.source_identity import source_url, display_work_id, artifact_stem
from bili_asr.storage.archive_contracts import runtime_contract, UNIVERSAL_V2, require_universal_contract


class SourceRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def part(self, part_id: int) -> dict:
        if type(part_id) is not int or part_id < 1:
            raise ValueError("part identity must be a positive integer")
        if runtime_contract(self.connection) == UNIVERSAL_V2:
            row = self.connection.execute("SELECT * FROM v_source_parts WHERE video_part_id=?", (part_id,)).fetchone()
        else:
            row = self.connection.execute(
                "SELECT p.*, 'bilibili' AS platform,p.bvid AS external_video_id,v.title AS video_title,"
                "v.pubdate,v.updated_at AS observed_at,CAST(v.mid AS TEXT) AS creator_external_id,"
                "u.display_name AS creator_name,NULL AS original_language FROM video_parts p "
                "JOIN videos v ON v.bvid=p.bvid JOIN bilibili_users u ON u.mid=v.mid WHERE p.video_part_id=?",
                (part_id,)).fetchone()
        if row is None:
            raise ValueError("unknown source part")
        result = dict(row)
        ref = ContentRef(result["platform"], result["external_video_id"], int(result["page_index"]))
        result.update(content_ref=ref, canonical_url=source_url(ref), work_id=display_work_id(ref),
                      artifact_stem=artifact_stem(ref))
        return result

    def upsert_video(self, metadata: SourceVideoMetadata) -> int:
        """Register one YouTube processing unit in a caller-owned transaction."""
        require_universal_contract(self.connection)
        from bili_asr.storage.source_observations import require_observations, ObservationRepository
        if metadata.observations and require_observations(self.connection):
            ObservationRepository(self.connection).append(metadata.observations)
        if metadata.ref.platform != "youtube" or metadata.ref.part_index != 0:
            raise ValueError("Bilibili metadata must use its existing provider repository")
        from bili_asr.storage.source_projection import store_youtube_fields
        store_youtube_fields(self.connection, metadata.observations)
        now = int(time.time()) if metadata.observed_at is None else metadata.observed_at
        creator_id = None
        if metadata.creator_external_id is not None:
            self.connection.execute(
                "INSERT INTO source_creators(platform,external_id,display_name,created_at,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(platform,external_id) DO UPDATE SET display_name=COALESCE(excluded.display_name,source_creators.display_name),updated_at=excluded.updated_at",
                (metadata.ref.platform, metadata.creator_external_id, metadata.creator_name, now, now))
            creator_id = self.connection.execute("SELECT creator_id FROM source_creators WHERE platform=? AND external_id=?",
                                                (metadata.ref.platform, metadata.creator_external_id)).fetchone()[0]
        self.connection.execute(
            "INSERT INTO source_videos(platform,external_id,creator_id,title,published_at,original_language,canonical_url,observed_at,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(platform,external_id) DO UPDATE SET "
            "creator_id=COALESCE(excluded.creator_id,source_videos.creator_id),title=excluded.title,"
            "published_at=COALESCE(excluded.published_at,source_videos.published_at),"
            "original_language=COALESCE(excluded.original_language,source_videos.original_language),observed_at=excluded.observed_at,updated_at=excluded.updated_at",
            (metadata.ref.platform, metadata.ref.external_video_id, creator_id, metadata.title, metadata.published_at,
             metadata.original_language, source_url(metadata.ref), now, now, now))
        video_id = self.connection.execute("SELECT source_video_id FROM source_videos WHERE platform=? AND external_id=?",
                                           (metadata.ref.platform, metadata.ref.external_video_id)).fetchone()[0]
        self.connection.execute(
            "INSERT INTO video_parts(bvid,cid,page_index,title,duration_ms,processing_status,created_at,updated_at,source_video_id) "
            "VALUES (NULL,NULL,0,?,?,'metadata_collected',?,?,?) ON CONFLICT(source_video_id,page_index) "
            "DO UPDATE SET title=excluded.title,duration_ms=excluded.duration_ms,processing_status='metadata_collected',updated_at=excluded.updated_at",
            (metadata.title, metadata.duration_ms, now, now, video_id))
        return int(self.connection.execute("SELECT video_part_id FROM video_parts WHERE source_video_id=? AND page_index=0", (video_id,)).fetchone()[0])


def acquisition_selector(part: dict) -> tuple[str, str]:
    ref = part["content_ref"]
    return ("bvid", ref.external_video_id) if ref.platform == "bilibili" else ("source-ref", display_work_id(ref))
