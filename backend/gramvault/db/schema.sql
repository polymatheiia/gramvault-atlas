-- GramVault SQLite schema.
--
-- This is the canonical full definition for a FRESH database. Existing
-- databases are brought up to date by the numbered files in
-- `db/migrations/` (tracked via `PRAGMA user_version`) — see
-- `gramvault.db.session`. Every change here must be mirrored by a
-- migration, and vice versa; `tests/test_db_migrations.py` fails if the
-- two drift. `gramvault.db.session.init_db()` picks the right path
-- (schema.sql for a fresh DB, migrations for an existing one).

PRAGMA foreign_keys = ON;

-- Instagram authors/creators whose content was saved.
CREATE TABLE IF NOT EXISTS authors (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT NOT NULL UNIQUE,
    full_name    TEXT,
    profile_url  TEXT,
    avatar_path  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Single-category taxonomy for items (distinct from the many-to-many
-- `tags`). Seeded from `gramvault.db.session.DEFAULT_CATEGORIES` by
-- `init_db` / migration 002; user-editable via /api/library/categories.
CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    color       TEXT,
    description TEXT
);

-- One saved post/reel/carousel. May have multiple media_files (carousel).
-- The category_* columns mirror migration 002_categories.py.
CREATE TABLE IF NOT EXISTS items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id         TEXT UNIQUE, -- Instagram shortcode/post id, if known
    author_id           INTEGER REFERENCES authors(id) ON DELETE SET NULL,
    media_type          TEXT NOT NULL CHECK (media_type IN ('photo', 'video', 'reel', 'carousel')),
    caption             TEXT,
    permalink           TEXT,
    taken_at            TEXT, -- ISO8601 timestamp of original post, if known
    imported_at         TEXT NOT NULL DEFAULT (datetime('now')),
    import_job_id       INTEGER REFERENCES import_jobs(id) ON DELETE SET NULL,
    enrichment_status   TEXT NOT NULL DEFAULT 'pending'
                         CHECK (enrichment_status IN ('pending', 'running', 'done', 'failed')),
    raw_metadata_json   TEXT, -- original exporter JSON blob, for anything unmapped
    category_id         INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    category_source     TEXT CHECK (category_source IN ('keyword', 'llm', 'manual')),
    category_confidence REAL,
    category_reason     TEXT,
    category_updated_at TEXT,
    favourite           INTEGER NOT NULL DEFAULT 0, -- migration 009
    user_note           TEXT                        -- migration 009
);

CREATE INDEX IF NOT EXISTS idx_items_author_id ON items(author_id);
CREATE INDEX IF NOT EXISTS idx_items_enrichment_status ON items(enrichment_status);
CREATE INDEX IF NOT EXISTS idx_items_import_job_id ON items(import_job_id);
CREATE INDEX IF NOT EXISTS idx_items_category_id ON items(category_id);
CREATE INDEX IF NOT EXISTS idx_items_favourite ON items(favourite);

-- Individual media files belonging to an item (photo, or video + optional
-- extracted keyframes are tracked separately by the enrichment pipeline,
-- not as rows here -- this table is the "source" files only).
CREATE TABLE IF NOT EXISTS media_files (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id             INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    file_path           TEXT NOT NULL,
    media_type          TEXT NOT NULL CHECK (media_type IN ('photo', 'video')),
    sequence_index      INTEGER NOT NULL DEFAULT 0, -- ordering within a carousel
    width               INTEGER,
    height              INTEGER,
    duration_seconds    REAL, -- videos only
    transcript          TEXT, -- faster-whisper output, videos only (Agent A3)
    vision_caption      TEXT, -- llava output (Agent A3)
    checksum            TEXT,
    ocr_text            TEXT, -- cleaned on-screen text, NULL when discarded (migration 003)
    ocr_attempted_at    TEXT, -- set even on a discard; queue is `ocr_attempted_at IS NULL`
    ocr_model           TEXT, -- provider:model that produced ocr_text
    vision_model        TEXT, -- provenance for vision_caption
    transcript_model    TEXT  -- provenance for transcript
);

CREATE INDEX IF NOT EXISTS idx_media_files_item_id ON media_files(item_id);
CREATE INDEX IF NOT EXISTS idx_media_files_ocr_pending
    ON media_files(item_id) WHERE ocr_attempted_at IS NULL;

-- Per-segment Whisper timestamps for a video's transcript (migration 008).
-- media_files.transcript stays the flattened text; this is what a
-- caption deep-link or WebVTT track would read from.
CREATE TABLE IF NOT EXISTS transcript_segments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id  INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    sequence_index INTEGER NOT NULL,
    start_seconds  REAL NOT NULL,
    end_seconds    REAL NOT NULL,
    text           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transcript_segments_media_file_id
    ON transcript_segments(media_file_id);

-- Free-form tags, either hashtags pulled from captions, auto-generated by
-- the AI pipeline, or added manually by the user in the gallery UI.
CREATE TABLE IF NOT EXISTS tags (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE,
    kind    TEXT NOT NULL DEFAULT 'auto' CHECK (kind IN ('auto', 'manual', 'hashtag'))
);

