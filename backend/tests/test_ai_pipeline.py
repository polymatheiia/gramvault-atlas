"""Tests for `gramvault.ai.pipeline`: per-item AI enrichment orchestration
and its resumability guarantees.

`ollama_client`, `keyframes`, `transcription`, and `embedding_store` are all
mocked at the module boundary — no real Ollama server, ffmpeg binary, or
whisper model is required.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from gramvault.ai import pipeline
from gramvault.ai.ollama_client import OllamaNotRunningError
from gramvault.ai.transcription import TranscriptionResult
from gramvault.chat.retrieval import fetch_items
from gramvault.config import Config
from gramvault.db.session import session_scope
from gramvault.models.schemas import EnrichmentStatus


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _insert_item(
    conn: sqlite3.Connection,
    *,
    media_type: str = "photo",
    caption: str | None = "a caption",
) -> int:
    item_id = conn.execute(
        "INSERT INTO items (media_type, caption) VALUES (?, ?)",
        (media_type, caption),
    ).lastrowid
    conn.commit()
    assert item_id is not None
    return item_id


def _insert_media_file(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    file_media_type: str = "photo",
    file_path: str = "photo.jpg",
    vision_caption: str | None = None,
    transcript: str | None = None,
    sequence_index: int = 0,
) -> int:
    media_file_id = conn.execute(
        """
        INSERT INTO media_files
            (item_id, file_path, media_type, sequence_index, vision_caption, transcript)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (item_id, file_path, file_media_type, sequence_index, vision_caption, transcript),
    ).lastrowid
    conn.commit()
    assert media_file_id is not None
    return media_file_id


def _get_item(config: Config, item_id: int):
    with session_scope(config) as conn:
        return fetch_items(conn, [item_id])[item_id]


class TestProcessItemPhoto:
    @pytest.mark.anyio
    async def test_full_enrichment_captions_photo_and_embeds(self, tmp_config: Config) -> None:
        with session_scope(tmp_config) as conn:
            item_id = _insert_item(conn, media_type="photo")
            _insert_media_file(conn, item_id, file_media_type="photo", file_path="a.jpg")

        with (
            patch("gramvault.ai.ollama_client.ensure_running", new_callable=AsyncMock),
            patch("gramvault.ai.ollama_client.ensure_model_pulled", new_callable=AsyncMock),
            patch("gramvault.ai.ollama_client.caption_image",
                new_callable=AsyncMock,
                return_value="a nice photo",
            ) as mock_caption,
            patch.object(pipeline.keyframes, "extract_keyframes") as mock_extract,
            patch.object(pipeline.transcription, "transcribe") as mock_transcribe,
            patch("gramvault.ai.ollama_client.embed", new_callable=AsyncMock, return_value=[0.1, 0.2]
            ) as mock_embed,
            patch.object(pipeline.embedding_store, "upsert_item") as mock_upsert,
        ):
            await pipeline.process_item(item_id, config=tmp_config)

        item = _get_item(tmp_config, item_id)
        assert item.enrichment_status == EnrichmentStatus.DONE
        assert item.media_files[0].vision_caption == "a nice photo"
        mock_caption.assert_awaited_once()
        mock_extract.assert_not_called()
        mock_transcribe.assert_not_called()
        mock_embed.assert_awaited()
        mock_upsert.assert_called()

    @pytest.mark.anyio
    async def test_no_content_document_skips_embedding(self, tmp_config: Config) -> None:
        # No caption, no author, no tags, no media files -> build_content_document
        # returns "" -> _embed_and_upsert should short-circuit without embedding.
        with session_scope(tmp_config) as conn:
            item_id = _insert_item(conn, media_type="photo", caption=None)

        with (
            patch("gramvault.ai.ollama_client.ensure_running", new_callable=AsyncMock),
            patch("gramvault.ai.ollama_client.ensure_model_pulled", new_callable=AsyncMock),
            patch("gramvault.ai.ollama_client.embed", new_callable=AsyncMock
            ) as mock_embed,
            patch.object(pipeline.embedding_store, "upsert_item") as mock_upsert,
        ):
            await pipeline.process_item(item_id, config=tmp_config)

        item = _get_item(tmp_config, item_id)
        assert item.enrichment_status == EnrichmentStatus.DONE
        mock_embed.assert_not_awaited()
        mock_upsert.assert_not_called()


