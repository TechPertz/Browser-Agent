-- Andera SQLite schema. WAL mode, foreign keys on.
-- All timestamps stored as ISO-8601 UTC text for portability.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    task_name     TEXT NOT NULL,
    task_prompt   TEXT NOT NULL,
    input_path    TEXT NOT NULL,
    output_dir    TEXT NOT NULL,
    mode          TEXT NOT NULL DEFAULT 'auto',
    concurrency   INTEGER NOT NULL DEFAULT 4,
    status        TEXT NOT NULL DEFAULT 'pending',
    seed          INTEGER,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
    sample_id      TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    row_index      INTEGER NOT NULL,
    input_json     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    attempts       INTEGER NOT NULL DEFAULT 0,
    extracted_json TEXT,
    evidence_dir   TEXT,
    error          TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_samples_run_status ON samples(run_id, status);

CREATE TABLE IF NOT EXISTS artifacts (
    sha256     TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    mime       TEXT NOT NULL,
    size       INTEGER NOT NULL,
    path       TEXT NOT NULL,
    sample_id  TEXT REFERENCES samples(sample_id) ON DELETE SET NULL,
    run_id     TEXT REFERENCES runs(run_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_sample ON artifacts(sample_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);

CREATE TABLE IF NOT EXISTS queue (
    item_id      TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | claimed | done | dead
    claimed_at   TEXT,
    claim_token  TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status, created_at);

-- Hash-chained tamper-evident audit log.
CREATE TABLE IF NOT EXISTS audit_log (
    event_id     TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    run_id       TEXT,
    sample_id    TEXT,
    timestamp    TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash    TEXT,
    this_hash    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_log(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_sample ON audit_log(sample_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp);

-- Non-hash-chained operational event log (for dashboards/telemetry).
CREATE TABLE IF NOT EXISTS event_log (
    event_id     TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    run_id       TEXT,
    sample_id    TEXT,
    timestamp    TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_run ON event_log(run_id);
CREATE INDEX IF NOT EXISTS idx_event_time ON event_log(timestamp);
