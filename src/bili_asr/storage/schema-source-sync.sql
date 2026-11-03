-- Target-only compatibility synchronization: existing Bilibili writers keep
-- their real provider tables while every processing part has a neutral parent.
CREATE TRIGGER source_creator_from_bili_insert AFTER INSERT ON bilibili_users BEGIN
    INSERT INTO source_creators(platform,external_id,display_name,created_at,updated_at)
    VALUES ('bilibili',CAST(NEW.mid AS TEXT),NEW.display_name,NEW.created_at,NEW.updated_at);
END;
CREATE TRIGGER source_creator_from_bili_update AFTER UPDATE ON bilibili_users BEGIN
    UPDATE source_creators SET display_name=NEW.display_name,updated_at=NEW.updated_at
    WHERE platform='bilibili' AND external_id=CAST(NEW.mid AS TEXT);
END;
CREATE TRIGGER source_video_from_bili_insert AFTER INSERT ON videos BEGIN
    INSERT INTO source_videos(platform,external_id,creator_id,title,published_at,canonical_url,observed_at,created_at,updated_at)
    SELECT 'bilibili',NEW.bvid,creator_id,NEW.title,NEW.pubdate,
           'https://www.bilibili.com/video/' || NEW.bvid || '/?p=1',NEW.updated_at,NEW.created_at,NEW.updated_at
    FROM source_creators WHERE platform='bilibili' AND external_id=CAST(NEW.mid AS TEXT);
END;
CREATE TRIGGER source_video_from_bili_update AFTER UPDATE ON videos BEGIN
    UPDATE source_videos SET title=NEW.title,published_at=NEW.pubdate,updated_at=NEW.updated_at,observed_at=NEW.updated_at,
        creator_id=(SELECT creator_id FROM source_creators WHERE platform='bilibili' AND external_id=CAST(NEW.mid AS TEXT))
    WHERE platform='bilibili' AND external_id=NEW.bvid;
END;
CREATE TRIGGER source_part_from_bili_insert AFTER INSERT ON video_parts WHEN NEW.bvid IS NOT NULL BEGIN
    UPDATE video_parts SET source_video_id=(SELECT source_video_id FROM source_videos
        WHERE platform='bilibili' AND external_id=NEW.bvid) WHERE video_part_id=NEW.video_part_id;
END;
CREATE TRIGGER source_part_identity_insert BEFORE INSERT ON video_parts WHEN NEW.source_video_id IS NOT NULL BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM source_videos v WHERE v.source_video_id=NEW.source_video_id
        AND ((v.platform='bilibili' AND v.external_id=NEW.bvid AND NEW.cid>0)
          OR (v.platform='youtube' AND NEW.bvid IS NULL AND NEW.cid IS NULL AND NEW.page_index=0)))
        THEN RAISE(ABORT, 'source part identity mismatch') END;
END;
CREATE TRIGGER source_part_identity_update BEFORE UPDATE OF source_video_id,bvid,cid,page_index ON video_parts BEGIN
    SELECT CASE WHEN NEW.source_video_id IS NULL OR NOT EXISTS (SELECT 1 FROM source_videos v
        WHERE v.source_video_id=NEW.source_video_id
        AND ((v.platform='bilibili' AND v.external_id=NEW.bvid AND NEW.cid>0)
          OR (v.platform='youtube' AND NEW.bvid IS NULL AND NEW.cid IS NULL AND NEW.page_index=0)))
        THEN RAISE(ABORT, 'source part identity mismatch') END;
END;
CREATE TRIGGER source_video_identity_immutable BEFORE UPDATE OF platform,external_id ON source_videos BEGIN
    SELECT RAISE(ABORT, 'source video identity is immutable');
END;
CREATE TRIGGER source_creator_identity_immutable BEFORE UPDATE OF platform,external_id ON source_creators BEGIN
    SELECT RAISE(ABORT, 'source creator identity is immutable');
END;
CREATE VIEW v_source_parts AS SELECT p.video_part_id,p.source_video_id,p.bvid,p.cid,p.page_index,
    p.title,p.duration_ms,p.processing_status,v.platform,v.external_id AS external_video_id,
    v.title AS video_title,v.published_at AS pubdate,v.original_language,v.canonical_url,v.observed_at,
    c.external_id AS creator_external_id,c.display_name AS creator_name
    FROM video_parts p JOIN source_videos v USING(source_video_id)
    LEFT JOIN source_creators c USING(creator_id);

-- Bilibili exhaustion retains its historical credential evidence. YouTube
-- admission uses a separately versioned successful public inventory policy;
-- no Bilibili absence/credential flag is fabricated for another provider.
CREATE VIEW v_missing_audio AS
SELECT * FROM v_bilibili_missing_audio
UNION ALL
SELECT p.video_part_id, 'youtube:' || p.external_video_id || ':p0' AS work_id,
       NULL AS bvid,p.page_index,NULL AS cid,p.title AS part_title,p.duration_ms,
       p.video_title,p.pubdate,'no-subtitle' AS newest_outcome,NULL AS newest_error_code
FROM v_source_parts p
JOIN source_caption_observations o ON o.observation_id=(
    SELECT observation_id FROM source_caption_observations WHERE video_part_id=p.video_part_id
    ORDER BY observed_at DESC,observation_id DESC LIMIT 1)
WHERE p.platform='youtube' AND p.processing_status<>'gone'
  AND o.state='no-tracks' AND o.policy_version='youtube-public-v1'
  AND json_extract(o.provenance_json,'$.verified')=1
  AND NOT EXISTS(SELECT 1 FROM transcripts t WHERE t.video_part_id=p.video_part_id)
  AND NOT EXISTS(SELECT 1 FROM part_audio_objects a WHERE a.video_part_id=p.video_part_id);

CREATE VIEW v_part_pipeline AS
SELECT legacy.* FROM v_bilibili_part_pipeline legacy
JOIN video_parts p USING(video_part_id) WHERE p.bvid IS NOT NULL
UNION ALL
SELECT p.video_part_id,'youtube:' || p.external_video_id || ':p0',NULL,p.page_index,p.processing_status,
       CASE WHEN EXISTS(SELECT 1 FROM transcripts t WHERE t.video_part_id=p.video_part_id) THEN 'transcribed'
            WHEN EXISTS(SELECT 1 FROM part_audio_objects a WHERE a.video_part_id=p.video_part_id) THEN 'audio_ok'
            WHEN EXISTS(SELECT 1 FROM v_missing_audio a WHERE a.video_part_id=p.video_part_id) THEN 'audio_pending'
            WHEN (SELECT state FROM source_caption_observations WHERE video_part_id=p.video_part_id
                  ORDER BY observed_at DESC,observation_id DESC LIMIT 1)='no-tracks' THEN 'no_subtitle'
            ELSE 'discovered' END
FROM v_source_parts p WHERE p.platform='youtube';