class TestProcessItemVideo:
    @pytest.mark.anyio
    async def test_full_enrichment_extracts_keyframes_captions_and_transcribes(
        self, tmp_config: Config, tmp_path
    ) -> None:
        with session_scope(tmp_config) as conn:
            item_id = _insert_item(conn, media_type="video")
            _insert_media_file(conn, item_id, file_media_type="video", file_path="clip.mp4")

        fake_frames = [tmp_path / "frame1.jpg", tmp_path / "frame2.jpg"]
        fake_transcript = TranscriptionResult(text="someone talking about pasta")

        with (
            patch("gramvault.ai.ollama_client.ensure_running", new_callable=AsyncMock),
            patch("gramvault.ai.ollama_client.ensure_model_pulled", new_callable=AsyncMock),
            patch.object(
                pipeline.keyframes, "extract_keyframes", return_value=fake_frames
            ) as mock_extract,
            patch("gramvault.ai.ollama_client.caption_image",
                new_callable=AsyncMock,
                side_effect=["frame one caption", "frame two caption"],
            ) as mock_caption,
            patch.object(
                pipeline.transcription, "transcribe", return_value=fake_transcript
            ) as mock_transcribe,
            patch("gramvault.ai.ollama_client.embed", new_callable=AsyncMock, return_value=[0.1]
            ),
            patch.object(pipeline.embedding_store, "upsert_item") as mock_upsert,
        ):
            await pipeline.process_item(item_id, config=tmp_config)

        item = _get_item(tmp_config, item_id)
        assert item.enrichment_status == EnrichmentStatus.DONE
        media_file = item.media_files[0]
        assert media_file.vision_caption == "frame one caption | frame two caption"
        assert media_file.transcript == "someone talking about pasta"
        mock_extract.assert_called_once()
        assert mock_caption.await_count == 2
        mock_transcribe.assert_called_once()
        mock_upsert.assert_called()

    @pytest.mark.anyio
    async def test_video_with_no_frames_yields_empty_caption(
        self, tmp_config: Config
    ) -> None:
        with session_scope(tmp_config) as conn:
            item_id = _insert_item(conn, media_type="video")
            _insert_media_file(conn, item_id, file_media_type="video", file_path="clip.mp4")

        fake_transcript = TranscriptionResult(text="")

        with (
            patch("gramvault.ai.ollama_client.ensure_running", new_callable=AsyncMock),
            patch("gramvault.ai.ollama_client.ensure_model_pulled", new_callable=AsyncMock),
            patch.object(pipeline.keyframes, "extract_keyframes", return_value=[]),
            patch("gramvault.ai.ollama_client.caption_image", new_callable=AsyncMock
            ) as mock_caption,
            patch.object(pipeline.transcription, "transcribe", return_value=fake_transcript),
            patch("gramvault.ai.ollama_client.embed", new_callable=AsyncMock),
            patch.object(pipeline.embedding_store, "upsert_item"),
        ):
            await pipeline.process_item(item_id, config=tmp_config)

        item = _get_item(tmp_config, item_id)
        assert item.media_files[0].vision_caption == ""
        mock_caption.assert_not_awaited()

    @pytest.mark.anyio
    async def test_skip_vision_still_transcribes_and_embeds(self, tmp_config: Config) -> None:
        """`--skip-vision` exists for libraries where the local vision model
        is wrong often enough to poison retrieval. Everything else — the
        transcript, the content document, the embedding — must still run,
        and the item must still reach `done`."""
        with session_scope(tmp_config) as conn:
            item_id = _insert_item(conn, media_type="video")
            _insert_media_file(conn, item_id, file_media_type="video", file_path="clip.mp4")

        with (
            patch("gramvault.ai.ollama_client.ensure_running", new_callable=AsyncMock),
            patch("gramvault.ai.ollama_client.ensure_model_pulled", new_callable=AsyncMock
            ) as mock_pull,
            patch.object(pipeline.keyframes, "extract_keyframes") as mock_extract,
            patch("gramvault.ai.ollama_client.caption_image", new_callable=AsyncMock
            ) as mock_caption,
            patch.object(
                pipeline.transcription,
                "transcribe",
                return_value=TranscriptionResult(text="a recipe for chili lime chicken"),
            ) as mock_transcribe,
            patch("gramvault.ai.ollama_client.embed", new_callable=AsyncMock, return_value=[0.1]
            ),
            patch.object(pipeline.embedding_store, "upsert_item") as mock_upsert,
        ):
            await pipeline.process_item(item_id, config=tmp_config, skip_vision=True)

        item = _get_item(tmp_config, item_id)
        assert item.enrichment_status == EnrichmentStatus.DONE
        assert item.media_files[0].transcript == "a recipe for chili lime chicken"
        assert item.media_files[0].vision_caption is None  # left for a later pass
        mock_caption.assert_not_awaited()
        mock_extract.assert_not_called()  # no ffmpeg work either
        mock_transcribe.assert_called_once()
        mock_upsert.assert_called()
        # Only the embedding model is needed — don't pull a vision model we won't use.
        pulled = {call.args[0] for call in mock_pull.await_args_list}
        assert pulled == {tmp_config.models.embedding_model}


