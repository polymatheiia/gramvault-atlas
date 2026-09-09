"""`gramvault.chat.fts` — the items_fts index maintenance helpers, and
`keyword_search`'s FTS path vs. its LIKE fallback."""

from __future__ import annotations

import sqlite3

from gramvault.chat import fts, retrieval


def _item(conn: sqlite3.Connection, *, caption=None, tags=None, transcript=None) -> int:
    item_id = conn.execute(
        "INSERT INTO items (media_type, caption) VALUES ('photo', ?)", (caption,)
    ).lastrowid
    if transcript is not None:
        conn.execute(
            "INSERT INTO media_files (item_id, file_path, media_type, transcript) "
            "VALUES (?, 'x.jpg', 'photo', ?)",
            (item_id, transcript),
        )
    for name in tags or []:
        row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        tag_id = row["id"] if row else conn.execute(
            "INSERT INTO tags (name) VALUES (?)", (name,)
        ).lastrowid
        conn.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (item_id, tag_id))
    conn.commit()
    return item_id


class TestReindex:
    def test_full_reindex_populates_all_fields(self, tmp_db_conn: sqlite3.Connection) -> None:
        i = _item(tmp_db_conn, caption="sourdough loaf", tags=["baking"], transcript="proof overnight")
        fts.reindex(tmp_db_conn, None)

        for term in ("sourdough", "baking", "overnight"):
            hit = tmp_db_conn.execute(
                "SELECT rowid FROM items_fts WHERE items_fts MATCH ?", (term,)
            ).fetchone()
            assert hit is not None and hit["rowid"] == i

    def test_targeted_reindex_refreshes_one_item(self, tmp_db_conn: sqlite3.Connection) -> None:
        i = _item(tmp_db_conn, caption="original")
        fts.reindex(tmp_db_conn, None)
        tmp_db_conn.execute("UPDATE items SET caption = 'revised text' WHERE id = ?", (i,))
        tmp_db_conn.commit()

        fts.reindex(tmp_db_conn, [i])
        assert tmp_db_conn.execute(
            "SELECT 1 FROM items_fts WHERE items_fts MATCH 'revised'"
        ).fetchone()
        assert not tmp_db_conn.execute(
            "SELECT 1 FROM items_fts WHERE items_fts MATCH 'original'"
        ).fetchone()

    def test_ensure_populated_rebuilds_an_empty_index(self, tmp_db_conn: sqlite3.Connection) -> None:
        _item(tmp_db_conn, caption="hello world")
        # index left empty (no reindex called)
        assert tmp_db_conn.execute("SELECT count(*) FROM items_fts").fetchone()[0] == 0
        assert fts.ensure_populated(tmp_db_conn) is True
        assert tmp_db_conn.execute("SELECT count(*) FROM items_fts").fetchone()[0] == 1

    def test_helpers_no_op_without_the_table(self, tmp_db_conn: sqlite3.Connection) -> None:
        tmp_db_conn.execute("DROP TABLE items_fts")
        tmp_db_conn.commit()
        assert fts.fts_available(tmp_db_conn) is False
        fts.reindex(tmp_db_conn, None)  # no raise
        assert fts.ensure_populated(tmp_db_conn) is False


class TestKeywordSearchFtsPath:
    def test_prefix_and_diacritics_match(self, tmp_db_conn: sqlite3.Connection) -> None:
        i = _item(tmp_db_conn, caption="Réceptif au changement")
        fts.reindex(tmp_db_conn, None)
        # substring LIKE would need the exact accented string; FTS folds it
        results = retrieval.keyword_search(tmp_db_conn, "recept")
        assert [r.item_id for r in results] == [i]

    def test_multi_word_query_is_anded(self, tmp_db_conn: sqlite3.Connection) -> None:
        both = _item(tmp_db_conn, caption="morning yoga routine")
        _item(tmp_db_conn, caption="morning coffee")
        fts.reindex(tmp_db_conn, None)
        results = retrieval.keyword_search(tmp_db_conn, "morning yoga")
        assert [r.item_id for r in results] == [both]

    def test_punctuation_only_query_returns_nothing(self, tmp_db_conn: sqlite3.Connection) -> None:
        _item(tmp_db_conn, caption="anything")
        fts.reindex(tmp_db_conn, None)
        assert retrieval.keyword_search(tmp_db_conn, "!!! ???") == []

    def test_falls_back_to_like_when_table_missing(self, tmp_db_conn: sqlite3.Connection) -> None:
        i = _item(tmp_db_conn, caption="a pasta recipe")
        tmp_db_conn.execute("DROP TABLE items_fts")
        tmp_db_conn.commit()
        results = retrieval.keyword_search(tmp_db_conn, "pasta")
        assert [r.item_id for r in results] == [i]
