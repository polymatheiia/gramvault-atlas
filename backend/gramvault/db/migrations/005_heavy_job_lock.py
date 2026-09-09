"""005: at most one "heavy" job active at a time.

The per-kind unique index from 001 (`idx_jobs_one_active_per_kind`) stops
a second *enrich* job while one is running, but nothing stopped an enrich
and a digest — or a re-embed and an LLM categorize — from running at the
same time and thrashing Ollama + faster-whisper on a small box
(INTEGRATION-PLAN.md §H2, the "global heavy-job semaphore of 1").

This adds a second partial UNIQUE index over a constant expression,
scoped to the model-bound kinds (`enrich`, `categorize`, `digest`,
`reembed`), so the DB rejects a second heavy job exactly the way it
rejects a same-kind one. `pull` / `model_pull` are network-bound and stay
free to overlap.

`.py` for consistency with 002-004; `schema.sql` carries the same index
inline and the parity test in `tests/test_db_migrations.py` keeps them in
sync. Keep the kind list here identical to `jobs._HEAVY_KINDS`.
"""

from __future__ import annotations

import sqlite3

_HEAVY_LOCK_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_one_active_heavy
    ON jobs((1))
    WHERE kind IN ('enrich', 'categorize', 'digest', 'reembed')
      AND status IN ('pending', 'running')
"""


def up(conn: sqlite3.Connection) -> None:
    conn.execute(_HEAVY_LOCK_INDEX)