class TestResumability:
    @pytest.mark.anyio
    async def test_skips_already_captioned_video_but_still_transcribes(
        self, tmp_config: Config
    ) -> None:
        with session_scope(tmp_config) as conn:
            item_id = _insert_item(conn, media_type="video")
            _insert_media_file(
                conn,
                item_id,
                file_media_type="video",
                file_path="clip.mp4",
                vision_caption="already captioned",
                transcript=None,
            )

        fake_transcript = TranscriptionResult(text="new transcript")

        with (
            patch("gramvault.ai.ollama_client.ensure_running", new_callable=AsyncMock),
            patch("gramvault.ai.ollama_client.ensure_model_pulled", new_callable=AsyncMock),
            patch.object(pipeline.keyframes, "extract_keyframes") as mock_extract,
            patch("gramvault.ai.ollama_client.caption_image", new_callable=AsyncMock
            ) as mock_caption,
            patch.object(
                pipeline.transcription, "transcribe", return_value=fake_transcript
            ) as mock_transcribe,
            patch("gramvault.ai.ollama_client.embed", new_callable=AsyncMock),
            patch.object(pipeline.embedding_store, "upsert_item"),
        ):
            await pipeline.process_item(item_id, config=tmp_config)

        # Already-done step (vision captioning) must NOT be redone.
        mock_extract.assert_not_called()
        mock_caption.assert_not_awaited()
        # Remaining step (transcription) must still run.
        mock_transcribe.assert_called_once()

        item = _get_item(tmp_config, item_id)
        assert item.media_files[0].vision_caption == "already captioned"
        assert item.media_files[0].transcript == "new transcript"
        assert item.enrichment_status == EnrichmentStatus.DONE

    @pytest.mark.anyio
    async def test_skips_already_captioned_photo_entirely(self, tmp_config: Config) -> None:
        with session_scope(tmp_config) as conn:
            item_id = _insert_item(conn, media_type="photo")
            _insert_media_file(
                conn,
                item_id,
                file_media_type="photo",
                file_path="a.jpg",
                vision_caption="already captioned",
            )

        with (
            patch("gramvault.ai.ollama_client.ensure_running", new_callable=AsyncMock),
            patch("gramvault.ai.ollama_client.ensure_model_pulled", new_callable=AsyncMock),
            patch("gramvault.ai.ollama_client.caption_image", new_callable=AsyncMock
            ) as mock_caption,
            patch("gramvault.ai.ollama_client.embed", new_callable=AsyncMock),
            patch.object(pipeline.embedding_store, "upsert_item"),
        ):
            await pipeline.process_item(item_id, config=tmp_config)

        mock_caption.assert_not_awaited()
        item = _get_item(tmp_config, item_id)
        assert item.media_files[0].vision_caption == "already captioned"
        assert item.enrichment_status == EnrichmentStatus.DONE

    @pytest.mark.anyio
    async def test_fully_done_item_does_not_require_vision_model_check(
        self, tmp_config: Config
    ) -> None:
        # needs_vision should be False when every media file already has a
        # vision_caption -> ensure_model_pulled should only be checked for
        # the embedding model, not the vision model.
        with session_scope(tmp_config) as conn:
            item_id = _insert_item(conn, media_type="photo")
            _insert_media_file(
                conn,
                item_id,
                file_media_type="photo",
                file_path="a.jpg",
                vision_caption="already captioned",
            )

        with (
            patch("gramvault.ai.ollama_client.ensure_running", new_callable=AsyncMock),
            patch("gramvault.ai.ollama_client.ensure_model_pulled", new_callable=AsyncMock
            ) as mock_ensure_pulled,
            patch("gramvault.ai.ollama_client.caption_image", new_callable=AsyncMock),
            patch("gramvault.ai.ollama_client.embed", new_callable=AsyncMock),
            patch.object(pipeline.embedding_store, "upsert_item"),
        ):
            await pipeline.process_item(item_id, config=tmp_config)

        checked_models = {call.args[0] for call in mock_ensure_pulled.await_args_list}
        assert checked_models == {tmp_config.models.embedding_model}


