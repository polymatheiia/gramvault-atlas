"""003: on-screen text (OCR) as a first-class enrichment step.

Reels are frequently text-over-video: the overlay carries the recipe, the
punchline, the book title. `gramvault.ai.ocr` reads it, but its output was
being written into `media_files.vision_caption` — the same column as the
scene caption — with an empty string standing in for a discarded (Cyrillic
/ refusal) read so the queue wouldn't re-run it forever. That overloading
made the two kinds of text indistinguishable to retrieval and to the
document builder.

This migration gives OCR its own columns:

    ocr_text          cleaned on-screen text, or NULL when discarded
    ocr_attempted_at  set *even on a discard* — the queue is
                      `ocr_attempted_at IS NULL`, so a frame the local
                      model can't read (Cyrillic) stops being retried
                      until a stronger vision model is configured, at
                      which point the "retry discarded" scope re-runs
                      exactly the `ocr_text IS NULL AND ocr_attempted_at
                      IS NOT NULL` rows.
    ocr_model         provider:model that produced `ocr_text`
    vision_model      provenance for `vision_caption` (same idea)
    transcript_model  provenance for `transcript`

`.py` (not `.sql`) because SQLite has no `ALTER TABLE ... ADD COLUMN IF
NOT EXISTS`; the `_has_column` guard makes it re-runnable if it fails
partway. `schema.sql` carries the same columns inline — the parity test
in `tests/test_db_migrations.py` keeps the two from drifting.
"""

from __future__ import annotations

import sqlite3

_MEDIA_COLUMNS = [
    "ocr_text TEXT",
    "ocr_attempted_at TEXT",
    "ocr_model TEXT",
    "vision_model TEXT",
    "transcript_model TEXT",
]


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def up(conn: sqlite3.Connection) -> None:
    for column_def in _MEDIA_COLUMNS:
        column = column_def.split()[0]
        if not _has_column(conn, "media_files", column):
            conn.execute(f"ALTER TABLE media_files ADD COLUMN {column_def}")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_media_files_ocr_pending "
        "ON media_files(item_id) WHERE ocr_attempted_at IS NULL"
    )
