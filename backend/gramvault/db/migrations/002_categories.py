"""002: item categories.

Exactly one category per item (distinct from the many-to-many `tags`) —
first-class enough to facet, filter, and put in Obsidian frontmatter, so
it's a column on `items` plus a `categories` lookup table rather than a
reserved tag kind.

`.py` (not `.sql`) because SQLite has no `ALTER TABLE ... ADD COLUMN IF
NOT EXISTS` — the `_has_column` guard makes the migration re-runnable if
it fails partway.

The seed taxonomy lives in `db/session.DEFAULT_CATEGORIES` (also used by
`init_db` for a fresh database) so it's defined once.
"""

from __future__ import annotations

import sqlite3

from gramvault.db.session import seed_default_categories

_ITEM_COLUMNS = [
    "category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL",
    "category_source TEXT CHECK (category_source IN ('keyword', 'llm', 'manual'))",
    "category_confidence REAL",
    "category_reason TEXT",
    "category_updated_at TEXT",
]


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            color       TEXT,
            description TEXT
        )
        """
    )
    seed_default_categories(conn)

    for column_def in _ITEM_COLUMNS:
        column = column_def.split()[0]
        if not _has_column(conn, "items", column):
            conn.execute(f"ALTER TABLE items ADD COLUMN {column_def}")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_category_id ON items(category_id)")
