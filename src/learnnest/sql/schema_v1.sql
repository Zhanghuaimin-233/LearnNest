PRAGMA foreign_keys = ON;

CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE source_files (
    path TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('task', 'batch', 'schedule')),
    sha256 TEXT NOT NULL
);

CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    task_path TEXT NOT NULL UNIQUE,
    schema_version TEXT NOT NULL,
    title TEXT NOT NULL,
    source_input TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    profile TEXT NOT NULL,
    error_summary TEXT,
    duplicate_of_task_id TEXT REFERENCES tasks(task_id)
);

CREATE TABLE source_identities (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
    normalized_source TEXT NOT NULL,
    platform TEXT,
    platform_id TEXT,
    content_sha256 TEXT
);

CREATE TABLE task_stages (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (task_id, stage)
);

CREATE TABLE task_artifacts (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    position INTEGER NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (task_id, stage, position)
);

CREATE TABLE attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    reason TEXT NOT NULL,
    from_stage TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    failed_stage TEXT,
    batch_id TEXT,
    failure_code TEXT,
    failure_category TEXT,
    failure_disposition TEXT,
    safe_summary TEXT,
    next_retry_at TEXT,
    UNIQUE (task_id, ordinal)
);

CREATE TABLE schedules (
    schedule_id TEXT PRIMARY KEY,
    schedule_path TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    cursor TEXT
);

CREATE TABLE batches (
    batch_id TEXT PRIMARY KEY,
    batch_path TEXT NOT NULL UNIQUE,
    schema_version TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    profile TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    schedule_id TEXT REFERENCES schedules(schedule_id),
    cursor_before TEXT,
    cursor_after TEXT,
    CHECK (kind NOT IN ('scan', 'scheduled') OR schedule_id IS NOT NULL)
);

CREATE TABLE batch_items (
    batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    input TEXT NOT NULL,
    input_type TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    task_id TEXT REFERENCES tasks(task_id),
    attempt_id TEXT REFERENCES attempts(attempt_id),
    error TEXT,
    failure_code TEXT,
    failure_disposition TEXT,
    PRIMARY KEY (batch_id, position)
);

CREATE VIEW failure_queue AS
SELECT
    attempts.task_id,
    attempts.attempt_id,
    attempts.ordinal,
    attempts.failed_stage,
    attempts.failure_code,
    attempts.failure_category,
    attempts.failure_disposition,
    attempts.safe_summary,
    attempts.next_retry_at
FROM attempts
WHERE attempts.status IN ('failed', 'interrupted')
  AND attempts.failure_disposition = 'retryable'
  AND attempts.failed_stage NOT IN ('note', 'podcast_script', 'tts')
  AND attempts.ordinal = (
    SELECT MAX(latest.ordinal)
    FROM attempts AS latest
    WHERE latest.task_id = attempts.task_id
  );
