"""`gramvault.export.repository.load_latest_category_digests` (§G3)."""

from __future__ import annotations

import json
import sqlite3

from gramvault.export.repository import load_latest_category_digests


def _insert_digest(
    conn: sqlite3.Connection,
    *,
    name: str,
    selection: dict,
    markdown: str | None,
    status: str = "done",
) -> None:
    conn.execute(
        "INSERT INTO digests (name, template, status, selection_json, item_ids_json, markdown) "
        "VALUES (?, 'book-titles', ?, ?, '[]', ?)",
        (name, status, json.dumps(selection), markdown),
    )
    conn.commit()


def test_returns_newest_completed_digest_per_category(tmp_db_conn: sqlite3.Connection) -> None:
    _insert_digest(tmp_db_conn, name="old", selection={"category": "psychology"}, markdown="v1")
    _insert_digest(tmp_db_conn, name="new", selection={"category": "psychology"}, markdown="v2")
    _insert_digest(tmp_db_conn, name="books", selection={"category": "books/manga"}, markdown="b1")

    result = load_latest_category_digests(tmp_db_conn)
    assert result == {"psychology": "v2", "books/manga": "b1"}


def test_skips_unfinished_empty_query_scoped_and_uncategorized(
    tmp_db_conn: sqlite3.Connection,
) -> None:
    _insert_digest(
        tmp_db_conn, name="running", selection={"category": "psychology"},
        markdown=None, status="running",
    )
    _insert_digest(tmp_db_conn, name="empty", selection={"category": "beauty"}, markdown="")
    _insert_digest(
        tmp_db_conn, name="search", selection={"category": "psychology", "query": "adhd"},
        markdown="q1",
    )
    _insert_digest(tmp_db_conn, name="nocat", selection={"item_ids": [1, 2]}, markdown="n1")

    assert load_latest_category_digests(tmp_db_conn) == {}
