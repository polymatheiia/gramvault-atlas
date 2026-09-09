"""Maintenance for the `items_fts` FTS5 index (migration 006).

Rebuilt in bulk, not via triggers: the searchable text of an item only
changes on import/pull (new rows) and enrichment (transcript/vision/ocr on
existing rows). Those paths call `reindex(conn, ids)` for the items they
touched; `chat.retrieval.keyword_search` calls `ensure_populated()` and
does a full rebuild if it finds the index empty.
"""

from __future__ import annotations

import sqlite3

# Keep in sync with `db/migrations/006_items_fts.py::_POPULATE`.
_ROW_SELECT = """
SELECT
    i.id,
    COALESCE(i.caption, ''),
    TRIM(COALESCE(a.username, '') || ' ' || COALESCE(a.full_name, '')),
    COALESCE((SELECT GROUP_CONCAT(t.name, ' ')
              FROM item_tags it JOIN tags t ON t.id = it.tag_id
              WHERE it.item_id = i.id), ''),
    COALESCE((SELECT GROUP_CONCAT(mf.transcript, ' ')
              FROM media_files mf WHERE mf.item_id = i.id AND mf.transcript <> ''), ''),
    COALESCE((SELECT GROUP_CONCAT(mf.vision_caption, ' ')
              FROM media_files mf WHERE mf.item_id = i.id AND mf.vision_caption <> ''), ''),
    COALESCE((SELECT GROUP_CONCAT(mf.ocr_text, ' ')
              FROM media_files mf WHERE mf.item_id = i.id AND mf.ocr_text <> ''), '')
FROM items i
LEFT JOIN authors a ON a.id = i.author_id
"""

_INSERT = "INSERT INTO items_fts(rowid, caption, author, tags, transcript, vision, ocr) "


def fts_available(conn: sqlite3.Connection) -> bool:
    """True if the `items_fts` table exists (migration 006 applied)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'items_fts'"
    ).fetchone()
    return row is not None


def reindex(conn: sqlite3.Connection, item_ids: list[int] | None = None) -> None:
    """Rebuild FTS rows. `None` rebuilds the whole index; a list rebuilds
    just those items (call it after writing their caption/tags/enrichment).
    A no-op if migration 006 hasn't run."""
    if not fts_available(conn):
        return
    if item_ids is None:
        conn.execute("DELETE FROM items_fts")
        conn.execute(_INSERT + _ROW_SELECT)
    elif item_ids:
        marks = ",".join("?" * len(item_ids))
        conn.execute(f"DELETE FROM items_fts WHERE rowid IN ({marks})", tuple(item_ids))
        conn.execute(_INSERT + _ROW_SELECT + f" WHERE i.id IN ({marks})", tuple(item_ids))
    conn.commit()


def ensure_populated(conn: sqlite3.Connection) -> bool:
    """Full rebuild if the index is empty but the library isn't (a DB from
    before 006's populate, or a manually dropped index). Returns True if
    the index is usable."""
    if not fts_available(conn):
        return False
    fts_n = conn.execute("SELECT count(*) FROM items_fts").fetchone()[0]
    items_n = conn.execute("SELECT count(*) FROM items").fetchone()[0]
    if fts_n == 0 and items_n > 0:
        reindex(conn, None)
    return True


__all__ = ["ensure_populated", "fts_available", "reindex"]
