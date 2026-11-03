PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS bilibili_users (
    mid INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS videos (
    bvid TEXT PRIMARY KEY,
    aid INTEGER UNIQUE,
    mid INTEGER NOT NULL,
    title TEXT NOT NULL,
    pubdate INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY (mid) REFERENCES bilibili_users(mid) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS video_parts (
    video_part_id INTEGER PRIMARY KEY,
    bvid TEXT NOT NULL,
    page_index INTEGER NOT NULL CHECK (page_index >= 0),
    cid INTEGER NOT NULL CHECK (cid > 0),
    title TEXT NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),
    processing_status TEXT NOT NULL CHECK (
        processing_status IN ('discovered', 'metadata_collected', 'gone')
    ),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (bvid, page_index),
    FOREIGN KEY (bvid) REFERENCES videos(bvid) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS video_tags (
    bvid TEXT NOT NULL,
    tag_id INTEGER NOT NULL CHECK (tag_id > 0),
    tag_name TEXT NOT NULL,
    tag_type TEXT NOT NULL,
    PRIMARY KEY (bvid, tag_id),
    FOREIGN KEY (bvid) REFERENCES videos(bvid) ON DELETE RESTRICT
);

-- Optional tag acquisition evidence, separate from the last successful set.
CREATE TABLE IF NOT EXISTS video_tag_observations (
    bvid TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('success_nonempty', 'success_empty', 'unavailable')),
    observed_at INTEGER NOT NULL CHECK (observed_at >= 0),
    error_code TEXT CHECK (error_code IS NULL OR error_code IN (
        'auth_error', 'rate_limited', 'not_found', 'transport_error', 'response_error', 'shape_error', 'unavailable'
    )),
    run_id TEXT,
    FOREIGN KEY (bvid) REFERENCES videos(bvid) ON DELETE RESTRICT
);

-- The video's current category and cover, one row per video, refreshed by the
-- next collection that observed at least one of the three (``observed_at``
-- records the last *successful* collection, never an attempt).
-- ``"desc"`` is quoted because ``desc`` is a SQL keyword; the column keeps
-- upstream's own field name, and ``PRAGMA table_info`` reports it unquoted.
-- Keep optional detail fields separate from the core video identity. Schema
-- initialization validates the current table contract before any DDL; an
-- incompatible archive must be deleted and recollected, never altered in place.
CREATE TABLE IF NOT EXISTS video_details (
    bvid TEXT PRIMARY KEY,
    pic TEXT,
    "desc" TEXT,
    tid INTEGER CHECK (tid IS NULL OR tid > 0),
    observed_at INTEGER NOT NULL,
    FOREIGN KEY (bvid) REFERENCES videos(bvid) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id TEXT PRIMARY KEY,
    mid INTEGER NOT NULL,
    source_package TEXT NOT NULL CHECK (source_package = 'bilibili-api-python'),
    source_version TEXT NOT NULL,
    requested_start_page INTEGER NOT NULL CHECK (requested_start_page >= 1),
    requested_page_limit INTEGER CHECK (
        requested_page_limit IS NULL OR requested_page_limit > 0
    ),
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    outcome TEXT NOT NULL CHECK (
        outcome IN ('running', 'complete', 'limited', 'risk_interrupted', 'failed')
    ),
    FOREIGN KEY (mid) REFERENCES bilibili_users(mid) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS ingestion_cursors (
    mid INTEGER PRIMARY KEY,
    next_page INTEGER NOT NULL CHECK (next_page >= 1),
    observed_total INTEGER CHECK (observed_total IS NULL OR observed_total >= 0),
    state TEXT NOT NULL CHECK (
        state IN ('ready', 'complete', 'limited', 'risk_interrupted')
    ),
    last_error_code TEXT CHECK (last_error_code IS NULL OR length(last_error_code) <= 64),
    updated_at INTEGER NOT NULL,
    FOREIGN KEY (mid) REFERENCES bilibili_users(mid) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS ingestion_pages (
    run_id TEXT NOT NULL,
    page_number INTEGER NOT NULL CHECK (page_number >= 1),
    outcome TEXT NOT NULL CHECK (
        outcome IN ('ok', 'empty', 'risk_interrupted', 'failed')
    ),
    error_code TEXT CHECK (error_code IS NULL OR length(error_code) <= 64),
    started_at INTEGER NOT NULL,
    finished_at INTEGER NOT NULL,
    PRIMARY KEY (run_id, page_number),
    FOREIGN KEY (run_id) REFERENCES ingestion_runs(run_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS ingestion_discoveries (
    run_id TEXT NOT NULL,
    page_number INTEGER NOT NULL CHECK (page_number >= 1),
    bvid TEXT NOT NULL,
    source_position INTEGER CHECK (source_position IS NULL OR source_position >= 0),
    discovered_at INTEGER NOT NULL,
    PRIMARY KEY (run_id, page_number, bvid),
    FOREIGN KEY (run_id) REFERENCES ingestion_runs(run_id) ON DELETE RESTRICT,
    FOREIGN KEY (bvid) REFERENCES videos(bvid) ON DELETE RESTRICT
);

-- Reserved structured media boundary. These tables are intentionally empty in
-- the metadata plan; media bytes remain external objects referenced by key.
CREATE TABLE IF NOT EXISTS audio_objects (
    audio_id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    format TEXT NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    storage_key TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS part_audio_objects (
    video_part_id INTEGER NOT NULL,
    audio_id INTEGER NOT NULL,
    acquired_at INTEGER NOT NULL,
    acquisition_source TEXT NOT NULL,
    PRIMARY KEY (video_part_id, audio_id),
    FOREIGN KEY (video_part_id) REFERENCES video_parts(video_part_id) ON DELETE RESTRICT,
    FOREIGN KEY (audio_id) REFERENCES audio_objects(audio_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS asr_models (
    model_id INTEGER PRIMARY KEY,
    model_name TEXT NOT NULL,
    revision TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (model_name, revision)
);

-- Queue gap reads join parts to their video and then impose the stable
-- publication order.  Keep that order index explicit so SQLite does not
-- repeatedly sort the full video set as the archive grows.
CREATE INDEX IF NOT EXISTS ix_videos_pubdate_bvid
    ON videos(pubdate DESC, bvid ASC);

-- The transcript and acquisition process-record tables live in their own
-- resource (``schema-transcripts.sql``). Initialization applies all four schema
-- resources to a fresh database and rejects incompatible existing tables before
-- writing. Rebuilding discards the old database data and requires recollection.

CREATE VIEW IF NOT EXISTS v_video_parts AS
SELECT
    vp.video_part_id,
    vp.bvid || ':p' || vp.page_index AS work_id,
    u.display_name AS user_name,
    v.title AS video_title,
    vp.page_index,
    vp.cid,
    vp.title AS part_title,
    vp.duration_ms,
    vp.processing_status,
    vp.created_at,
    vp.updated_at
FROM video_parts AS vp
JOIN videos AS v ON vp.bvid = v.bvid
JOIN bilibili_users AS u ON v.mid = u.mid;

CREATE VIEW IF NOT EXISTS v_ingestion_run_stats AS
SELECT
    ir.run_id,
    ir.mid,
    ir.started_at,
    ir.finished_at,
    ir.outcome,
    COUNT(DISTINCT ip.page_number) AS page_count,
    COUNT(DISTINCT id.bvid) AS video_count
FROM ingestion_runs AS ir
LEFT JOIN ingestion_pages AS ip ON ir.run_id = ip.run_id
LEFT JOIN ingestion_discoveries AS id ON ir.run_id = id.run_id
GROUP BY ir.run_id;

CREATE VIEW IF NOT EXISTS v_pending_metadata AS
SELECT
    vp.video_part_id,
    vp.bvid || ':p' || vp.page_index AS work_id,
    vp.bvid,
    vp.page_index,
    vp.processing_status
FROM video_parts AS vp
WHERE vp.processing_status = 'discovered';
