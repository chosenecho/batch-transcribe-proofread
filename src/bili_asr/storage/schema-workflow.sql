-- Stateless workflow control plane.
--
-- Metadata, audio objects and immutable transcript versions remain the data
-- plane.  This schema owns only scheduling and operational evidence.  In
-- particular, a subtitle result never decides whether a local-ASR job exists.

CREATE TABLE IF NOT EXISTS workflow_asr_profiles (
    profile_id INTEGER PRIMARY KEY,
    profile_key TEXT NOT NULL,
    model_name TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    aligner_name TEXT NOT NULL,
    device TEXT NOT NULL,
    language TEXT,
    config_sha256 TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_asr_profiles_config
    ON workflow_asr_profiles(profile_key, config_sha256);

-- Additive extension: legacy profile rows and their digests remain untouched.
CREATE TABLE IF NOT EXISTS workflow_asr_profile_configs (
    profile_id INTEGER PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 2),
    config_json TEXT NOT NULL CHECK (json_valid(config_json)),
    FOREIGN KEY (profile_id) REFERENCES workflow_asr_profiles(profile_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS workflow_jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('subtitle', 'audio', 'asr', 'publish', 'index', 'proofread', 'render_document')),
    video_part_id INTEGER,
    profile_id INTEGER,
    policy_key TEXT,
    dedupe_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    priority INTEGER NOT NULL DEFAULT 0,
    available_at INTEGER NOT NULL,
    lease_owner TEXT,
    lease_expires_at INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error_code TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY (video_part_id) REFERENCES video_parts(video_part_id) ON DELETE RESTRICT,
    FOREIGN KEY (profile_id) REFERENCES workflow_asr_profiles(profile_id) ON DELETE RESTRICT,
    CHECK ((kind = 'asr' AND profile_id IS NOT NULL) OR kind <> 'asr')
);

CREATE INDEX IF NOT EXISTS ix_workflow_jobs_claim
    ON workflow_jobs(status, available_at, priority DESC, created_at, job_id);

CREATE INDEX IF NOT EXISTS ix_workflow_jobs_part
    ON workflow_jobs(video_part_id, kind, status);

CREATE TABLE IF NOT EXISTS workflow_job_dependencies (
    job_id TEXT NOT NULL,
    prerequisite_job_id TEXT NOT NULL,
    PRIMARY KEY (job_id, prerequisite_job_id),
    FOREIGN KEY (job_id) REFERENCES workflow_jobs(job_id) ON DELETE CASCADE,
    FOREIGN KEY (prerequisite_job_id) REFERENCES workflow_jobs(job_id) ON DELETE RESTRICT,
    CHECK (job_id <> prerequisite_job_id)
);

CREATE TABLE IF NOT EXISTS workflow_attempts (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    outcome TEXT NOT NULL CHECK (outcome IN ('running', 'succeeded', 'failed', 'cancelled')),
    error_code TEXT,
    result_json TEXT,
    FOREIGN KEY (job_id) REFERENCES workflow_jobs(job_id) ON DELETE RESTRICT,
    CHECK ((outcome = 'running' AND finished_at IS NULL)
        OR (outcome IN ('succeeded', 'failed', 'cancelled') AND finished_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS ix_workflow_attempts_job
    ON workflow_attempts(job_id, started_at DESC);

CREATE TABLE IF NOT EXISTS workflow_quality_assessments (
    assessment_id INTEGER PRIMARY KEY,
    video_part_id INTEGER NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('subtitle-ai', 'subtitle-cc', 'asr-local')),
    score REAL NOT NULL CHECK (score >= 0.0 AND score <= 1.0),
    assessor TEXT NOT NULL,
    details_json TEXT NOT NULL,
    assessed_at INTEGER NOT NULL,
    FOREIGN KEY (video_part_id) REFERENCES video_parts(video_part_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_workflow_quality_part
    ON workflow_quality_assessments(video_part_id, assessed_at DESC);

CREATE TABLE IF NOT EXISTS workflow_publications (
    publication_id INTEGER PRIMARY KEY,
    video_part_id INTEGER NOT NULL,
    transcript_id INTEGER NOT NULL,
    published_at INTEGER NOT NULL,
    artifact_json TEXT NOT NULL,
    UNIQUE (video_part_id, transcript_id),
    FOREIGN KEY (video_part_id) REFERENCES video_parts(video_part_id) ON DELETE RESTRICT,
    FOREIGN KEY (transcript_id) REFERENCES transcripts(transcript_id) ON DELETE RESTRICT
);
