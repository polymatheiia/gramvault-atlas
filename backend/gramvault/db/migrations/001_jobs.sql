-- 001: background job tracking.
--
-- A single table for the long-running jobs the Pipeline UI drives
-- (enrich, categorize, digest, pull, model_pull, reembed). Kept separate
-- from the older `import_jobs` / `export_jobs` tables, which keep their
-- bespoke progress columns.
--
-- The partial UNIQUE index enforces at most one active (pending/running)
-- job per kind at the database level, so a duplicate `POST .../run`
-- fails with an IntegrityError regardless of process/thread count rather
-- than relying on an in-process lock.

CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT NOT NULL CHECK (kind IN (
                        'enrich', 'categorize', 'digest', 'pull', 'model_pull', 'reembed'
                      )),
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                        'pending', 'running', 'done', 'failed', 'cancelled'
                      )),
    params_json       TEXT,   -- request parameters, for reproducibility / resume
    progress_json     TEXT,   -- kind-specific {"done": N, "total": M, ...}
    result_json       TEXT,   -- summary written on success
    error_message     TEXT,
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    started_at        TEXT,
    finished_at       TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_kind_status ON jobs(kind, status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_one_active_per_kind
    ON jobs(kind) WHERE status IN ('pending', 'running');
