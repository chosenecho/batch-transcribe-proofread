-- Optional, explicitly initialized extension. Existing archive evidence is retained.
CREATE TABLE IF NOT EXISTS content_rights_events (
    event_id INTEGER PRIMARY KEY,
    video_part_id INTEGER NOT NULL REFERENCES video_parts(video_part_id),
    edition_id TEXT REFERENCES publication_editions(edition_id),
    action TEXT NOT NULL CHECK (action IN ('grant', 'revoke', 'hold', 'clear')),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    CHECK ((action IN ('grant', 'revoke') AND edition_id IS NOT NULL)
        OR (action IN ('hold', 'clear') AND edition_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_content_rights_edition
    ON content_rights_events(edition_id, event_id);
CREATE INDEX IF NOT EXISTS idx_content_rights_part
    ON content_rights_events(video_part_id, event_id);
CREATE TRIGGER IF NOT EXISTS immutable_content_rights_update
BEFORE UPDATE ON content_rights_events BEGIN
    SELECT RAISE(ABORT, 'content rights events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS immutable_content_rights_delete
BEFORE DELETE ON content_rights_events BEGIN
    SELECT RAISE(ABORT, 'content rights events are append-only');
END;
