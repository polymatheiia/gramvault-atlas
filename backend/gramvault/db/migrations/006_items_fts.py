"""006: FTS5 keyword index over the library (`items_fts`).

`chat.retrieval.keyword_search` used a `LIKE` + `GROUP_CONCAT` scan of the
whole table per query — fine at a couple thousand items, but it does a
substring match (no word/diacritic handling) and gets linearly slower.
This adds an FTS5 table over caption / author / tags / transcript / vision
/ OCR text.

Not trigger-maintained: item text only changes on import/pull (new rows)
and enrichment (transcript/vision/ocr on existing rows), and both call
`gramvault.chat.fts.reindex()` at the end of their batch. `keyword_search`
also self-heals with a full rebuild if it finds the index empty.

`.py` for consistency with 002-005; `schema.sql` carries the same
`CREATE VIRTUAL TABLE` and its populate is a no-op on a fresh (empty) DB.
Keep the row SELECT here in sync with `gramvault.chat.fts._ROW_SELECT`.
"""

from __future__ import annotations

import sqlite3

_FTS_TABLE = """
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    caption, author, tags, transcript, vision, ocr,
    tokenize = 'unicode61 remove_diacritics 2'
)
"""

_POPULATE = """
INSERT INTO items_fts(rowid, caption, author, tags, transcript, vision, ocr)
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


def up(conn: sqlite3.Connection) -> None:
    conn.execute(_FTS_TABLE)
    conn.execute("DELETE FROM items_fts")
    conn.execute(_POPULATE)
