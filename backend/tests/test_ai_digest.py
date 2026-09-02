"""Tests for `gramvault.ai.digest` — template loading, selection,
token batching, the citation/link post-processor, and `run_digest`
end-to-end with the provider mocked."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from gramvault.ai import digest as digest_engine
from gramvault.ai.digest import (
    DigestError,
    DigestTemplate,
    Selection,
    _postprocess,
    _reduce,
    plan_batches,
    run_digest,
    select_items,
)
from gramvault.config import Config
from gramvault.db.session import session_scope
from gramvault.models.schemas import Author, Item, MediaFile


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seed(
    config: Config,
    caption: str,
    *,
    category: str | None = None,
    username: str = "acc",
    permalink: str | None = None,
) -> int:
    with session_scope(config) as conn:
        author_id = conn.execute(
            "INSERT INTO authors (username) VALUES (?) ON CONFLICT(username) DO UPDATE "
            "SET username = excluded.username RETURNING id",
            (username,),
        ).fetchone()["id"]
        category_id = None
        if category:
            category_id = conn.execute(
                "SELECT id FROM categories WHERE name = ?", (category,)
            ).fetchone()["id"]
        item_id = conn.execute(
            "INSERT INTO items (media_type, caption, author_id, category_id, permalink) "
            "VALUES ('reel', ?, ?, ?, ?)",
            (caption, author_id, category_id, permalink or f"https://instagram.com/p/{caption[:6]}/"),
        ).lastrowid
    assert item_id is not None
    return item_id


class TestTemplates:
    def test_bundled_templates_load(self, tmp_config: Config) -> None:
        templates = digest_engine.load_templates(tmp_config)
        assert "book-titles" in templates
        assert templates["book-titles"].source == "builtin"
        assert templates["book-titles"].extract_prompt

    def test_user_template_overrides_bundled(self, tmp_config: Config) -> None:
        user_dir = tmp_config.resolved_db_path.parent / "digest_templates"
        user_dir.mkdir(parents=True, exist_ok=True)
        (user_dir / "book-titles.yaml").write_text(
            "name: book-titles\nextract_prompt: custom\nreduce_prompt: custom\n"
            "description: mine\nversion: '9'\n",
            encoding="utf-8",
        )
        templates = digest_engine.load_templates(tmp_config)
        assert templates["book-titles"].source == "user"
        assert templates["book-titles"].version == "9"

    def test_unknown_template_raises(self, tmp_config: Config) -> None:
        with pytest.raises(DigestError):
            digest_engine.get_template("nope", tmp_config)


class TestSelection:
    @pytest.mark.anyio
    async def test_explicit_ids(self, tmp_config: Config, tmp_db_conn) -> None:
        a = _seed(tmp_config, "one")
        _seed(tmp_config, "two")
        with session_scope(tmp_config) as conn:
            ids = await select_items(conn, Selection(item_ids=[a, 9999]), tmp_config)
        assert ids == [a]

    @pytest.mark.anyio
    async def test_by_category(self, tmp_config: Config, tmp_db_conn) -> None:
        book = _seed(tmp_config, "a novel", category="books/manga")
        _seed(tmp_config, "leg day", category="workouts")
        with session_scope(tmp_config) as conn:
            ids = await select_items(conn, Selection(category="books/manga"), tmp_config)
        assert ids == [book]

    @pytest.mark.anyio
    async def test_empty_selection_raises(self, tmp_config: Config, tmp_db_conn) -> None:
        with session_scope(tmp_config) as conn, pytest.raises(DigestError):
            await select_items(conn, Selection(), tmp_config)


class TestBatching:
    def test_splits_on_token_budget(self) -> None:
        big = [
            Item(id=i, media_type="reel", caption="x " * 6000, author=Author(username="a"))
            for i in range(4)
        ]
        batches = plan_batches(big)
        assert len(batches) > 1
        assert sum(len(b) for b in batches) == 4


class TestReduce:
    @pytest.mark.anyio
    async def test_two_level_reduce_when_entries_exceed_budget(self) -> None:
        template = DigestTemplate(
            name="t", description="", extract_prompt="", reduce_prompt="merge"
        )
        rows = [{"note": "x " * 400, "item_id": i} for i in range(60)]  # ~12k tokens

        async def fake_chat(_model, messages):
            user = messages[-1]["content"]
            return "## merged\n- a [[item:1]]\n" if "Fragments to merge" in user else "## chunk\n"

        provider = AsyncMock()
        provider.chat.side_effect = fake_chat
        markdown, t_in, t_out = await _reduce(provider, "m", template, "d", rows)

        assert "merged" in markdown
        assert provider.chat.await_count >= 3  # >=2 chunk reduces + 1 merge
        assert t_in > 0 and t_out > 0


class TestPostprocess:
    def _item(self, item_id: int, username: str, url: str) -> Item:
        return Item(
            id=item_id,
            media_type="reel",
            caption="c",
            permalink=url,
            author=Author(id=1, username=username),
            media_files=[MediaFile(id=1, item_id=item_id, file_path="f", media_type="video")],
        )

    def test_drops_dangling_citation(self) -> None:
        items = {1: self._item(1, "real", "https://ig/p/1/")}
        out = _postprocess("- A thing [[item:1]][[item:77]]\n", items)
        assert "[[item:1]]" in out
        assert "77" not in out

    def test_rewrites_link_and_handle_from_db(self) -> None:
        items = {5: self._item(5, "true_account", "https://ig/p/REAL/")}
        md = "- **X** — note — @wrong_guess ([reel](https://wrong.example)) [[item:5]]\n"
        out = _postprocess(md, items)
        assert "https://ig/p/REAL/" in out
        assert "@true_account" in out
        assert "wrong" not in out

    def test_strips_malformed_markers_and_drops_dead_bullets(self) -> None:
        items = {1: self._item(1, "a", "u1")}
        md = "## Unresolved\n- [[item:]]\n- [[item:abc]]\n- kept [[item:1]]\n"
        out = _postprocess(md, items)
        assert "[[item:]]" not in out
        assert "[[item:abc]]" not in out
        assert "- kept [[item:1]]" in out
        assert out.count("\n- ") == 1  # the two dead bullets are gone

    def test_leaves_multi_citation_lines_alone(self) -> None:
        items = {1: self._item(1, "a", "u1"), 2: self._item(2, "b", "u2")}
        md = "- Shared theme [[item:1]] [[item:2]]\n"
        assert "[[item:1]]" in _postprocess(md, items)
        assert "[[item:2]]" in _postprocess(md, items)


class TestRunDigest:
    @pytest.mark.anyio
    async def test_map_reduce_end_to_end(
        self, tmp_config: Config, tmp_db_conn, monkeypatch
    ) -> None:
        i1 = _seed(tmp_config, "Read Dune by Herbert", category="books/manga", username="booksacc")
        i2 = _seed(tmp_config, "Also Neuromancer", category="books/manga", username="booksacc")

        with session_scope(tmp_config) as conn:
            digest_id = conn.execute(
                "INSERT INTO digests (name, template, status, selection_json, item_ids_json) "
                "VALUES ('t', 'book-titles', 'pending', '{}', ?)",
                (json.dumps([i1, i2]),),
            ).lastrowid

        provider = AsyncMock()
        provider.name = "ollama"
        # The two short items fit in one extract batch -> one extract call,
        # then the reduce call.
        provider.chat.side_effect = [
            json.dumps(
                [
                    {"title": "Dune", "author": "Herbert", "item_id": i1},
                    {"title": "Neuromancer", "author": "Gibson", "item_id": i2},
                ]
            ),
            f"## Sci-fi\n- **Dune** — Herbert [[item:{i1}]]\n"
            f"- **Neuromancer** — Gibson [[item:{i2}]]\n",
        ]
        monkeypatch.setattr(
            digest_engine, "get_provider", lambda task, config=None: (provider, "fake-model")
        )

        summary = await run_digest(digest_id, tmp_config)

        assert summary["status"] == "done"
        assert summary["entries"] == 2
        with session_scope(tmp_config) as conn:
            row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
        assert row["status"] == "done"
        assert "Dune" in row["markdown"]
        assert row["model"] == "fake-model"
        assert row["tokens_in"] > 0

    @pytest.mark.anyio
    async def test_nothing_extracted_raises(
        self, tmp_config: Config, tmp_db_conn, monkeypatch
    ) -> None:
        i1 = _seed(tmp_config, "something", category="books/manga")
        with session_scope(tmp_config) as conn:
            digest_id = conn.execute(
                "INSERT INTO digests (name, template, status, selection_json, item_ids_json) "
                "VALUES ('t', 'book-titles', 'pending', '{}', ?)",
                (json.dumps([i1]),),
            ).lastrowid

        provider = AsyncMock()
        provider.name = "ollama"
        provider.chat.return_value = "[]"
        monkeypatch.setattr(
            digest_engine, "get_provider", lambda task, config=None: (provider, "m")
        )

        with pytest.raises(DigestError):
            await run_digest(digest_id, tmp_config)
