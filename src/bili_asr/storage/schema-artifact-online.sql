-- Explicit optional extension; installed only into an unpublished offline stage.
CREATE TABLE artifact_online_contract (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL CHECK (version = 1)
);
INSERT INTO artifact_online_contract VALUES (1, 1);
CREATE TABLE artifact_reservations (
    reservation_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('local', 'local-artifacts')),
    byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
    owner TEXT NOT NULL,
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    released_at INTEGER CHECK (released_at IS NULL OR released_at >= created_at)
);
CREATE TABLE artifact_policy_runs (
    run_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL REFERENCES artifact_policies(policy_id) ON DELETE RESTRICT,
    policy_sha256 TEXT NOT NULL CHECK (length(policy_sha256) = 64 AND policy_sha256 NOT GLOB '*[^0-9a-f]*'),
    policy_json TEXT NOT NULL CHECK (json_valid(policy_json)),
    plan_json TEXT CHECK (plan_json IS NULL OR json_valid(plan_json)),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    state TEXT NOT NULL CHECK (state IN ('planned', 'running', 'complete', 'blocked', 'failed')),
    retry_after INTEGER NOT NULL CHECK (retry_after >= 0),
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= 0)
);
CREATE TABLE artifact_input_states (
    job_id TEXT PRIMARY KEY REFERENCES workflow_jobs(job_id) ON DELETE RESTRICT,
    object_id TEXT REFERENCES artifact_objects(object_id) ON DELETE RESTRICT,
    readiness TEXT NOT NULL CHECK (readiness IN ('ready', 'blocked')),
    reason TEXT NOT NULL,
    observed_at INTEGER NOT NULL CHECK (observed_at >= 0),
    retry_after INTEGER NOT NULL CHECK (retry_after >= 0)
);
