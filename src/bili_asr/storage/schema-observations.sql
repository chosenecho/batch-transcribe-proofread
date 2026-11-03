CREATE TABLE source_metadata_observations (
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL COLLATE BINARY,
    operation TEXT NOT NULL CHECK (operation IN ('summary','details','parts','tags')),
    field TEXT NOT NULL CHECK (field IN ('title','pubdate','aid','pic','desc','tid','parts','tags')),
    state TEXT NOT NULL CHECK (state IN ('present','empty','missing','unavailable','denied')),
    observed_at INTEGER NOT NULL,
    last_success_at INTEGER,
    error_code TEXT,
    value_json TEXT CHECK (value_json IS NULL OR json_valid(value_json)),
    PRIMARY KEY(platform,external_id,operation,field)
);
CREATE TABLE metadata_refresh_attempts (
    id TEXT PRIMARY KEY,
    bvid TEXT NOT NULL CHECK (length(bvid) > 0),
    operation TEXT NOT NULL CHECK (operation IN ('summary','details','parts','tags')),
    state TEXT NOT NULL CHECK (state IN ('present','empty','missing','unavailable','denied')),
    error_code TEXT,
    started_at INTEGER NOT NULL,
    finished_at INTEGER NOT NULL CHECK (finished_at >= started_at),
    details_json TEXT NOT NULL CHECK (json_valid(details_json))
);
