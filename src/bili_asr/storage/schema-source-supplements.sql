CREATE TABLE source_supplement_contract (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    policy TEXT NOT NULL CHECK (policy = 'legacy-part-title-supplement-v1')
);
INSERT INTO source_supplement_contract VALUES (1, 'legacy-part-title-supplement-v1');
CREATE TABLE source_metadata_supplements (
    supplement_id TEXT PRIMARY KEY CHECK (length(supplement_id) = 64),
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    registered_at INTEGER NOT NULL CHECK (registered_at >= 0)
);
CREATE TABLE publication_source_supplements (
    edition_id TEXT PRIMARY KEY REFERENCES publication_import_origins(edition_id),
    supplement_id TEXT NOT NULL REFERENCES source_metadata_supplements(supplement_id),
    root_edition_id TEXT NOT NULL REFERENCES publication_editions(edition_id)
);
CREATE TABLE source_supplement_batches (
    plan_digest TEXT PRIMARY KEY CHECK (length(plan_digest) = 64),
    plan_json TEXT NOT NULL CHECK (json_valid(plan_json)),
    result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    actor TEXT NOT NULL,
    applied_at INTEGER NOT NULL CHECK (applied_at >= 0)
);
CREATE TRIGGER supplement_contract_no_update BEFORE UPDATE ON source_supplement_contract
BEGIN SELECT RAISE(ABORT, 'immutable supplement contract'); END;
CREATE TRIGGER supplement_contract_no_delete BEFORE DELETE ON source_supplement_contract
BEGIN SELECT RAISE(ABORT, 'immutable supplement contract'); END;
CREATE TRIGGER supplements_no_update BEFORE UPDATE ON source_metadata_supplements
BEGIN SELECT RAISE(ABORT, 'immutable source supplement'); END;
CREATE TRIGGER supplements_no_delete BEFORE DELETE ON source_metadata_supplements
BEGIN SELECT RAISE(ABORT, 'immutable source supplement'); END;
CREATE TRIGGER edition_supplements_no_update BEFORE UPDATE ON publication_source_supplements
BEGIN SELECT RAISE(ABORT, 'immutable edition supplement'); END;
CREATE TRIGGER edition_supplements_no_delete BEFORE DELETE ON publication_source_supplements
BEGIN SELECT RAISE(ABORT, 'immutable edition supplement'); END;
CREATE TRIGGER supplement_batches_no_update BEFORE UPDATE ON source_supplement_batches
BEGIN SELECT RAISE(ABORT, 'immutable supplement receipt'); END;
CREATE TRIGGER supplement_batches_no_delete BEFORE DELETE ON source_supplement_batches
BEGIN SELECT RAISE(ABORT, 'immutable supplement receipt'); END;