class TestProcessItemOcr:
    @pytest.mark.anyio
    async def test_ocr_step_writes_ocr_text_and_stamps_attempted(
        self, tmp_config: Config
    ) -> None:
        with session_scope(tmp_config) as conn:
            item_id = _insert_item(conn, media_type="photo", caption="#recipe #foryou")
            _insert_media_file(conn, item_id, file_media_type="photo", file_path="slide.jpg")

        with (
            patch("gramvault.ai.ollama_client.ensure_running", new_callable=AsyncMock),
            patch("gramvault.ai.ollama_client.ensure_model_pulled", new_callable=AsyncMock),
            patch(
                "gramvault.ai.ollama_client.caption_image",
                new_callable=AsyncMock,
                return_value="BUY MILK EGGS FLOUR",
            ) as mock_caption,
            patch("gramvault.ai.ollama_client.embed", new_callable=AsyncMock),
            patch.object(pipeline.embedding_store, "upsert_item"),
        ):
            await pipeline.process_item(
                item_id,
                config=tmp_config,
                steps=pipeline.PipelineSteps(
                    transcribe=False, ocr=True, vision_caption=False, embed=False
                ),
                ocr_scope="all_media",
            )

        media_file = _get_item(tmp_config, item_id).media_files[0]
        assert media_file.ocr_text == "BUY MILK EGGS FLOUR"
        assert media_file.ocr_attempted_at is not None
        assert media_file.ocr_model
        mock_caption.assert_awaited_once()
        # An OCR-only pass over a pending item leaves its status alone.
        assert _get_item(tmp_config, item_id).enrichment_status == EnrichmentStatus.PENDING

    @pytest.mark.anyio
    async def test_discarded_ocr_read_still_stamps_attempted_so_it_is_not_retried(
        self, tmp_config: Config
    ) -> None:
        with session_scope(tmp_config) as conn:
            item_id = _insert_item(conn, media_type="photo", caption="#foryou")
            _insert_media_file(conn, item_id, file_media_type="photo", file_path="slide.jpg")

        steps = pipeline.PipelineSteps(
            transcribe=False, ocr=True, vision_caption=False, embed=False
        )
        with (
            patch("gramvault.ai.ollama_client.ensure_running", new_callable=AsyncMock),
            patch("gramvault.ai.ollama_client.ensure_model_pulled", new_callable=AsyncMock),
            patch(
                "gramvault.ai.ollama_client.caption_image",
                new_callable=AsyncMock,
                return_value="Купить молоко и яйца",  # Cyrillic -> discarded
            ) as mock_caption,
        ):
            await pipeline.process_item(
                item_id, config=tmp_config, steps=steps, ocr_scope="all_media"
            )
            media_file = _get_item(tmp_config, item_id).media_files[0]
            assert media_file.ocr_text is None
            assert media_file.ocr_attempted_at is not None

            # A second identical run must not re-hit the model.
            await pipeline.process_item(
                item_id, config=tmp_config, steps=steps, ocr_scope="all_media"
            )
        assert mock_caption.await_count == 1

    def test_should_ocr_predicate(self) -> None:
        from gramvault.models.schemas import Item, MediaFile

        thin = Item(id=1, media_type="video", caption="#foryou #viral")
        wordy = Item(id=2, media_type="video", caption="Here is the full method, " * 5)
        fresh_video = MediaFile(id=1, item_id=1, file_path="v.mp4", media_type="video")
        captioned_video = MediaFile(
            id=2, item_id=2, file_path="v.mp4", media_type="video", transcript="hello there"
        )
        discarded = MediaFile(
            id=3,
            item_id=1,
            file_path="v.mp4",
            media_type="video",
            ocr_attempted_at="2026-01-01",
        )
        photo = MediaFile(id=4, item_id=1, file_path="p.jpg", media_type="photo")

        assert pipeline._should_ocr(fresh_video, thin, "silent_thin_caption") is True
        assert pipeline._should_ocr(fresh_video, wordy, "silent_thin_caption") is False
        assert pipeline._should_ocr(fresh_video, wordy, "all_silent") is True
        assert pipeline._should_ocr(captioned_video, thin, "all_silent") is False
        assert pipeline._should_ocr(photo, thin, "all_silent") is False
        assert pipeline._should_ocr(photo, thin, "all_media") is True
        assert pipeline._should_ocr(discarded, thin, "all_media") is False
        assert pipeline._should_ocr(discarded, thin, "retry_discarded") is True
        assert pipeline._should_ocr(fresh_video, thin, "retry_discarded") is False


