"""Tests for `gramvault.ai.classifier` — the keyword vote, the LLM
re-label batching/parsing, and the `categorize_items` orchestration
(with the `categorize` provider mocked at the module boundary)."""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock

import pytest

from gramvault.ai import classifier
from gramvault.ai.classifier import (
    _batches,
    _parse_labels,
    categorize_items,
    keyword_vote,
)
from gramvault.ai.providers.base import ChatResult
from gramvault.config import Config
from gramvault.db.session import DEFAULT_CATEGORIES, session_scope
from gramvault.models.schemas import Author, Item, MediaFile, Tag

CATEGORY_NAMES = [name for name, _ in DEFAULT_CATEGORIES]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _item(
    caption: str = "",
    *,
    item_id: int = 1,
    tags: list[str] | None = None,
    transcript: str | None = None,
    vision: str | None = None,
    ocr: str | None = None,
) -> Item:
    media_files: list[MediaFile] = []
    if transcript or vision or ocr:
        media_files.append(
            MediaFile(
                id=1,
                item_id=item_id,
                file_path="clip.mp4",
                media_type="video",
                transcript=transcript,
                vision_caption=vision,
                ocr_text=ocr,
            )
        )
    return Item(
        id=item_id,
        media_type="reel",
        caption=caption,
        author=Author(id=1, username="acc"),
        tags=[Tag(id=i, name=name, kind="hashtag") for i, name in enumerate(tags or [])],
        media_files=media_files,
    )


class TestKeywordVote:
    def test_picks_the_obvious_category(self) -> None:
        result = keyword_vote(
            _item("Leg day at the gym — 4 sets of squats and deadlifts"), CATEGORY_NAMES
        )
        assert result.category == "workouts"
        assert result.source == "keyword"
        assert result.confidence > 0.5

    def test_hashtag_override_wins_outright(self) -> None:
        result = keyword_vote(
            _item("loved every page of this", tags=["bookstagram"]), CATEGORY_NAMES
        )
        assert result.category == "books/manga"
        assert "#bookstagram" in result.reason

    def test_no_signal_is_other_at_low_confidence(self) -> None:
        result = keyword_vote(_item("hello everyone, hope you had a lovely weekend"), CATEGORY_NAMES)
        assert result.category == "other"
        assert result.confidence <= 0.1

    def test_single_weak_hit_needs_review(self) -> None:
        # "posture" alone scores 1 -> below _MIN_SCORE -> capped confidence.
        result = keyword_vote(_item("a note on posture"), CATEGORY_NAMES)
        assert result.confidence <= 0.5

    def test_reads_transcript_and_on_screen_text(self) -> None:
        result = keyword_vote(
            _item("✨", transcript="today we are baking a chocolate cake, preheat the oven"),
            CATEGORY_NAMES,
        )
        assert result.category == "recipes"

    def test_falls_back_when_other_is_absent(self) -> None:
        names = [n for n in CATEGORY_NAMES if n != "other"]
        result = keyword_vote(_item("nothing to categorise here really"), names)
        assert result.category in names


class TestBatching:
    def test_respects_the_token_budget(self) -> None:
        lines = ["x" * 20_000] * 3  # ~5000 tokens each, budget is 6000
        assert len(list(_batches(lines))) == 3

    def test_small_lines_share_one_batch(self) -> None:
        assert list(_batches(["#1 a", "#2 b", "#3 c"])) == [["#1 a", "#2 b", "#3 c"]]

    def test_empty(self) -> None:
        assert list(_batches([])) == []


class TestParseLabels:
    def test_strips_a_code_fence(self) -> None:
        parsed = _parse_labels('```json\n{"7": {"category": "memes"}}\n```')
        assert parsed == {"7": {"category": "memes"}}

    def test_ignores_prose_around_the_object(self) -> None:
        parsed = _parse_labels('Sure! Here you go:\n{"1": {"category": "recipes"}}\nHope that helps')
        assert parsed["1"]["category"] == "recipes"

    def test_rejects_a_reply_with_no_object(self) -> None:
        with pytest.raises(ValueError, match="no JSON object"):
            _parse_labels("I could not classify these.")


def _seed(conn: sqlite3.Connection, caption: str, *, source: str | None = None) -> int:
    item_id = conn.execute(
        "INSERT INTO items (media_type, caption, category_source) VALUES ('reel', ?, ?)",
        (caption, source),
    ).lastrowid
    conn.commit()
    assert item_id is not None
    return item_id


