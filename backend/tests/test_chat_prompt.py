"""Tests for `gramvault.chat.prompt`: context-block rendering and the
citation marker format contract."""

from __future__ import annotations

import re
from datetime import datetime

from gramvault.chat import prompt
from gramvault.chat.retrieval import RetrievalResult
from gramvault.models.schemas import Author, Item, MediaFile, MediaType, Tag


def _make_item(item_id: int, **overrides) -> Item:
    defaults: dict = {
        "id": item_id,
        "media_type": MediaType.PHOTO,
        "caption": "A lovely sunset over the beach",
        "author": Author(id=1, username="jane.doe"),
        "taken_at": datetime(2024, 3, 1),
        "tags": [Tag(id=1, name="sunset")],
        "media_files": [],
    }
    defaults.update(overrides)
    return Item(**defaults)


class TestCitationFormat:
    def test_marker_regex_matches_documented_format(self) -> None:
        text = "You saved this recipe [[item:42]] last year."
        match = re.search(prompt.CITATION_MARKER_REGEX, text)
        assert match is not None
        assert match.group(1) == "42"

    def test_marker_regex_matches_multiple_back_to_back(self) -> None:
        text = "See [[item:1]][[item:2]] for more."
        matches = re.findall(prompt.CITATION_MARKER_REGEX, text)
        assert matches == ["1", "2"]


class TestBuildContextBlock:
    def test_includes_item_id_author_date_caption_and_tags(self) -> None:
        item = _make_item(42)
        result = RetrievalResult(item_id=42, score=0.9, snippet=None)

        block = prompt.build_context_block({42: item}, [result])

        assert "Item 42" in block
        assert "@jane.doe" in block
        assert "2024-03-01" in block
        assert "sunset over the beach" in block
        assert "sunset" in block  # tag name

    def test_includes_on_screen_text_even_when_there_is_a_caption(self) -> None:
        """OCR text is often the whole point of a silent text-overlay reel,
        but only reached the model via the snippet fallback — i.e. never
        once the post had any caption at all."""
        item = _make_item(
            7,
            caption="#recipe #food",
            media_files=[
                MediaFile(
                    id=1,
                    item_id=7,
                    file_path="media/aa/a.mp4",
                    media_type="video",
                    ocr_text="3 eggs, 200g flour, bake 20 min",
                )
            ],
        )
        block = prompt.build_context_block(
            {7: item}, [RetrievalResult(item_id=7, score=0.9, snippet=None)]
        )
        assert "On-screen text: 3 eggs, 200g flour, bake 20 min" in block

    def test_numbers_items_in_result_order(self) -> None:
        item1 = _make_item(1, caption="first item")
        item2 = _make_item(2, caption="second item")
        results = [
            RetrievalResult(item_id=2, score=0.9),
            RetrievalResult(item_id=1, score=0.5),
        ]

        block = prompt.build_context_block({1: item1, 2: item2}, results)

        assert block.index("Item 2") < block.index("Item 1")

    def test_no_results_returns_explicit_note(self) -> None:
        block = prompt.build_context_block({}, [])
        assert "no retrieved items" in block.lower() or "couldn't find" in block.lower() or "no" in block.lower()

    def test_skips_missing_item_ids_gracefully(self) -> None:
        result = RetrievalResult(item_id=999, score=0.5)
        block = prompt.build_context_block({}, [result])
        assert "999" not in block


class TestBuildMessages:
    def test_includes_system_instruction_mentioning_citation_format(self) -> None:
        messages = prompt.build_messages([], {}, [], "hello")
        assert messages[0]["role"] == "system"
        assert "[[item:<item_id>]]" in messages[0]["content"]

    def test_preserves_history_order_and_appends_final_user_turn(self) -> None:
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello there"},
        ]
        messages = prompt.build_messages(history, {}, [], "what did I save?")

        assert messages[1:3] == history
        assert messages[-1]["role"] == "user"
        assert "what did I save?" in messages[-1]["content"]

    def test_final_user_message_includes_retrieved_context(self) -> None:
        item = _make_item(7, caption="a specific recipe")
        results = [RetrievalResult(item_id=7, score=0.8)]

        messages = prompt.build_messages([], {7: item}, results, "any recipes?")

        assert "Item 7" in messages[-1]["content"]
        assert "a specific recipe" in messages[-1]["content"]
        assert "any recipes?" in messages[-1]["content"]

    def test_history_windowed_to_budget_keeps_most_recent_turns(self) -> None:
        # R4: a long session must not resend everything — old turns fall
        # off once the budget is exhausted, most recent kept.
        history = [
            {"role": "user", "content": "old question " * 200},
            {"role": "assistant", "content": "old answer " * 200},
            {"role": "user", "content": "recent question"},
            {"role": "assistant", "content": "recent answer"},
        ]

        messages = prompt.build_messages(history, {}, [], "latest?", budget_tokens=50)

        kept = messages[1:-1]
        assert {"role": "user", "content": "recent question"} in kept
        assert {"role": "assistant", "content": "recent answer"} in kept
        assert not any("old question" in m["content"] for m in kept)

    def test_history_budget_is_independent_of_a_large_context_block(self) -> None:
        # A normal-sized retrieved-context block must not silently zero out
        # history by sharing a budget with it — history gets its own pool.
        items = {i: _make_item(i, caption="x " * 200) for i in range(6)}
        results = [RetrievalResult(item_id=i, score=0.5) for i in items]
        history = [{"role": "user", "content": "a short recent question"}]

        messages = prompt.build_messages(history, items, results, "latest?")

        assert history[0] in messages[1:-1]

    def test_default_budget_keeps_history_with_no_retrieved_context(self) -> None:
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello there"},
        ]
        messages = prompt.build_messages(history, {}, [], "what did I save?")
        assert messages[1:3] == history