class TestProcessItemFailureHandling:
    @pytest.mark.anyio
    async def test_ollama_not_running_marks_item_failed_with_error_recorded(
        self, tmp_config: Config
    ) -> None:
        with session_scope(tmp_config) as conn:
            item_id = _insert_item(conn, media_type="photo")
            _insert_media_file(conn, item_id, file_media_type="photo", file_path="a.jpg")

        with patch("gramvault.ai.ollama_client.ensure_running",
            new_callable=AsyncMock,
            side_effect=OllamaNotRunningError("http://localhost:11434"),
        ):
            await pipeline.process_item(item_id, config=tmp_config)

        item = _get_item(tmp_config, item_id)
        assert item.enrichment_status == EnrichmentStatus.FAILED

        with session_scope(tmp_config) as conn:
            row = conn.execute(
                "SELECT raw_metadata_json FROM items WHERE id = ?", (item_id,)
            ).fetchone()
        metadata = json.loads(row["raw_metadata_json"])
        assert "Ollama" in metadata["enrichment_error"]

    @pytest.mark.anyio
    async def test_successful_rerun_clears_stale_error(self, tmp_config: Config) -> None:
        with session_scope(tmp_config) as conn:
            item_id = _insert_item(conn, media_type="photo")
            _insert_media_file(conn, item_id, file_media_type="photo", file_path="a.jpg")
            conn.execute(
                "UPDATE items SET raw_metadata_json = ? WHERE id = ?",
                (json.dumps({"enrichment_error": "previous failure"}), item_id),
            )

        with (
            patch("gramvault.ai.ollama_client.ensure_running", new_callable=AsyncMock),
            patch("gramvault.ai.ollama_client.ensure_model_pulled", new_callable=AsyncMock),
            patch("gramvault.ai.ollama_client.caption_image",
                new_callable=AsyncMock,
                return_value="a caption",
            ),
            patch("gramvault.ai.ollama_client.embed", new_callable=AsyncMock),
            patch.object(pipeline.embedding_store, "upsert_item"),
        ):
            await pipeline.process_item(item_id, config=tmp_config)

        with session_scope(tmp_config) as conn:
            row = conn.execute(
                "SELECT raw_metadata_json FROM items WHERE id = ?", (item_id,)
            ).fetchone()
        metadata = json.loads(row["raw_metadata_json"]) if row["raw_metadata_json"] else {}
        assert "enrichment_error" not in metadata

    @pytest.mark.anyio
    async def test_missing_item_id_is_a_noop(self, tmp_config: Config) -> None:
        # Should return quietly (logs a warning) rather than raising.
        await pipeline.process_item(999999, config=tmp_config)


