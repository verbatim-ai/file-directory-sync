-- Schema version 1 — file directory sync local state.
--
-- Applied by migrations.py, which tracks the applied version in
-- PRAGMA user_version. Every statement here must be safe to re-run.

-- One row per invocation of the job. Gives cron runs a history, lets a run
-- that died mid-flight be spotted (status still RUNNING with no finished_at),
-- and anchors the audit trail in `event`.
CREATE TABLE IF NOT EXISTS sync_run (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT    NOT NULL,
    finished_at    TEXT,
    status         TEXT    NOT NULL CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED')),
    mode           TEXT    NOT NULL,
    config_path    TEXT,
    corpus_id      TEXT    NOT NULL,
    root_dir       TEXT    NOT NULL,
    dry_run        INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0, 1)),
    files_scanned  INTEGER NOT NULL DEFAULT 0,
    files_uploaded INTEGER NOT NULL DEFAULT 0,
    files_updated  INTEGER NOT NULL DEFAULT 0,
    files_deleted  INTEGER NOT NULL DEFAULT 0,
    files_skipped  INTEGER NOT NULL DEFAULT 0,
    files_failed   INTEGER NOT NULL DEFAULT 0,
    error          TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_run_started
    ON sync_run (corpus_id, started_at DESC);

-- The core mapping table: one row per local file that passed the filters,
-- carrying the document UID it corresponds to in the corpus.
--
-- Identity is (corpus_id, rel_path): a path relative to root_dir, so moving
-- the root on disk does not orphan every document. rel_path always uses '/'
-- separators regardless of platform.
CREATE TABLE IF NOT EXISTS file (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    corpus_id             TEXT    NOT NULL,
    rel_path              TEXT    NOT NULL,
    abs_path              TEXT    NOT NULL,
    filename              TEXT    NOT NULL,
    content_type          TEXT,

    -- (size, mtime_ns) is the cheap change-detection fast path; content_hash
    -- is authoritative and also mirrors the server's duplicate detection,
    -- which rejects a commit whose content already exists in the corpus.
    size                  INTEGER,
    mtime_ns              INTEGER,
    content_hash          TEXT,

    -- NULL until POST /v1/doc/init succeeds and returns a document.
    document_id           TEXT,

    -- Mirror of the platform lifecycle status for this document.
    remote_status         TEXT CHECK (remote_status IS NULL OR remote_status IN (
                              'AWAITING_UPLOAD', 'PENDING', 'PROCESSING', 'READY', 'FAILED')),

    -- Local state machine, advanced by the sync engine.
    sync_state            TEXT    NOT NULL DEFAULT 'NEW' CHECK (sync_state IN (
                              'NEW', 'PENDING_UPLOAD', 'UPLOADED', 'COMMITTED',
                              'SYNCED', 'FAILED', 'MISSING_LOCAL')),

    -- Presigned PUT URL from init. Single-use and short-lived (~900s), but
    -- storing it lets an interrupted run resume without a second init.
    upload_url            TEXT,
    upload_url_expires_at TEXT,

    attempts              INTEGER NOT NULL DEFAULT 0,
    last_error            TEXT,

    first_seen_at         TEXT    NOT NULL,
    last_seen_at          TEXT    NOT NULL,
    last_synced_at        TEXT,

    -- Deletion detection: rows whose last_seen_run_id is not the current run
    -- were not found on disk this time round.
    last_seen_run_id      INTEGER REFERENCES sync_run (id) ON DELETE SET NULL,

    UNIQUE (corpus_id, rel_path)
);

-- A document UID may back at most one local file.
CREATE UNIQUE INDEX IF NOT EXISTS idx_file_document_id
    ON file (document_id) WHERE document_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_file_state
    ON file (corpus_id, sync_state);

CREATE INDEX IF NOT EXISTS idx_file_hash
    ON file (corpus_id, content_hash);

CREATE INDEX IF NOT EXISTS idx_file_last_seen_run
    ON file (corpus_id, last_seen_run_id);

-- Append-only audit trail: the database counterpart of the log file. Survives
-- log rotation, and makes "what did last Tuesday's run do to this file?"
-- answerable with a query.
CREATE TABLE IF NOT EXISTS event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER REFERENCES sync_run (id) ON DELETE CASCADE,
    file_id    INTEGER REFERENCES file (id) ON DELETE SET NULL,
    ts         TEXT NOT NULL,
    level      TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message    TEXT,
    details    TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_run ON event (run_id, id);
CREATE INDEX IF NOT EXISTS idx_event_file ON event (file_id, id);
