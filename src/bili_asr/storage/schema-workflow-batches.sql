CREATE TABLE workflow_batch_contract (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    version INTEGER NOT NULL CHECK(version=1)
);
CREATE TABLE workflow_batches (
    batch_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN ('active','paused')),
    desired_workers INTEGER NOT NULL CHECK(desired_workers BETWEEN 1 AND 64),
    frozen_config_json TEXT NOT NULL CHECK(json_valid(frozen_config_json)),
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE workflow_batch_members (
    job_id TEXT PRIMARY KEY REFERENCES workflow_jobs(job_id) ON DELETE RESTRICT,
    batch_id TEXT NOT NULL REFERENCES workflow_batches(batch_id) ON DELETE RESTRICT
);
CREATE INDEX workflow_batch_members_batch ON workflow_batch_members(batch_id,job_id);
CREATE TABLE workflow_batch_events (
    event_id INTEGER PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES workflow_batches(batch_id) ON DELETE RESTRICT,
    event TEXT NOT NULL,
    actor TEXT NOT NULL CHECK(length(trim(actor))>0),
    detail_json TEXT NOT NULL CHECK(json_valid(detail_json)),
    created_at INTEGER NOT NULL
);
CREATE TRIGGER immutable_workflow_batch_events_update BEFORE UPDATE ON workflow_batch_events BEGIN
    SELECT RAISE(ABORT, 'batch events are append-only');
END;
CREATE TRIGGER immutable_workflow_batch_events_delete BEFORE DELETE ON workflow_batch_events BEGIN
    SELECT RAISE(ABORT, 'batch events are append-only');
END;
