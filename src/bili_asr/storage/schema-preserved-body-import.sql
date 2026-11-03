CREATE TABLE archive_contract_extensions (
    name TEXT PRIMARY KEY CHECK (name = 'preserved-body-import-v1'),
    version INTEGER NOT NULL CHECK (version = 1)
);
INSERT INTO archive_contract_extensions VALUES ('preserved-body-import-v1', 1);
CREATE TABLE manuscript_import_baselines (
    import_id TEXT PRIMARY KEY CHECK (length(import_id) = 64),
    video_part_id INTEGER NOT NULL REFERENCES video_parts(video_part_id),
    revision_id TEXT NOT NULL REFERENCES editorial_revisions(revision_id),
    legacy_edition_id TEXT REFERENCES publication_editions(edition_id),
    baseline_json TEXT NOT NULL CHECK (json_valid(baseline_json)),
    body_path TEXT NOT NULL UNIQUE,
    body_sha256 TEXT NOT NULL CHECK (length(body_sha256) = 64),
    review_path TEXT NOT NULL UNIQUE,
    review_sha256 TEXT NOT NULL CHECK (length(review_sha256) = 64),
    imported_at INTEGER NOT NULL CHECK (imported_at >= 0)
);
CREATE TABLE publication_import_origins (
    edition_id TEXT PRIMARY KEY REFERENCES publication_editions(edition_id),
    import_id TEXT NOT NULL REFERENCES manuscript_import_baselines(import_id),
    root_edition_id TEXT NOT NULL REFERENCES publication_editions(edition_id),
    relation TEXT NOT NULL CHECK (relation IN ('preserved', 'derived')),
    expected_markdown_sha256 TEXT NOT NULL CHECK (length(expected_markdown_sha256) = 64)
);
CREATE TABLE manuscript_import_batches (
    plan_digest TEXT PRIMARY KEY CHECK (length(plan_digest) = 64),
    plan_json TEXT NOT NULL CHECK (json_valid(plan_json)),
    result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    actor TEXT NOT NULL,
    applied_at INTEGER NOT NULL CHECK (applied_at >= 0)
);
CREATE TRIGGER extensions_no_update BEFORE UPDATE ON archive_contract_extensions
BEGIN SELECT RAISE(ABORT, 'immutable archive extension'); END;
CREATE TRIGGER extensions_no_delete BEFORE DELETE ON archive_contract_extensions
BEGIN SELECT RAISE(ABORT, 'immutable archive extension'); END;
CREATE TRIGGER import_baselines_no_update BEFORE UPDATE ON manuscript_import_baselines
BEGIN SELECT RAISE(ABORT, 'immutable import baseline'); END;
CREATE TRIGGER import_baselines_no_delete BEFORE DELETE ON manuscript_import_baselines
BEGIN SELECT RAISE(ABORT, 'immutable import baseline'); END;
CREATE TRIGGER import_origins_no_update BEFORE UPDATE ON publication_import_origins
BEGIN SELECT RAISE(ABORT, 'immutable import origin'); END;
CREATE TRIGGER import_origins_no_delete BEFORE DELETE ON publication_import_origins
BEGIN SELECT RAISE(ABORT, 'immutable import origin'); END;
CREATE TRIGGER import_batches_no_update BEFORE UPDATE ON manuscript_import_batches
BEGIN SELECT RAISE(ABORT, 'immutable import receipt'); END;
CREATE TRIGGER import_batches_no_delete BEFORE DELETE ON manuscript_import_batches
BEGIN SELECT RAISE(ABORT, 'immutable import receipt'); END;
