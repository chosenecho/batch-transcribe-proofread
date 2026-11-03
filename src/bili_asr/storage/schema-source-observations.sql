CREATE TABLE platform_observation_contract (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL CHECK (version = 1)
);
INSERT INTO platform_observation_contract VALUES (1, 1);
CREATE TABLE platform_observations (
    observation_id TEXT PRIMARY KEY CHECK (length(observation_id) = 32),
    platform TEXT NOT NULL CHECK (platform IN ('bilibili','youtube')),
    operation TEXT NOT NULL,
    external_video_id TEXT,
    creator_id TEXT,
    observed_at INTEGER NOT NULL CHECK (observed_at >= 0),
    envelope_json TEXT NOT NULL CHECK (json_valid(envelope_json)),
    envelope_sha256 TEXT NOT NULL CHECK (length(envelope_sha256) = 64),
    payload_json TEXT CHECK (payload_json IS NULL OR json_valid(payload_json))
);
CREATE INDEX idx_platform_observation_video ON platform_observations(platform,external_video_id,observed_at);
CREATE INDEX idx_platform_observation_creator ON platform_observations(platform,creator_id,observed_at);
CREATE TRIGGER platform_observation_contract_no_update BEFORE UPDATE ON platform_observation_contract
BEGIN SELECT RAISE(ABORT, 'immutable observation contract'); END;
CREATE TRIGGER platform_observation_contract_no_delete BEFORE DELETE ON platform_observation_contract
BEGIN SELECT RAISE(ABORT, 'immutable observation contract'); END;
CREATE TRIGGER platform_observation_contract_no_replace BEFORE INSERT ON platform_observation_contract
WHEN EXISTS (SELECT 1 FROM platform_observation_contract WHERE singleton = NEW.singleton)
BEGIN SELECT RAISE(ABORT, 'immutable observation contract'); END;
CREATE TRIGGER platform_observations_no_update BEFORE UPDATE ON platform_observations
BEGIN SELECT RAISE(ABORT, 'immutable platform observation'); END;
CREATE TRIGGER platform_observations_no_delete BEFORE DELETE ON platform_observations
BEGIN SELECT RAISE(ABORT, 'immutable platform observation'); END;
CREATE TRIGGER platform_observations_no_replace BEFORE INSERT ON platform_observations
WHEN EXISTS (SELECT 1 FROM platform_observations WHERE observation_id = NEW.observation_id)
BEGIN SELECT RAISE(ABORT, 'immutable platform observation'); END;
