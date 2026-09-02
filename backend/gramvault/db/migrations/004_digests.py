"""004: saved digests.

A digest is a Markdown document distilled from a selection of items
through a template (`book-titles`, `advice-digest`, …) by the map/reduce
engine in `gramvault.ai.digest`. Each run is its own row — re-running the
same selection keeps the previous digest for history/diff instead of
overwriting it (mirrors the by-hand `~/reels-workflow` docs, which were
regenerated wholesale each pass).

`selection_json` is the request (`{category, query, item_ids}`);
`item_ids_json` is the exact id snapshot the run used, so a digest stays
reproducible even as the library grows. `provider`/`model`/`tokens_*`/
`cost_estimate` are the run manifest.

`.py` (not `.sql`) for consistency with 002/003 and the re-runnable
`_has_table` guard; `schema.sql` carries the same `CREATE TABLE` inline
and the parity test keeps them in sync.
"""

from __future__ import annotations

import sqlite3

_DIGESTS_TABLE = """
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
)
"""


def up(conn: sqlite3.Connection) -> None:
    conn.execute(_DIGESTS_TABLE)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_digests_created_at ON digests(created_at)")
