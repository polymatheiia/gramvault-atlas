"""Tests for the /api/library/* endpoints: filterable item listing,
single-item detail, authors/tags listing, and manual tag updates.

Seeds the DB directly via `tmp_config`'s session_scope rather than going
through the ingestion pipeline, since these tests are about the query/
filter logic, not import parsing (see test_ingestion_importer.py for
that).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from gramvault.ai import embedding_store
from gramvault.config import Config
from gramvault.db.session import session_scope


def _seed_library(config: Config) -> dict[str, int]:
    with session_scope(config) as conn:
        conn.execute("INSERT INTO authors (username, full_name) VALUES ('alice', 'Alice A')")
        alice_id = conn.execute(
            "SELECT id FROM authors WHERE username = 'alice'"
        ).fetchone()["id"]
        conn.execute("INSERT INTO authors (username) VALUES ('bob')")
        bob_id = conn.execute("SELECT id FROM authors WHERE username = 'bob'").fetchone()["id"]

        conn.execute(
            "INSERT INTO items (external_id, author_id, media_type, caption, taken_at) "
            "VALUES ('item-1', ?, 'photo', 'a sunny beach day', '2024-01-01T00:00:00')",
            (alice_id,),
        )
        item1_id = conn.execute(
            "SELECT id FROM items WHERE external_id = 'item-1'"
        ).fetchone()["id"]

        conn.execute(
            "INSERT INTO items (external_id, author_id, media_type, caption, taken_at) "
            "VALUES ('item-2', ?, 'reel', 'city nightlife video', '2024-06-01T00:00:00')",
            (bob_id,),
        )
        item2_id = conn.execute(
            "SELECT id FROM items WHERE external_id = 'item-2'"
        ).fetchone()["id"]

        conn.execute(
            "INSERT INTO media_files (item_id, file_path, media_type, sequence_index) "
            "VALUES (?, 'media/aa/aaaa.jpg', 'photo', 0)",
            (item1_id,),
        )

        conn.execute("INSERT INTO tags (name, kind) VALUES ('travel', 'auto')")
        travel_tag_id = conn.execute(
            "SELECT id FROM tags WHERE name = 'travel'"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (item1_id, travel_tag_id)
        )

    return {"alice_id": alice_id, "bob_id": bob_id, "item1_id": item1_id, "item2_id": item2_id}


def test_list_items_no_filters_returns_all(client: TestClient, tmp_config: Config) -> None:
    _seed_library(tmp_config)

    response = client.get("/api/library/items")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2


def test_list_items_filter_by_author(client: TestClient, tmp_config: Config) -> None:
    _seed_library(tmp_config)

    response = client.get("/api/library/items", params={"author": "alice"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["external_id"] == "item-1"
    assert body["items"][0]["author"]["username"] == "alice"
    assert len(body["items"][0]["media_files"]) == 1
    assert len(body["items"][0]["tags"]) == 1
    assert body["items"][0]["tags"][0]["name"] == "travel"


def test_list_item_ids_matches_list_order_and_filters(
    client: TestClient, tmp_config: Config
) -> None:
    ids = _seed_library(tmp_config)

    full = client.get("/api/library/items").json()
    id_only = client.get("/api/library/item-ids").json()
    assert id_only["total"] == 2
    assert id_only["ids"] == [it["id"] for it in full["items"]]

    reels = client.get("/api/library/item-ids", params={"media_type": "reel"}).json()
    assert reels["ids"] == [ids["item2_id"]]


def test_list_items_by_explicit_ids_preserves_order(
    client: TestClient, tmp_config: Config
) -> None:
    ids = _seed_library(tmp_config)
    order = [ids["item2_id"], ids["item1_id"]]

    body = client.get(
        "/api/library/items", params={"ids": ",".join(map(str, order))}
    ).json()
    assert [it["id"] for it in body["items"]] == order
    # unknown ids are silently dropped, not errors
    body2 = client.get("/api/library/items", params={"ids": f"{ids['item1_id']},999999"}).json()
    assert [it["id"] for it in body2["items"]] == [ids["item1_id"]]


def test_list_items_filter_by_media_type(client: TestClient, tmp_config: Config) -> None:
    _seed_library(tmp_config)

    response = client.get("/api/library/items", params={"media_type": "reel"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["external_id"] == "item-2"


def test_list_items_filter_by_tag(client: TestClient, tmp_config: Config) -> None:
    _seed_library(tmp_config)

    response = client.get("/api/library/items", params={"tag": "travel"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["external_id"] == "item-1"


def test_list_items_free_text_search(client: TestClient, tmp_config: Config) -> None:
    _seed_library(tmp_config)

    response = client.get("/api/library/items", params={"q": "nightlife"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["external_id"] == "item-2"


def test_list_items_free_text_search_matches_transcript(
    client: TestClient, tmp_config: Config
) -> None:
    """R12: `q` used to be `caption LIKE` only, even though the FTS5 index
    already covers transcript/vision/OCR text — that broader search was
    only reachable through the semantic chat endpoint. A word that
    appears in a media file's transcript, and nowhere in either item's
    caption, should now still find the item."""
    ids = _seed_library(tmp_config)
    with session_scope(tmp_config) as conn:
        conn.execute(
            "INSERT INTO media_files (item_id, file_path, media_type, sequence_index, transcript) "
            "VALUES (?, 'media/bb/bbbb.mp4', 'video', 0, 'a story about kayaking upriver')",
            (ids["item2_id"],),
        )

    response = client.get("/api/library/items", params={"q": "kayaking"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["external_id"] == "item-2"


def test_list_items_free_text_search_with_fts_operator_words_falls_back_to_like(
    client: TestClient, tmp_config: Config
) -> None:
    """FTS5 treats a bare AND/OR/NOT/NEAR as a query operator, not a
    search term — `q="cats and dogs"` tokenizes to `cats* AND* dogs*`,
    which is a MATCH syntax error (the operator followed by a stray `*`).
    Must fall back to the caption LIKE path instead of 500ing, the same
    way chat.retrieval.keyword_search already does for the semantic
    endpoint."""
    with session_scope(tmp_config) as conn:
        conn.execute(
            "INSERT INTO items (external_id, media_type, caption) "
            "VALUES ('item-cats', 'photo', 'cats and dogs playing')"
        )

    response = client.get("/api/library/items", params={"q": "cats and dogs"})

    assert response.status_code == 200
    assert response.json()["total"] == 1


def test_list_items_date_range_filter(client: TestClient, tmp_config: Config) -> None:
    _seed_library(tmp_config)

    response = client.get(
        "/api/library/items",
        params={"date_from": "2024-05-01T00:00:00", "date_to": "2024-12-31T00:00:00"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["external_id"] == "item-2"


def test_list_items_pagination(client: TestClient, tmp_config: Config) -> None:
    _seed_library(tmp_config)

    response = client.get("/api/library/items", params={"page": 1, "page_size": 1})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert len(body["items"]) == 1
    assert body["page"] == 1
    assert body["page_size"] == 1


def test_get_item_returns_full_detail(client: TestClient, tmp_config: Config) -> None:
    ids = _seed_library(tmp_config)

    response = client.get(f"/api/library/items/{ids['item1_id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["external_id"] == "item-1"
    assert body["author"]["username"] == "alice"


def test_get_item_404_for_unknown_id(client: TestClient, tmp_config: Config) -> None:
    _seed_library(tmp_config)

    response = client.get("/api/library/items/999999")

    assert response.status_code == 404


def test_list_authors(client: TestClient, tmp_config: Config) -> None:
    _seed_library(tmp_config)

    response = client.get("/api/library/authors")

    assert response.status_code == 200
    usernames = {a["username"] for a in response.json()}
    assert usernames == {"alice", "bob"}


def test_list_tags(client: TestClient, tmp_config: Config) -> None:
    _seed_library(tmp_config)

    response = client.get("/api/library/tags")

    assert response.status_code == 200
    names = {t["name"] for t in response.json()}
    assert names == {"travel"}


def test_update_item_tags_replaces_manual_tags_only(
    client: TestClient, tmp_config: Config
) -> None:
    ids = _seed_library(tmp_config)

    response = client.patch(
        f"/api/library/items/{ids['item1_id']}/tags", json={"tags": ["favorites", "summer"]}
    )

    assert response.status_code == 200
    body = response.json()
    tag_names = {t["name"] for t in body["tags"]}
    # The auto tag 'travel' survives a manual-tags update; the new manual
    # tags are added alongside it.
    assert tag_names == {"travel", "favorites", "summer"}


def test_update_item_tags_404_for_unknown_item(client: TestClient, tmp_config: Config) -> None:
    _seed_library(tmp_config)

    response = client.patch("/api/library/items/999999/tags", json={"tags": ["x"]})

    assert response.status_code == 404


# --- categories -----------------------------------------------------------


def _seed_categorised(config: Config) -> dict[str, int]:
    """Two items, one put in 'recipes', one left uncategorised."""
    ids = _seed_library(config)
    with session_scope(config) as conn:
        recipes_id = conn.execute(
            "SELECT id FROM categories WHERE name = 'recipes'"
        ).fetchone()["id"]
        conn.execute(
            "UPDATE items SET category_id = ?, category_source = 'manual' WHERE id = ?",
            (recipes_id, ids["item1_id"]),
        )
    ids["recipes_id"] = recipes_id
    return ids


def test_default_categories_are_seeded(client: TestClient, tmp_config: Config) -> None:
    body = client.get("/api/library/categories").json()
    names = [c["name"] for c in body["categories"]]
    assert "recipes" in names and "psychology" in names and "other" in names
    assert len(names) == 14
    # sorted by sort_order
    assert body["categories"] == sorted(body["categories"], key=lambda c: c["sort_order"])


def test_categories_report_counts_and_uncategorised(
    client: TestClient, tmp_config: Config
) -> None:
    _seed_categorised(tmp_config)
    body = client.get("/api/library/categories").json()
    by_name = {c["name"]: c for c in body["categories"]}
    assert by_name["recipes"]["count"] == 1
    assert by_name["memes"]["count"] == 0
    assert body["uncategorized_count"] == 1
    assert body["total"] == 2


def test_list_items_filter_by_category(client: TestClient, tmp_config: Config) -> None:
    ids = _seed_categorised(tmp_config)

    recipes = client.get("/api/library/items", params={"category": "recipes"}).json()
    assert [i["id"] for i in recipes["items"]] == [ids["item1_id"]]
    assert recipes["items"][0]["category"] == "recipes"
    assert recipes["items"][0]["category_source"] == "manual"

    uncat = client.get(
        "/api/library/items", params={"category": "__uncategorized__"}
    ).json()
    assert [i["id"] for i in uncat["items"]] == [ids["item2_id"]]

    unknown = client.get("/api/library/items", params={"category": "nope"}).json()
    assert unknown["total"] == 0


def test_patch_item_category_sets_and_clears(client: TestClient, tmp_config: Config) -> None:
    ids = _seed_categorised(tmp_config)
    memes_id = client.get("/api/library/categories").json()["categories"]
    memes_id = next(c["id"] for c in memes_id if c["name"] == "memes")

    set_resp = client.patch(
        f"/api/library/items/{ids['item2_id']}", json={"category_id": memes_id}
    )
    assert set_resp.status_code == 200
    assert set_resp.json()["category"] == "memes"
    assert set_resp.json()["category_source"] == "manual"

    clear_resp = client.patch(
        f"/api/library/items/{ids['item2_id']}", json={"category_id": None}
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json()["category"] is None
    assert clear_resp.json()["category_source"] is None


def test_patch_item_category_rejects_unknown_category(
    client: TestClient, tmp_config: Config
) -> None:
    ids = _seed_library(tmp_config)
    resp = client.patch(
        f"/api/library/items/{ids['item1_id']}", json={"category_id": 99999}
    )
    assert resp.status_code == 422


def test_patch_item_category_404_for_unknown_item(client: TestClient) -> None:
    assert client.patch("/api/library/items/424242", json={"category_id": None}).status_code == 404


def test_patch_item_meta_sets_favourite_and_note(client: TestClient, tmp_config: Config) -> None:
    ids = _seed_library(tmp_config)
    item_id = ids["item1_id"]

    res = client.patch(f"/api/library/items/{item_id}/meta", json={"favourite": True, "user_note": "watch again"})
    assert res.status_code == 200
    body = res.json()
    assert body["favourite"] is True
    assert body["user_note"] == "watch again"

    # A field left unset stays unchanged.
    res2 = client.patch(f"/api/library/items/{item_id}/meta", json={"favourite": False})
    assert res2.json()["favourite"] is False
    assert res2.json()["user_note"] == "watch again"

    # An explicit empty string clears the note.
    res3 = client.patch(f"/api/library/items/{item_id}/meta", json={"user_note": ""})
    assert res3.json()["user_note"] is None


def test_patch_item_meta_404_for_unknown_item(client: TestClient) -> None:
    assert client.patch("/api/library/items/424242/meta", json={"favourite": True}).status_code == 404


def test_media_captions_vtt(client: TestClient, tmp_config: Config) -> None:
    ids = _seed_library(tmp_config)
    with session_scope(tmp_config) as conn:
        media_id = conn.execute(
            "INSERT INTO media_files (item_id, file_path, media_type, sequence_index) "
            "VALUES (?, 'media/bb/bbbb.mp4', 'video', 0) RETURNING id",
            (ids["item2_id"],),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO transcript_segments (media_file_id, sequence_index, start_seconds, end_seconds, text) "
            "VALUES (?, 0, 0.0, 2.5, 'Hello there'), (?, 1, 2.5, 65.25, 'Second line')",
            (media_id, media_id),
        )

    res = client.get(f"/api/library/media/{media_id}/captions.vtt")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/vtt")
    assert res.text.startswith("WEBVTT\n\n")
    assert "00:00:00.000 --> 00:00:02.500" in res.text
    assert "Hello there" in res.text
    assert "00:01:05.250" in res.text


def test_media_captions_vtt_404_without_segments(client: TestClient, tmp_config: Config) -> None:
    ids = _seed_library(tmp_config)
    with session_scope(tmp_config) as conn:
        media_id = conn.execute(
            "INSERT INTO media_files (item_id, file_path, media_type, sequence_index) "
            "VALUES (?, 'media/cc/cccc.mp4', 'video', 0) RETURNING id",
            (ids["item2_id"],),
        ).fetchone()["id"]
    assert client.get(f"/api/library/media/{media_id}/captions.vtt").status_code == 404


def test_list_items_filter_by_favourite(client: TestClient, tmp_config: Config) -> None:
    ids = _seed_library(tmp_config)
    client.patch(f"/api/library/items/{ids['item1_id']}/meta", json={"favourite": True})

    res = client.get("/api/library/items?favourite=true")
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == ids["item1_id"]


def test_create_rename_and_delete_category(client: TestClient, tmp_config: Config) -> None:
    ids = _seed_categorised(tmp_config)

    created = client.post(
        "/api/library/categories", json={"name": "cooking", "description": "food"}
    )
    assert created.status_code == 201
    new_id = created.json()["id"]

    assert client.post("/api/library/categories", json={"name": "cooking"}).status_code == 409

    renamed = client.patch(f"/api/library/categories/{new_id}", json={"name": "food & drink"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "food & drink"

    # deleting an unused category is fine
    assert client.delete(f"/api/library/categories/{new_id}").status_code == 204

    # deleting a populated one without move_to is refused
    del_resp = client.delete(f"/api/library/categories/{ids['recipes_id']}")
    assert del_resp.status_code == 409

    # ...but works with move_to, reassigning the items
    other_id = next(
        c["id"]
        for c in client.get("/api/library/categories").json()["categories"]
        if c["name"] == "other"
    )
    moved = client.delete(
        f"/api/library/categories/{ids['recipes_id']}", params={"move_to": other_id}
    )
    assert moved.status_code == 204
    assert client.get("/api/library/items", params={"category": "other"}).json()["total"] == 1


def test_delete_item_removes_it_and_cascades(client: TestClient, tmp_config: Config) -> None:
    ids = _seed_library(tmp_config)

    resp = client.delete(f"/api/library/items/{ids['item1_id']}")
    assert resp.status_code == 204

    assert client.get(f"/api/library/items/{ids['item1_id']}").status_code == 404
    with session_scope(tmp_config) as conn:
        assert conn.execute(
            "SELECT 1 FROM media_files WHERE item_id = ?", (ids["item1_id"],)
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM item_tags WHERE item_id = ?", (ids["item1_id"],)
        ).fetchone() is None

    # the other item is untouched
    assert client.get(f"/api/library/items/{ids['item2_id']}").status_code == 200


def test_delete_item_404_for_unknown_id(client: TestClient, tmp_config: Config) -> None:
    _seed_library(tmp_config)
    assert client.delete("/api/library/items/999999").status_code == 404


def test_delete_item_drops_its_embedded_vectors(client: TestClient, tmp_config: Config) -> None:
    ids = _seed_library(tmp_config)
    embedding_store.upsert_item(
        ids["item1_id"], [0.1, 0.2, 0.3], "a sunny beach day", config=tmp_config
    )

    assert client.delete(f"/api/library/items/{ids['item1_id']}").status_code == 204

    assert embedding_store.get_item_embedding(ids["item1_id"], config=tmp_config) is None


def test_similar_items_returns_empty_before_embedding(
    client: TestClient, tmp_config: Config
) -> None:
    ids = _seed_library(tmp_config)
    assert client.get(f"/api/library/items/{ids['item1_id']}/similar").json() == []


def test_similar_items_404_for_unknown_id(client: TestClient, tmp_config: Config) -> None:
    _seed_library(tmp_config)
    assert client.get("/api/library/items/999999/similar").status_code == 404


def test_similar_items_excludes_self_and_orders_by_score(
    client: TestClient, tmp_config: Config
) -> None:
    ids = _seed_library(tmp_config)
    embedding_store.upsert_item(
        ids["item1_id"], [1.0, 0.0, 0.0], "a sunny beach day", config=tmp_config
    )
    embedding_store.upsert_item(
        ids["item2_id"], [0.9, 0.1, 0.0], "city nightlife video", config=tmp_config
    )

    resp = client.get(f"/api/library/items/{ids['item1_id']}/similar")
    assert resp.status_code == 200
    body = resp.json()
    assert [item["id"] for item in body] == [ids["item2_id"]]


def test_sort_default_is_saved_date_newest_first(client: TestClient, tmp_config: Config) -> None:
    ids = _seed_library(tmp_config)
    resp = client.get("/api/library/items")
    assert [i["id"] for i in resp.json()["items"]] == [ids["item2_id"], ids["item1_id"]]


def test_sort_posted_date_orders_by_taken_at_not_import_order(
    client: TestClient, tmp_config: Config
) -> None:
    with session_scope(tmp_config) as conn:
        # Inserted (and so imported) first, but taken far in the future —
        # saved_date and posted_date must disagree on the order for this
        # pair, or the test can't tell the two sort modes apart.
        conn.execute(
            "INSERT INTO items (external_id, media_type, taken_at) "
            "VALUES ('future', 'photo', '2030-01-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO items (external_id, media_type, taken_at) "
            "VALUES ('past', 'photo', '2020-01-01T00:00:00')"
        )

    saved_date = client.get("/api/library/items").json()
    assert [i["external_id"] for i in saved_date["items"]] == ["past", "future"]

    posted_date = client.get("/api/library/items", params={"sort": "posted_date"}).json()
    assert [i["external_id"] for i in posted_date["items"]] == ["future", "past"]


def test_sort_author_orders_alphabetically_by_username(
    client: TestClient, tmp_config: Config
) -> None:
    ids = _seed_library(tmp_config)  # alice -> item-1, bob -> item-2

    resp = client.get("/api/library/items", params={"sort": "author"})

    assert [i["id"] for i in resp.json()["items"]] == [ids["item1_id"], ids["item2_id"]]


def test_sort_relevance_ranks_stronger_fts_matches_first(
    client: TestClient, tmp_config: Config
) -> None:
    with session_scope(tmp_config) as conn:
        conn.execute(
            "INSERT INTO items (external_id, media_type, caption) "
            "VALUES ('weak', 'photo', 'pasta mentioned once here')"
        )
        conn.execute(
            "INSERT INTO items (external_id, media_type, caption) "
            "VALUES ('strong', 'photo', 'pasta pasta pasta recipe')"
        )

    resp = client.get("/api/library/items", params={"q": "pasta", "sort": "relevance"})

    assert [i["external_id"] for i in resp.json()["items"]] == ["strong", "weak"]


def test_sort_relevance_without_q_falls_back_to_saved_date(
    client: TestClient, tmp_config: Config
) -> None:
    ids = _seed_library(tmp_config)

    resp = client.get("/api/library/items", params={"sort": "relevance"})

    assert [i["id"] for i in resp.json()["items"]] == [ids["item2_id"], ids["item1_id"]]


def test_item_ids_accepts_sort_too_and_matches_items_order(
    client: TestClient, tmp_config: Config
) -> None:
    ids = _seed_library(tmp_config)

    resp = client.get("/api/library/item-ids", params={"sort": "author"})

    assert resp.json()["ids"] == [ids["item1_id"], ids["item2_id"]]