@pytest.mark.anyio
class TestCategorizeItems:
    async def test_keyword_method_writes_labels(self, tmp_config: Config, tmp_db_conn) -> None:
        with session_scope(tmp_config) as conn:
            gym = _seed(conn, "gym workout, squats and deadlifts, leg day")
            cake = _seed(conn, "recipe: preheat the oven and bake the dough")

        summary = await categorize_items([gym, cake], "keyword", tmp_config)

        assert summary["processed"] == 2
        assert summary["keyword"] == 2
        assert summary["by_category"] == {"workouts": 1, "recipes": 1}
        with session_scope(tmp_config) as conn:
            rows = {
                r["id"]: (r["name"], r["category_source"], r["category_reason"])
                for r in conn.execute(
                    "SELECT items.id, categories.name, items.category_source, items.category_reason "
                    "FROM items JOIN categories ON categories.id = items.category_id"
                )
            }
        assert rows[gym][0] == "workouts"
        assert rows[gym][1] == "keyword"
        assert rows[gym][2]  # a non-empty reason

    async def test_never_overwrites_a_manual_label(self, tmp_config: Config, tmp_db_conn) -> None:
        with session_scope(tmp_config) as conn:
            item_id = _seed(conn, "gym workout leg day", source="manual")
            conn.execute(
                "UPDATE items SET category_id = (SELECT id FROM categories WHERE name = 'beauty') "
                "WHERE id = ?",
                (item_id,),
            )
            conn.commit()

        summary = await categorize_items([item_id], "keyword", tmp_config)

        assert summary["skipped_manual"] == 1
        assert summary["processed"] == 0
        with session_scope(tmp_config) as conn:
            name = conn.execute(
                "SELECT categories.name FROM items JOIN categories ON categories.id = items.category_id "
                "WHERE items.id = ?",
                (item_id,),
            ).fetchone()["name"]
        assert name == "beauty"

    async def test_llm_method_uses_the_model_label(
        self, tmp_config: Config, tmp_db_conn, monkeypatch
    ) -> None:
        with session_scope(tmp_config) as conn:
            item_id = _seed(conn, "an ambiguous caption the keywords will call other")

        fake_provider = AsyncMock()
        fake_provider.complete.return_value = ChatResult(
            text=f'{{"{item_id}": {{"category": "astrology", "confidence": 0.82, "reason": "birth chart"}}}}'
        )
        monkeypatch.setattr(
            classifier, "get_provider", lambda task, config=None: (fake_provider, "fake-model")
        )

        summary = await categorize_items([item_id], "llm", tmp_config)

        assert summary["llm"] == 1
        with session_scope(tmp_config) as conn:
            row = conn.execute(
                "SELECT categories.name, items.category_source, items.category_confidence "
                "FROM items JOIN categories ON categories.id = items.category_id WHERE items.id = ?",
                (item_id,),
            ).fetchone()
        assert row["name"] == "astrology"
        assert row["category_source"] == "llm"
        assert row["category_confidence"] == 0.82

    async def test_llm_falls_back_to_keyword_on_unparseable_reply(
        self, tmp_config: Config, tmp_db_conn, monkeypatch
    ) -> None:
        with session_scope(tmp_config) as conn:
            item_id = _seed(conn, "gym workout, 5 sets of squats, leg day")

        fake_provider = AsyncMock()
        fake_provider.complete.return_value = ChatResult(text="I'm not able to help with that.")
        monkeypatch.setattr(
            classifier, "get_provider", lambda task, config=None: (fake_provider, "fake-model")
        )

        summary = await categorize_items([item_id], "llm", tmp_config)

        assert summary["keyword"] == 1
        assert summary["llm"] == 0
        with session_scope(tmp_config) as conn:
            name = conn.execute(
                "SELECT categories.name FROM items JOIN categories ON categories.id = items.category_id "
                "WHERE items.id = ?",
                (item_id,),
            ).fetchone()["name"]
        assert name == "workouts"

    async def test_progress_callback_reports_completion(
        self, tmp_config: Config, tmp_db_conn
    ) -> None:
        with session_scope(tmp_config) as conn:
            ids = [_seed(conn, "gym workout leg day"), _seed(conn, "recipe preheat oven bake")]

        seen: list[tuple[int, int]] = []
        await categorize_items(ids, "keyword", tmp_config, progress_cb=lambda d, t: seen.append((d, t)))

        assert seen[-1] == (2, 2)

    async def test_truncated_reply_splits_the_batch_instead_of_dropping_it(
        self, tmp_config: Config, tmp_db_conn, monkeypatch
    ) -> None:
        """R5: a reply that hits max_tokens must not fall the whole batch
        back to keyword guesses — split in half and retry each half."""
        with session_scope(tmp_config) as conn:
            id_a = _seed(conn, "an ambiguous caption the keywords will call other")
            id_b = _seed(conn, "another ambiguous one, also other by keyword")

        fake_provider = AsyncMock()
        fake_provider.complete.side_effect = [
            ChatResult(text="", truncated=True),  # full batch: truncated
            ChatResult(text=f'{{"{id_a}": {{"category": "astrology", "confidence": 0.9}}}}'),
            ChatResult(text=f'{{"{id_b}": {{"category": "recipes", "confidence": 0.9}}}}'),
        ]
        monkeypatch.setattr(
            classifier, "get_provider", lambda task, config=None: (fake_provider, "fake-model")
        )

        summary = await categorize_items([id_a, id_b], "llm", tmp_config)

        assert summary["llm"] == 2
        assert fake_provider.complete.await_count == 3
        with session_scope(tmp_config) as conn:
            names = {
                r["id"]: r["name"]
                for r in conn.execute(
                    "SELECT items.id AS id, categories.name AS name FROM items "
                    "JOIN categories ON categories.id = items.category_id"
                )
            }
        assert names[id_a] == "astrology"
        assert names[id_b] == "recipes"
