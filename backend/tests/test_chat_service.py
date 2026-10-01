"""Tests for `gramvault.chat.service`: persistence, citation parsing, the
streaming RAG flow, and semantic search.

Ollama (`gramvault.ai.ollama_client`) and the retrieval layer are mocked
throughout — no real Ollama server, model, or ChromaDB instance required.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from gramvault.ai.ollama_client import ModelNotPulledError, OllamaNotRunningError
from gramvault.chat import service
from gramvault.chat.retrieval import RetrievalResult
from gramvault.config import Config
from gramvault.models.schemas import ChatRole, Item, MediaType


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _fake_stream_chat(messages, model=None, config=None):
    for chunk in ["Sure — ", "here's what I found [[item:1]] and also [[item:999]]."]:
        yield chunk


async def _raising_stream_chat(messages, model=None, config=None):
    raise OllamaNotRunningError("http://localhost:11434")
    yield  # pragma: no cover - makes this an async generator function


class TestSessionPersistence:
    def test_create_and_get_session(self, tmp_db_conn: sqlite3.Connection) -> None:
        session = service.create_session(tmp_db_conn, title="My chat")
        assert session.id is not None
        assert session.title == "My chat"

        fetched = service.get_session(tmp_db_conn, session.id)
        assert fetched is not None
        assert fetched.id == session.id

    def test_get_missing_session_returns_none(self, tmp_db_conn: sqlite3.Connection) -> None:
        assert service.get_session(tmp_db_conn, 9999) is None

    def test_list_sessions_most_recent_first(self, tmp_db_conn: sqlite3.Connection) -> None:
        first = service.create_session(tmp_db_conn, title="first")
        second = service.create_session(tmp_db_conn, title="second")

        sessions = service.list_sessions(tmp_db_conn)

        assert [s.id for s in sessions][:2] == [second.id, first.id]

    def test_list_messages_includes_citations(self, tmp_db_conn: sqlite3.Connection) -> None:
        session = service.create_session(tmp_db_conn)
        message_id = service._persist_message(tmp_db_conn, session.id, ChatRole.ASSISTANT, "hi [[item:1]]")
        tmp_db_conn.execute(
            "INSERT INTO items (media_type, caption) VALUES ('photo', 'x')"
        )
        tmp_db_conn.commit()
        tmp_db_conn.execute(
            "INSERT INTO chat_citations (message_id, item_id, snippet) VALUES (?, 1, 'snip')",
            (message_id,),
        )
        tmp_db_conn.commit()

        messages = service.list_messages(tmp_db_conn, session.id)

        assert len(messages) == 1
        assert messages[0].citations[0].item_id == 1
        assert messages[0].citations[0].snippet == "snip"


class TestParseCitations:
    def test_extracts_valid_citations_in_order_deduped(self) -> None:
        text = "See [[item:2]] and [[item:1]] and again [[item:2]]."
        result = service.parse_citations(text, valid_item_ids={1, 2})
        assert result == [2, 1]

    def test_drops_hallucinated_ids_not_in_valid_set(self) -> None:
        text = "See [[item:1]] and [[item:42]]."
        result = service.parse_citations(text, valid_item_ids={1})
        assert result == [1]

    def test_no_markers_returns_empty(self) -> None:
        assert service.parse_citations("no citations here", valid_item_ids={1, 2}) == []


class TestEnsureOllamaReady:
    @pytest.mark.anyio
    async def test_checks_running_and_both_models(self, tmp_config: Config) -> None:
        with (
            patch("gramvault.ai.ollama_client.ensure_running", new_callable=AsyncMock) as mock_running,
            patch("gramvault.ai.ollama_client.ensure_model_pulled", new_callable=AsyncMock
            ) as mock_pulled,
        ):
            await service.ensure_ollama_ready(tmp_config)

        # Each provider checks readiness independently: the server-up check
        # runs once per provider, the model-pull check once per model.
        mock_running.assert_awaited()
        checked_models = {call.args[0] for call in mock_pulled.await_args_list}
        assert checked_models == {tmp_config.models.chat_model, tmp_config.models.embedding_model}

    @pytest.mark.anyio
    async def test_api_chat_provider_gates_on_the_key_not_ollama(self) -> None:
        from gramvault.ai.errors import ProviderNotReadyError
        from gramvault.config import Config

        cfg = Config.model_validate(
            {
                "ai": {"chat": {"provider": "anthropic", "model": "claude-sonnet-5"}},
                "providers": {"anthropic": {"kind": "anthropic"}},  # no api_key
            }
        )
        with (
            patch("gramvault.ai.ollama_client.ensure_running", new_callable=AsyncMock),
            pytest.raises(ProviderNotReadyError, match="Anthropic API key"),
        ):
            await service.ensure_ollama_ready(cfg)


class TestStreamMessage:
    @pytest.mark.anyio
    async def test_streams_tokens_persists_message_and_valid_citations_only(
        self, tmp_config: Config
    ) -> None:
        from gramvault.db.session import get_connection, init_db

        conn = get_connection(tmp_config)
        init_db(conn)
        session = service.create_session(conn, title="test")
        conn.execute(
            "INSERT INTO items (id, media_type, caption) VALUES (1, 'photo', 'a pasta recipe')"
        )
        conn.commit()
        item = Item(id=1, media_type=MediaType.PHOTO, caption="a pasta recipe")
        conn.close()

        fake_result = RetrievalResult(item_id=1, score=0.9, snippet="a pasta recipe")

        with (
            patch.object(
                service.retrieval, "hybrid_search", new_callable=AsyncMock
            ) as mock_hybrid,
            patch.object(service.retrieval, "fetch_items", return_value={1: item}),
            patch("gramvault.ai.ollama_client.stream_chat", new=_fake_stream_chat),
        ):
            mock_hybrid.return_value = [fake_result]

            events = [
                event
                async for event in service.stream_message(session.id, "any pasta?", config=tmp_config)
            ]

        source_events = [e for e in events if e["event"] == "sources"]
        token_events = [e for e in events if e["event"] == "token"]
        done_events = [e for e in events if e["event"] == "done"]
        assert len(done_events) == 1
        assert not [e for e in events if e["event"] == "error"]

        # sources arrives once, before the first token, and carries what
        # retrieval actually found (not filtered down to what got cited).
        assert events.index(source_events[0]) < events.index(token_events[0])
        assert json.loads(source_events[0]["data"]) == {
            "results": [{"item_id": 1, "media_file_id": None, "snippet": "a pasta recipe", "score": 0.9}]
        }

        full_content = "".join(json.loads(e["data"])["content"] for e in token_events)
        assert "Sure" in full_content

        done_payload = json.loads(done_events[0]["data"])
        # item 1 was actually retrieved -> kept; item 999 was not -> dropped
        cited_ids = {c["item_id"] for c in done_payload["citations"]}
        assert cited_ids == {1}

        # verify persistence: user + assistant messages, with citation row
        conn = get_connection(tmp_config)
        messages = service.list_messages(conn, session.id)
        conn.close()
        assert [m.role for m in messages] == [ChatRole.USER, ChatRole.ASSISTANT]
        assert messages[0].content == "any pasta?"
        assert messages[1].citations[0].item_id == 1

    @pytest.mark.anyio
    async def test_disconnect_mid_stream_persists_partial_reply(
        self, tmp_config: Config
    ) -> None:
        """R7: a client disconnect (the SSE layer calling aclose() on the
        generator) must not lose the assistant text streamed so far, and
        must not itself raise (a swallowed exception inside the `finally`
        would surface as a RuntimeError from aclose())."""
        from gramvault.db.session import get_connection, init_db

        conn = get_connection(tmp_config)
        init_db(conn)
        session = service.create_session(conn, title="test")
        conn.close()

        with (
            patch.object(
                service.retrieval, "hybrid_search", new_callable=AsyncMock, return_value=[]
            ),
            patch.object(service.retrieval, "fetch_items", return_value={}),
            patch("gramvault.ai.ollama_client.stream_chat", new=_fake_stream_chat),
        ):
            gen = service.stream_message(session.id, "any pasta?", config=tmp_config)
            sources = await gen.__anext__()
            assert sources["event"] == "sources"
            first_token = await gen.__anext__()
            assert first_token["event"] == "token"
            await gen.aclose()  # simulates the client going away mid-stream

        conn = get_connection(tmp_config)
        messages = service.list_messages(conn, session.id)
        conn.close()

        assert [m.role for m in messages] == [ChatRole.USER, ChatRole.ASSISTANT]
        # Only the first chunk had been yielded before aclose(); that's all
        # that should have been persisted.
        assert messages[1].content == "Sure — "

    @pytest.mark.anyio
    async def test_retrieval_failure_leaves_no_dangling_user_message(
        self, tmp_config: Config
    ) -> None:
        """R7: a retrieval error must not persist the user's turn — no
        question with a guaranteed-missing reply left in history. It ends
        the stream with an `error` event rather than escaping the generator
        (which closed the SSE stream with no terminal event)."""
        from gramvault.db.session import get_connection, init_db

        conn = get_connection(tmp_config)
        init_db(conn)
        session = service.create_session(conn, title="test")
        conn.close()

        with patch.object(
            service.retrieval,
            "hybrid_search",
            new_callable=AsyncMock,
            side_effect=RuntimeError("chroma is on fire"),
        ):
            events = [
                event
                async for event in service.stream_message(session.id, "any pasta?", config=tmp_config)
            ]

        assert [e["event"] for e in events] == ["error"]
        assert "chroma is on fire" in json.loads(events[0]["data"])["detail"]

        conn = get_connection(tmp_config)
        messages = service.list_messages(conn, session.id)
        conn.close()
        assert messages == []

    @pytest.mark.anyio
    async def test_ollama_not_running_yields_error_event_not_exception(
        self, tmp_config: Config
    ) -> None:
        from gramvault.db.session import get_connection, init_db

        conn = get_connection(tmp_config)
        init_db(conn)
        session = service.create_session(conn, title="test")
        conn.close()

        with (
            patch.object(service.retrieval, "hybrid_search", new_callable=AsyncMock, return_value=[]),
            patch.object(service.retrieval, "fetch_items", return_value={}),
            patch("gramvault.ai.ollama_client.stream_chat", new=_raising_stream_chat),
        ):
            events = [
                event
                async for event in service.stream_message(session.id, "hello", config=tmp_config)
            ]

        assert events[-1]["event"] == "error"
        assert "Ollama" in json.loads(events[-1]["data"])["detail"]

    @pytest.mark.anyio
    async def test_unexpected_mid_stream_failure_ends_with_error_event(
        self, tmp_config: Config
    ) -> None:
        """A provider error that isn't a readiness failure (an HTTP 529 /
        500 mid-reply) used to escape the generator: the SSE stream closed
        with no `done`/`error` event and the chat UI waited forever."""
        from gramvault.db.session import get_connection, init_db

        conn = get_connection(tmp_config)
        init_db(conn)
        session = service.create_session(conn, title="test")
        conn.close()

        async def _failing_stream(messages, model=None, config=None):
            yield "partial "
            raise RuntimeError("upstream 529 overloaded")

        with (
            patch.object(service.retrieval, "hybrid_search", new_callable=AsyncMock, return_value=[]),
            patch.object(service.retrieval, "fetch_items", return_value={}),
            patch("gramvault.ai.ollama_client.stream_chat", new=_failing_stream),
        ):
            events = [
                event
                async for event in service.stream_message(session.id, "hello", config=tmp_config)
            ]

        assert [e["event"] for e in events] == ["sources", "token", "error"]
        assert "529" in json.loads(events[-1]["data"])["detail"]
        conn = get_connection(tmp_config)
        try:
            contents = [m.content for m in service.list_messages(conn, session.id)]
        finally:
            conn.close()
        assert contents == ["hello", "partial "]  # the partial reply is still kept


class TestSemanticSearch:
    @pytest.mark.anyio
    async def test_returns_item_score_snippet_dicts(self, tmp_config: Config) -> None:
        item = Item(id=5, media_type=MediaType.PHOTO, caption="beach sunset")
        fake_result = RetrievalResult(item_id=5, score=0.75, snippet="beach sunset")

        with (
            patch.object(
                service.retrieval,
                "hybrid_search_faceted",
                new_callable=AsyncMock,
                return_value=([fake_result], 1),
            ),
            patch.object(service.retrieval, "fetch_items", return_value={5: item}),
        ):
            results, total = await service.semantic_search("beach", top_k=5, config=tmp_config)

        assert len(results) == 1
        assert results[0]["item"].id == 5
        assert results[0]["score"] == 0.75
        assert results[0]["snippet"] == "beach sunset"
        assert total == 1

    @pytest.mark.anyio
    async def test_propagates_ollama_errors_for_route_to_translate(self, tmp_config: Config) -> None:
        with (
            patch.object(
                service.retrieval,
                "hybrid_search_faceted",
                new_callable=AsyncMock,
                side_effect=ModelNotPulledError("nomic-embed-text"),
            ),
            pytest.raises(ModelNotPulledError),
        ):
            await service.semantic_search("beach", config=tmp_config)
