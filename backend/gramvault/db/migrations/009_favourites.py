"""009: favourites and user notes on items (Phase 4 feature backlog —
audit §5.1 "Favourites / user notes").

Two plain columns on `items`: `favourite` (a flag, filterable from the
gallery) and `user_note` (free text, shown/edited on the item page and
exported as a "My note" section of the Obsidian note's managed body —
see `export/markdown_builder.py`). Deliberately not indexed into `items_fts`:
that would mean keeping `chat/fts.py`'s reindex triggers, `_ROW_SELECT`,
and the FTS schema all in sync with a column that changes on every
keystroke of a note edit, for a "search my own notes" feature nobody
has asked for yet. Filtering by `favourite=true` in the gallery doesn't
need FTS at all — it's an indexed boolean column.
"""

from __future__ import annotations

import sqlite3


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def up(conn: sqlite3.Connection) -> None:
    if not _has_column(conn, "items", "favourite"):
        conn.execute("ALTER TABLE items ADD COLUMN favourite INTEGER NOT NULL DEFAULT 0")
    if not _has_column(conn, "items", "user_note"):
        conn.execute("ALTER TABLE items ADD COLUMN user_note TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_favourite ON items(favourite)")