class TestProcessItems:
    @pytest.mark.anyio
    async def test_processes_each_id_sequentially_in_order(self, tmp_config: Config) -> None:
        calls: list[int] = []

        async def fake_process_item(
            item_id: int, config: Config | None = None, *, skip_vision: bool = False, **_kw
        ) -> None:
            calls.append(item_id)

        with patch.object(pipeline, "process_item", new=fake_process_item):
            await pipeline.process_items([3, 1, 2], config=tmp_config)

        assert calls == [3, 1, 2]

    @pytest.mark.anyio
    async def test_skip_vision_is_forwarded_to_each_item(self, tmp_config: Config) -> None:
        """The flag has to reach `process_item` — forgetting to thread it
        through would silently run the vision model anyway."""
        seen: list[bool] = []

        async def fake_process_item(
            item_id: int, config: Config | None = None, *, skip_vision: bool = False, **_kw
        ) -> None:
            seen.append(skip_vision)

        with patch.object(pipeline, "process_item", new=fake_process_item):
            await pipeline.process_items([1, 2], config=tmp_config, skip_vision=True)

        assert seen == [True, True]

    @pytest.mark.anyio
    async def test_empty_list_does_nothing(self, tmp_config: Config) -> None:
        with patch.object(pipeline, "process_item", new_callable=AsyncMock) as mock_process:
            await pipeline.process_items([], config=tmp_config)
        mock_process.assert_not_awaited()


class TestResolveTargetItemIds:
    def test_explicit_ids_are_returned_verbatim(self, tmp_db_conn: sqlite3.Connection) -> None:
        assert pipeline.resolve_target_item_ids(tmp_db_conn, [5, 3]) == [5, 3]

    def test_none_resolves_to_all_pending_items(self, tmp_db_conn: sqlite3.Connection) -> None:
        pending_id = tmp_db_conn.execute(
            "INSERT INTO items (media_type, enrichment_status) VALUES ('photo', 'pending')"
        ).lastrowid
        tmp_db_conn.execute(
            "INSERT INTO items (media_type, enrichment_status) VALUES ('photo', 'done')"
        )
        tmp_db_conn.commit()

        result = pipeline.resolve_target_item_ids(tmp_db_conn, None)

        assert result == [pending_id]

    def test_none_with_no_pending_items_returns_empty(
        self, tmp_db_conn: sqlite3.Connection
    ) -> None:
        tmp_db_conn.execute(
            "INSERT INTO items (media_type, enrichment_status) VALUES ('photo', 'done')"
        )
        tmp_db_conn.commit()
        assert pipeline.resolve_target_item_ids(tmp_db_conn, None) == []


class TestMarkItemsPending:
    def test_flips_given_items_to_pending(self, tmp_db_conn: sqlite3.Connection) -> None:
        item_id = tmp_db_conn.execute(
            "INSERT INTO items (media_type, enrichment_status) VALUES ('photo', 'done')"
        ).lastrowid
        tmp_db_conn.commit()

        pipeline.mark_items_pending(tmp_db_conn, [item_id])
        tmp_db_conn.commit()

        row = tmp_db_conn.execute(
            "SELECT enrichment_status FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row["enrichment_status"] == "pending"

    def test_empty_list_is_a_noop(self, tmp_db_conn: sqlite3.Connection) -> None:
        # Should not raise (e.g. no malformed "IN ()" SQL).
        pipeline.mark_items_pending(tmp_db_conn, [])
