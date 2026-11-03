-- Optional storage extension. Install only in an explicitly staged offline copy.
-- Existing domain identities, frozen JSON and execution state remain authoritative.
CREATE TABLE artifact_storage_contract (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL CHECK (version = 1)
);
INSERT INTO artifact_storage_contract VALUES (1, 1);

CREATE TABLE artifact_objects (
    object_id TEXT PRIMARY KEY CHECK (length(object_id) = 64 AND object_id NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    created_at INTEGER NOT NULL CHECK (created_at >= 0)
);
CREATE TABLE artifact_audio_bindings (
    audio_id INTEGER PRIMARY KEY REFERENCES audio_objects(audio_id) ON DELETE RESTRICT,
    object_id TEXT NOT NULL REFERENCES artifact_objects(object_id) ON DELETE RESTRICT
);
CREATE TABLE artifact_groups (
    group_id TEXT PRIMARY KEY,
    owner_kind TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    version TEXT NOT NULL,
    role TEXT NOT NULL,
    member_count INTEGER NOT NULL CHECK (member_count > 0),
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    UNIQUE (owner_kind, owner_id, version, role)
);
CREATE TABLE artifact_group_members (
    group_id TEXT NOT NULL REFERENCES artifact_groups(group_id) ON DELETE RESTRICT,
    role TEXT NOT NULL,
    object_id TEXT NOT NULL REFERENCES artifact_objects(object_id) ON DELETE RESTRICT,
    PRIMARY KEY (group_id, role)
);
CREATE TABLE artifact_group_paths (
    group_id TEXT NOT NULL,
    role TEXT NOT NULL,
    relative_key TEXT NOT NULL,
    PRIMARY KEY (group_id, role),
    UNIQUE (group_id, relative_key),
    FOREIGN KEY (group_id, role) REFERENCES artifact_group_members(group_id, role) ON DELETE RESTRICT
);
CREATE TABLE artifact_publication_groups (
    publication_id INTEGER PRIMARY KEY REFERENCES workflow_publications(publication_id) ON DELETE RESTRICT,
    group_id TEXT NOT NULL REFERENCES artifact_groups(group_id) ON DELETE RESTRICT
);
CREATE TABLE artifact_targets (
    target_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('local', 'directory')),
    created_at INTEGER NOT NULL CHECK (created_at >= 0)
);
CREATE TABLE artifact_packages (
    package_id TEXT NOT NULL,
    target_id TEXT NOT NULL REFERENCES artifact_targets(target_id) ON DELETE RESTRICT,
    relative_key TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    verified_at INTEGER NOT NULL CHECK (verified_at >= 0),
    UNIQUE (target_id, relative_key),
    PRIMARY KEY (package_id, target_id, relative_key)
);
CREATE TABLE artifact_replicas (
    replica_id INTEGER PRIMARY KEY,
    object_id TEXT NOT NULL REFERENCES artifact_objects(object_id) ON DELETE RESTRICT,
    target_id TEXT NOT NULL REFERENCES artifact_targets(target_id) ON DELETE RESTRICT,
    relative_key TEXT NOT NULL,
    package_id TEXT,
    member_key TEXT NOT NULL DEFAULT '',
    generation INTEGER NOT NULL CHECK (generation > 0),
    verified_sha256 TEXT NOT NULL,
    verified_byte_size INTEGER NOT NULL CHECK (verified_byte_size >= 0),
    verified_at INTEGER NOT NULL CHECK (verified_at >= 0),
    presence TEXT NOT NULL CHECK (presence IN ('present', 'missing', 'released', 'unknown')),
    observed_at INTEGER NOT NULL CHECK (observed_at >= 0),
    CHECK (verified_sha256 = object_id),
    CHECK ((package_id IS NULL AND member_key = '') OR (package_id IS NOT NULL AND member_key != '')),
    FOREIGN KEY (package_id, target_id, relative_key) REFERENCES artifact_packages(package_id, target_id, relative_key) ON DELETE RESTRICT,
    UNIQUE (target_id, relative_key, member_key, generation)
);
CREATE INDEX ix_artifact_replicas_object ON artifact_replicas(object_id, presence, verified_at DESC);
CREATE TABLE artifact_transfers (
    transfer_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('copy', 'offload', 'restore')),
    plan_sha256 TEXT NOT NULL CHECK (length(plan_sha256) = 64 AND plan_sha256 NOT GLOB '*[^0-9a-f]*'),
    target_id TEXT REFERENCES artifact_targets(target_id) ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK (state IN ('planned', 'copying', 'verified', 'releasing', 'complete', 'failed')),
    error_code TEXT,
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= 0)
);
CREATE TABLE artifact_transfer_items (
    transfer_id TEXT NOT NULL REFERENCES artifact_transfers(transfer_id) ON DELETE RESTRICT,
    object_id TEXT NOT NULL REFERENCES artifact_objects(object_id) ON DELETE RESTRICT,
    source_replica_id INTEGER REFERENCES artifact_replicas(replica_id) ON DELETE RESTRICT,
    target_replica_id INTEGER REFERENCES artifact_replicas(replica_id) ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK (state IN ('planned', 'copied', 'verified', 'released', 'restored', 'failed')),
    error_code TEXT,
    updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
    PRIMARY KEY (transfer_id, object_id)
);
CREATE TABLE artifact_pins (
    pin_id TEXT PRIMARY KEY,
    object_id TEXT NOT NULL REFERENCES artifact_objects(object_id) ON DELETE RESTRICT,
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    released_at INTEGER CHECK (released_at IS NULL OR released_at >= created_at)
);
CREATE INDEX ix_artifact_pins_object ON artifact_pins(object_id, released_at);
-- One intent per physical input, including equal bytes at different paths.
-- The immutable generation is checked before quarantine and durable release.
CREATE TABLE artifact_release_intents (
    transfer_id TEXT NOT NULL REFERENCES artifact_transfers(transfer_id) ON DELETE RESTRICT,
    copy_id TEXT NOT NULL,
    object_id TEXT NOT NULL REFERENCES artifact_objects(object_id) ON DELETE RESTRICT,
    source_target_id TEXT NOT NULL REFERENCES artifact_targets(target_id) ON DELETE RESTRICT,
    source_key TEXT NOT NULL,
    quarantine_key TEXT NOT NULL,
    source_replica_id INTEGER REFERENCES artifact_replicas(replica_id) ON DELETE RESTRICT,
    source_generation_json TEXT NOT NULL CHECK (json_valid(source_generation_json)),
    state TEXT NOT NULL CHECK (state IN ('planned', 'isolated', 'released', 'cancelled')),
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
    PRIMARY KEY (transfer_id, copy_id)
);
CREATE TABLE artifact_policies (
    policy_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('off', 'copy', 'offload')),
    target_id TEXT REFERENCES artifact_targets(target_id) ON DELETE RESTRICT,
    config_json TEXT NOT NULL CHECK (json_valid(config_json)),
    updated_at INTEGER NOT NULL CHECK (updated_at >= 0)
);
CREATE TABLE artifact_catalog_upgrades (
    upgrade_id TEXT PRIMARY KEY,
    source_fingerprint TEXT NOT NULL,
    report_key TEXT NOT NULL UNIQUE,
    report_sha256 TEXT NOT NULL CHECK (length(report_sha256) = 64 AND report_sha256 NOT GLOB '*[^0-9a-f]*'),
    inventory_sha256 TEXT NOT NULL CHECK (length(inventory_sha256) = 64 AND inventory_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at INTEGER NOT NULL CHECK (created_at >= 0)
);
-- Frozen alternate preservation evidence, independently enumerable from a
-- snapshot DB. Canonical mutable paths are audit-only in the upgrade report.
CREATE TABLE artifact_catalog_upgrade_files (
    upgrade_id TEXT NOT NULL REFERENCES artifact_catalog_upgrades(upgrade_id) ON DELETE RESTRICT,
    relative_key TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    PRIMARY KEY (upgrade_id, relative_key)
);
