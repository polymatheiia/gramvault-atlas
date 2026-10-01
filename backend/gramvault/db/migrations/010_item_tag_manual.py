"""010: remember which item<->tag links the user added by hand.

`PATCH /api/library/items/{id}/tags` replaces an item's *manual* tags, and
used to decide "manual" by the tag's global `kind`. But tag names are
unique across kinds, so adding a name that already existed as a hashtag
or AI tag linked that existing tag — and since its kind isn't 'manual',
the link could never be removed again (and the UI showed it as a
non-removable tag). Whether a link was user-added is a property of the
link, not of the tag, so it lives on `item_tags`.

Existing links to `kind='manual'` tags are backfilled as manual, which is
exactly what the old endpoint treated as user-owned.

`.py` (ALTER TABLE ADD COLUMN, guarded) per the same pattern as 003/007/
009; `schema.sql` carries the same column inline.
"""

from __future__ import annotations

import sqlite3


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def up(conn: sqlite3.Connection) -> None:
    if not _has_column(conn, "item_tags", "manual"):
        conn.execute("ALTER TABLE item_tags ADD COLUMN manual INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "UPDATE item_tags SET manual = 1 "
        "WHERE tag_id IN (SELECT id FROM tags WHERE kind = 'manual')"
    )
