-- Explicit, backed-up maintenance migration; foreign_keys must be disabled
-- outside the transaction and checked again before publication.
CREATE TABLE artifact_targets_v2 (
    target_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('local', 'directory', 's3')),
    created_at INTEGER NOT NULL CHECK (created_at >= 0)
);
INSERT INTO artifact_targets_v2(rowid,target_id,kind,created_at)
    SELECT rowid,target_id,kind,created_at FROM artifact_targets;
DROP TABLE artifact_targets;
ALTER TABLE artifact_targets_v2 RENAME TO artifact_targets;
DROP TABLE artifact_storage_contract;
CREATE TABLE artifact_storage_contract (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL CHECK (version = 2)
);
INSERT INTO artifact_storage_contract VALUES (1, 2);
CREATE TABLE artifact_remote_instances (
    target_id TEXT PRIMARY KEY REFERENCES artifact_targets(target_id) ON DELETE RESTRICT,
    instance_id TEXT NOT NULL CHECK (length(instance_id) = 32 AND instance_id NOT GLOB '*[^0-9a-f]*')
);
CREATE TRIGGER immutable_artifact_remote_instance_update BEFORE UPDATE ON artifact_remote_instances
BEGIN SELECT RAISE(ABORT, 'remote target identity is immutable'); END;
CREATE TRIGGER immutable_artifact_remote_instance_delete BEFORE DELETE ON artifact_remote_instances
BEGIN SELECT RAISE(ABORT, 'remote target identity is immutable'); END;
CREATE TABLE artifact_multipart_uploads (
    transfer_id TEXT PRIMARY KEY REFERENCES artifact_transfers(transfer_id) ON DELETE RESTRICT,
    upload_id TEXT NOT NULL CHECK (length(upload_id) BETWEEN 1 AND 4096),
    part_size INTEGER NOT NULL CHECK (part_size BETWEEN 5242880 AND 67108864),
    parts_json TEXT NOT NULL CHECK (json_valid(parts_json)),
    updated_at INTEGER NOT NULL CHECK (updated_at >= 0)
);
