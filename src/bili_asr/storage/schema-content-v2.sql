-- Only an explicit new-target bootstrap or offline migration installs this contract.
-- The Bilibili tables remain real legacy facts, not aliases for another provider.
CREATE TABLE archive_contract (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    contract TEXT NOT NULL CHECK (contract = 'universal-v2')
);
INSERT INTO archive_contract VALUES (1, 'universal-v2');

CREATE TABLE source_creators (
    creator_id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL CHECK (platform IN ('bilibili', 'youtube')),
    external_id TEXT NOT NULL COLLATE BINARY CHECK (length(external_id) > 0),
    display_name TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (platform, external_id)
);
CREATE TABLE source_videos (
    source_video_id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL CHECK (platform IN ('bilibili', 'youtube')),
    external_id TEXT NOT NULL COLLATE BINARY CHECK (length(external_id) > 0),
    creator_id INTEGER REFERENCES source_creators(creator_id),
    title TEXT NOT NULL,
    published_at INTEGER,
    original_language TEXT,
    canonical_url TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (platform, external_id)
);
CREATE TABLE editorial_input_versions (
    input_id TEXT PRIMARY KEY REFERENCES editorial_inputs(input_id),
    version INTEGER NOT NULL CHECK (version IN (1, 2))
);
CREATE TABLE publication_content_versions (
    edition_id TEXT PRIMARY KEY REFERENCES publication_editions(edition_id),
    version INTEGER NOT NULL CHECK (version IN (1, 2))
);
CREATE TRIGGER immutable_input_version_update BEFORE UPDATE ON editorial_input_versions BEGIN
    SELECT RAISE(ABORT, 'input contract version is immutable');
END;
CREATE TRIGGER immutable_input_version_delete BEFORE DELETE ON editorial_input_versions BEGIN
    SELECT RAISE(ABORT, 'input contract version is immutable');
END;
CREATE TRIGGER immutable_content_version_update BEFORE UPDATE ON publication_content_versions BEGIN
    SELECT RAISE(ABORT, 'content contract version is immutable');
END;
CREATE TRIGGER immutable_content_version_delete BEFORE DELETE ON publication_content_versions BEGIN
    SELECT RAISE(ABORT, 'content contract version is immutable');
END;
CREATE TABLE source_caption_observations (
    observation_id INTEGER PRIMARY KEY,
    video_part_id INTEGER NOT NULL REFERENCES video_parts(video_part_id),
    run_id TEXT NOT NULL REFERENCES acquisition_runs(run_id),
    policy_version TEXT NOT NULL CHECK (policy_version = 'youtube-public-v1'),
    state TEXT NOT NULL CHECK (state IN ('tracks', 'no-tracks', 'unavailable')),
    access_context TEXT NOT NULL CHECK (access_context IN ('anonymous', 'credentialed')),
    error_code TEXT,
    observed_at INTEGER NOT NULL,
    provenance_json TEXT NOT NULL CHECK (json_valid(provenance_json))
);
CREATE INDEX ix_source_caption_observation_part ON source_caption_observations(video_part_id, observation_id DESC);
CREATE TABLE migration_records (
    migration_id TEXT PRIMARY KEY,
    source_fingerprint TEXT NOT NULL UNIQUE,
    source_contract TEXT NOT NULL,
    target_contract TEXT NOT NULL CHECK (target_contract = 'universal-v2'),
    report_json TEXT NOT NULL CHECK (json_valid(report_json)),
    completed_at INTEGER NOT NULL
);
CREATE TRIGGER immutable_migration_record_update BEFORE UPDATE ON migration_records BEGIN
    SELECT RAISE(ABORT, 'migration record is immutable');
END;
CREATE TRIGGER immutable_migration_record_delete BEFORE DELETE ON migration_records BEGIN
    SELECT RAISE(ABORT, 'migration record is immutable');
END;