CREATE TABLE IF NOT EXISTS item_tags (
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, tag_id)
);

-- Tracks a single "import a ZIP export" run, for progress + resumability.
CREATE TABLE IF NOT EXISTS import_jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path         TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'running', 'done', 'failed')),
    total_items         INTEGER NOT NULL DEFAULT 0,
    processed_items     INTEGER NOT NULL DEFAULT 0,
    failed_items        INTEGER NOT NULL DEFAULT 0,
    error_message       TEXT,
    started_at          TEXT,
    finished_at         TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    cancel_requested    INTEGER NOT NULL DEFAULT 0
);

-- Chat sessions + messages. Agent A4 owns the actual RAG logic; this is
-- just the persistence shape.
CREATE TABLE IF NOT EXISTS chat_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session_id ON chat_messages(session_id);

-- Citations attached to an assistant message, pointing back at the
-- library item (and optionally the specific media file) that grounded it.
CREATE TABLE IF NOT EXISTS chat_citations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      INTEGER NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
    item_id         INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    media_file_id   INTEGER REFERENCES media_files(id) ON DELETE SET NULL,
    snippet         TEXT
);

CREATE INDEX IF NOT EXISTS idx_chat_citations_message_id ON chat_citations(message_id);

-- Tracks a single "export to Obsidian vault" run (Agent A6), mirroring
-- import_jobs. Kept as its own table (rather than adding a `kind` column
-- to import_jobs) to avoid touching a table A2 is concurrently relying on.
CREATE TABLE IF NOT EXISTS export_jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_subfolder     TEXT,
    status              TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'running', 'done', 'failed')),
    total_items         INTEGER NOT NULL DEFAULT 0,
    processed_items     INTEGER NOT NULL DEFAULT 0,
    failed_items        INTEGER NOT NULL DEFAULT 0,
    notes_written       INTEGER NOT NULL DEFAULT 0,
    notes_updated       INTEGER NOT NULL DEFAULT 0,
    media_files_copied  INTEGER NOT NULL DEFAULT 0,
    skipped_json        TEXT, -- JSON list of {"item_id": ..., "reason": ...}
    error_message       TEXT,
    started_at          TEXT,
    finished_at         TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_export_jobs_created_at ON export_jobs(created_at);

-- Background jobs for the Pipeline UI (enrich / categorize / digest /
-- pull / model_pull / reembed). Mirror of migration 001_jobs.sql — keep
-- the two in sync. `import_jobs` / `export_jobs` predate this and keep
-- their own bespoke progress columns.
CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT NOT NULL CHECK (kind IN (
                        'enrich', 'categorize', 'digest', 'pull', 'model_pull', 'reembed'
                      )),
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                        'pending', 'running', 'done', 'failed', 'cancelled'
                      )),
    params_json       TEXT,
    progress_json     TEXT,
    result_json       TEXT,
    error_message     TEXT,
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    started_at        TEXT,
    finished_at       TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_kind_status ON jobs(kind, status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at);

-- At most one active (pending/running) job per kind, enforced in the DB.
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_one_active_per_kind
    ON jobs(kind) WHERE status IN ('pending', 'running');

-- At most one active "heavy" (model-bound) job across kinds — enrich,
-- categorize, digest and reembed all hammer Ollama + faster-whisper, so
-- they can't overlap on a small box (migration 005; keep the kind list in
-- sync with jobs._HEAVY_KINDS). pull / model_pull are network-bound and
-- may still overlap.
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_one_active_heavy
    ON jobs((1))
    WHERE kind IN ('enrich', 'categorize', 'digest', 'reembed')
      AND status IN ('pending', 'running');

-- Saved digests: a Markdown doc distilled from a selection of items via a
-- template by the map/reduce engine in `gramvault.ai.digest`. Mirror of
-- migration 004_digests.py — keep the two in sync. Each run is its own
-- row (history/diff, never overwritten); `item_ids_json` snapshots the
-- exact selection so the digest stays reproducible.
CREATE TABLE IF NOT EXISTS digests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    template          TEXT NOT NULL,
    template_version  TEXT,
    status            TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'running', 'done', 'failed', 'cancelled')),
    selection_json    TEXT NOT NULL,
    item_ids_json     TEXT NOT NULL DEFAULT '[]',
    provider          TEXT,
    model             TEXT,
    tokens_in         INTEGER NOT NULL DEFAULT 0,
    tokens_out        INTEGER NOT NULL DEFAULT 0,
    cost_estimate     REAL,
    markdown          TEXT,
    error_message     TEXT,
    job_id            INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_digests_created_at ON digests(created_at);

-- FTS5 keyword index over the library (migration 006_items_fts.py — keep
-- in sync). Not trigger-maintained: import/pull and enrichment call
-- gramvault.chat.fts.reindex() for the items they touch, and
-- keyword_search() does a full rebuild if it finds the index empty. On a
-- fresh DB this starts empty (nothing to populate).
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    caption, author, tags, transcript, vision, ocr,
    tokenize = 'unicode61 remove_diacritics 2'
);
