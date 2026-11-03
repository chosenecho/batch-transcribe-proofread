-- Fixed input and derived content. Execution ownership remains workflow_attempts.
CREATE TABLE IF NOT EXISTS manuscript_contract (
    version INTEGER PRIMARY KEY CHECK (version = 1)
);
INSERT OR IGNORE INTO manuscript_contract VALUES (1);

CREATE TABLE IF NOT EXISTS editorial_inputs (
    input_id TEXT PRIMARY KEY,
    video_part_id INTEGER NOT NULL REFERENCES video_parts(video_part_id),
    base_transcript_id INTEGER NOT NULL REFERENCES transcripts(transcript_id),
    reference_transcript_id INTEGER REFERENCES transcripts(transcript_id),
    prepared_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS editorial_job_inputs (
    job_id TEXT PRIMARY KEY REFERENCES workflow_jobs(job_id),
    input_id TEXT NOT NULL REFERENCES editorial_inputs(input_id)
);

CREATE TABLE IF NOT EXISTS editorial_model_calls (
    call_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES workflow_attempts(attempt_id),
    input_id TEXT NOT NULL REFERENCES editorial_inputs(input_id),
    chunk_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT,
    error_code TEXT,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    CHECK (response_json IS NULL OR finished_at IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS editorial_chunk_results (
    input_id TEXT NOT NULL REFERENCES editorial_inputs(input_id),
    chunk_id TEXT NOT NULL,
    call_id TEXT NOT NULL REFERENCES editorial_model_calls(call_id),
    blocks_json TEXT NOT NULL,
    PRIMARY KEY (input_id, chunk_id)
);

CREATE TABLE IF NOT EXISTS editorial_revisions (
    revision_id TEXT PRIMARY KEY,
    input_id TEXT NOT NULL UNIQUE REFERENCES editorial_inputs(input_id),
    job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id),
    blocks_json TEXT NOT NULL,
    quality_status TEXT NOT NULL CHECK (quality_status IN ('ai-unreviewed', 'needs-review')),
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS document_artifacts (
    revision_id TEXT NOT NULL REFERENCES editorial_revisions(revision_id),
    template_version TEXT NOT NULL CHECK (template_version = 'ai-draft-v1'),
    artifact_name TEXT NOT NULL CHECK (artifact_name IN ('ai-draft.md', 'review.md')),
    manuscript_role TEXT NOT NULL CHECK (manuscript_role IN ('ai-draft', 'review-reference')),
    relative_path TEXT NOT NULL UNIQUE,
    content_sha256 TEXT NOT NULL CHECK (
        length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    PRIMARY KEY (revision_id, template_version, artifact_name),
    CHECK ((artifact_name = 'ai-draft.md' AND manuscript_role = 'ai-draft')
        OR (artifact_name = 'review.md' AND manuscript_role = 'review-reference'))
);

CREATE TABLE IF NOT EXISTS publication_editions (
    edition_id TEXT PRIMARY KEY,
    video_part_id INTEGER NOT NULL REFERENCES video_parts(video_part_id),
    revision_id TEXT NOT NULL REFERENCES editorial_revisions(revision_id),
    parent_edition_id TEXT REFERENCES publication_editions(edition_id),
    content_json TEXT NOT NULL CHECK (json_valid(content_json)),
    content_sha256 TEXT NOT NULL CHECK (
        length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    created_at INTEGER NOT NULL,
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS publication_edition_reviews (
    edition_id TEXT PRIMARY KEY REFERENCES publication_editions(edition_id),
    review_id TEXT NOT NULL UNIQUE,
    content_sha256 TEXT NOT NULL CHECK (
        length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    status TEXT NOT NULL CHECK (status IN (
        'pending-review', 'in-review', 'changes-requested',
        'approved', 'rejected'
    )),
    actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    note TEXT NOT NULL DEFAULT '',
    issue_url TEXT,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS publication_releases (
    release_id TEXT PRIMARY KEY,
    video_part_id INTEGER NOT NULL REFERENCES video_parts(video_part_id),
    edition_id TEXT NOT NULL UNIQUE REFERENCES publication_editions(edition_id),
    review_id TEXT NOT NULL REFERENCES publication_edition_reviews(review_id),
    content_sha256 TEXT NOT NULL CHECK (
        length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    template_version TEXT NOT NULL CHECK (template_version = 'publish-v1'),
    relative_path TEXT NOT NULL UNIQUE,
    artifact_sha256 TEXT NOT NULL CHECK (
        length(artifact_sha256) = 64 AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    published_at INTEGER NOT NULL,
    published_by TEXT NOT NULL CHECK (length(trim(published_by)) > 0),
    status TEXT NOT NULL CHECK (status IN ('published', 'superseded', 'withdrawn'))
);

CREATE TABLE IF NOT EXISTS publication_heads (
    video_part_id INTEGER PRIMARY KEY REFERENCES video_parts(video_part_id),
    current_edition_id TEXT NOT NULL REFERENCES publication_editions(edition_id),
    current_release_id TEXT REFERENCES publication_releases(release_id)
);

CREATE TABLE IF NOT EXISTS publication_events (
    event_id INTEGER PRIMARY KEY,
    video_part_id INTEGER NOT NULL REFERENCES video_parts(video_part_id),
    edition_id TEXT NOT NULL REFERENCES publication_editions(edition_id),
    release_id TEXT REFERENCES publication_releases(release_id),
    review_id TEXT REFERENCES publication_edition_reviews(review_id),
    content_sha256 TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'created', 'edited', 'in-review', 'changes-requested',
        'approved', 'rejected', 'published', 'superseded', 'withdrawn'
    )),
    from_status TEXT,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    issue_url TEXT,
    note TEXT NOT NULL DEFAULT '',
    changed_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_publication_events_part
    ON publication_events(video_part_id, event_id);

CREATE TRIGGER IF NOT EXISTS immutable_publication_editions_update
BEFORE UPDATE ON publication_editions BEGIN
    SELECT RAISE(ABORT, 'publication editions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_publication_editions_delete
BEFORE DELETE ON publication_editions BEGIN
    SELECT RAISE(ABORT, 'publication editions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS terminal_publication_reviews
BEFORE UPDATE ON publication_edition_reviews
WHEN OLD.status IN ('approved', 'rejected') BEGIN
    SELECT RAISE(ABORT, 'terminal publication review is immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_publication_review_identity
BEFORE UPDATE ON publication_edition_reviews
WHEN NEW.edition_id IS NOT OLD.edition_id OR NEW.review_id IS NOT OLD.review_id
    OR NEW.content_sha256 IS NOT OLD.content_sha256 BEGIN
    SELECT RAISE(ABORT, 'publication review identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_publication_reviews_delete
BEFORE DELETE ON publication_edition_reviews BEGIN
    SELECT RAISE(ABORT, 'publication reviews are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_publication_releases
BEFORE UPDATE ON publication_releases
WHEN NEW.release_id IS NOT OLD.release_id
    OR NEW.video_part_id IS NOT OLD.video_part_id
    OR NEW.edition_id IS NOT OLD.edition_id
    OR NEW.review_id IS NOT OLD.review_id
    OR NEW.content_sha256 IS NOT OLD.content_sha256
    OR NEW.template_version IS NOT OLD.template_version
    OR NEW.relative_path IS NOT OLD.relative_path
    OR NEW.artifact_sha256 IS NOT OLD.artifact_sha256
    OR NEW.published_at IS NOT OLD.published_at
    OR NEW.published_by IS NOT OLD.published_by
    OR NOT ((OLD.status = 'published' AND NEW.status IN ('superseded', 'withdrawn'))
        OR (OLD.status = 'superseded' AND NEW.status = 'withdrawn')) BEGIN
    SELECT RAISE(ABORT, 'publication release identity or terminal state is immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_publication_releases_delete
BEFORE DELETE ON publication_releases BEGIN
    SELECT RAISE(ABORT, 'publication releases are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_publication_events_update
BEFORE UPDATE ON publication_events BEGIN
    SELECT RAISE(ABORT, 'publication events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS immutable_publication_events_delete
BEFORE DELETE ON publication_events BEGIN
    SELECT RAISE(ABORT, 'publication events are append-only');
END;
