"""Per-item AI enrichment orchestration + a simple in-process job "queue".

Pipeline, per item:
    1. For each of the item's media files:
         - photo: caption directly with the vision model (llava).
         - video: extract up to `video.max_keyframes` keyframes (ffmpeg,
           one every `video.keyframe_interval_seconds` seconds), caption
           each with the vision model and join them into one
           `vision_caption` string; separately transcribe the audio track
           with faster-whisper into `transcript`.
       Fields that are already populated are left untouched — this is
       what makes a restart resumable: re-running a partially-enriched
       item only performs the remaining steps instead of redoing
       everything (there's no separate "captioning/transcribing/embedding"
       sub-status column; the presence of `vision_caption`/`transcript`
       on each `media_files` row already encodes that state without
       needing to touch the shared `items.enrichment_status` CHECK
       constraint, which only allows pending/running/done/failed).
    2. Build one combined "content document" for the item (author,
       original caption, tags, per-media captions/transcripts) via
       `document_builder`.
    3. Chunk it (`chunking.chunk_size`/`chunk_overlap`) and embed each
       chunk with the configured embedding model (nomic-embed-text via
       Ollama), upserting into ChromaDB via `embedding_store`.
    4. Flip `items.enrichment_status` to 'done', or 'failed' with the
       exception message recorded in `items.raw_metadata_json` (merged in
       as `{"enrichment_error": "..."}`, preserving whatever was already
       in that column) if anything raised.

Job "queue": `items.enrichment_status` IS the queue. `process_items()` is
handed a list of item ids and processes them sequentially; routes_enrich.py
schedules it as a FastAPI `BackgroundTasks` callback rather than running a
separate worker thread/process, per the "keep it simple, no Celery/Redis"
brief. Because progress lives in SQLite, a crash/restart mid-run just
leaves some items at 'running' (or 'pending') to be picked up again by the
next `/api/enrich/run` call — no additional state to reconcile.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from gramvault.ai import document_builder, embedding_store, keyframes, ocr, transcription
from gramvault.ai.ollama_client import DEFAULT_CAPTION_PROMPT
from gramvault.ai.providers import get_provider
from gramvault.chat import fts
from gramvault.chat.retrieval import fetch_items
from gramvault.config import Config, get_config
from gramvault.db.session import session_scope
from gramvault.models.schemas import EnrichmentStatus, Item, MediaFile

logger = logging.getLogger(__name__)


# --- which steps a run performs --------------------------------------------


OcrScope = Literal["silent_thin_caption", "all_silent", "all_media", "retry_discarded"]


@dataclass(frozen=True)
class PipelineSteps:
    """Toggles for the four independent enrichment passes. The defaults
    reproduce the historical `enrich` behaviour (caption + transcribe +
    embed, OCR opt-in) so callers that don't pass `steps` are unaffected."""

    transcribe: bool = True
    ocr: bool = False
    vision_caption: bool = True
    embed: bool = True


DEFAULT_STEPS = PipelineSteps()


class _ItemStepError(Exception):
    """Raised by `process_item` when a non-embed pass (OCR / transcribe /
    caption without embed) fails. The item's `enrichment_status` is
    deliberately left untouched — the un-stamped `ocr_attempted_at` / null
    `transcript` already makes the next run retry just the media that
    didn't get processed. `process_items` counts these and aborts the
    batch after a run of them, treating a repeated failure as systemic
    (Ollama unreachable) rather than one bad file."""

    def __init__(self, item_id: int) -> None:
        super().__init__(f"step failed for item {item_id}")
        self.item_id = item_id


# Consecutive non-embed failures before `process_items` gives up on the batch.
_MAX_CONSECUTIVE_FAILURES = 5


def _caption_substance(caption: str | None) -> int:
    """Length of a caption once URLs, #hashtags and @mentions are stripped —
    the same heuristic `gramvault ocr` uses to spot a 'thin' caption whose
    reel's only real text is the on-screen overlay."""
    text = re.sub(r"https?://\S+", "", caption or "")
    text = re.sub(r"[#@]\w+", "", text)
    return len(re.sub(r"[^\w\s]", "", text).strip())


# --- path helpers ------------------------------------------------------------


def _resolve_media_path(config: Config, file_path: str) -> Path:
    """`media_files.file_path` may be stored absolute or relative to the
    configured library directory — resolve either form consistently."""
    path = Path(file_path)
    if path.is_absolute():
        return path
    return config.resolved_library_dir / path


def _keyframes_dir(config: Config, media_file_id: int) -> Path:
    """Scratch directory for extracted video keyframes, alongside the
    SQLite DB / Chroma dir under the configured `paths.db_path`'s parent
    (so everything the app writes lives under one `data/` root)."""
    return config.resolved_db_path.parent / "keyframes" / str(media_file_id)


# --- SQLite update helpers ----------------------------------------------------


def _update_media_file(
    conn: sqlite3.Connection,
    media_file_id: int,
    *,
    vision_caption: str | None = None,
    vision_model: str | None = None,
    transcript: str | None = None,
    transcript_model: str | None = None,
) -> None:
    if vision_caption is not None:
        conn.execute(
            "UPDATE media_files SET vision_caption = ?, vision_model = ? WHERE id = ?",
            (vision_caption, vision_model, media_file_id),
        )
    if transcript is not None:
        conn.execute(
            "UPDATE media_files SET transcript = ?, transcript_model = ? WHERE id = ?",
            (transcript, transcript_model, media_file_id),
        )


def _record_transcript_segments(
    conn: sqlite3.Connection,
    media_file_id: int,
    segments: list[transcription.TranscriptSegment],
) -> None:
    """Replace `media_file_id`'s timed segments (migration 008, R11) —
    delete-then-insert, same shape as `chat.fts.reindex`, so a re-run
    (e.g. `retry_discarded`-style re-transcription) doesn't duplicate rows."""
    conn.execute("DELETE FROM transcript_segments WHERE media_file_id = ?", (media_file_id,))
    if segments:
        conn.executemany(
            "INSERT INTO transcript_segments "
            "(media_file_id, sequence_index, start_seconds, end_seconds, text) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (media_file_id, index, segment.start, segment.end, segment.text)
                for index, segment in enumerate(segments)
            ],
        )


def _record_ocr(
    conn: sqlite3.Connection, media_file_id: int, text: str | None, model: str
) -> None:
    """Write an OCR result. `ocr_attempted_at` is stamped even when `text`
    is None (a discarded read) so the `ocr_attempted_at IS NULL` queue
    doesn't keep re-running a frame the model can't read."""
    conn.execute(
        "UPDATE media_files SET ocr_text = ?, ocr_attempted_at = datetime('now'), "
        "ocr_model = ? WHERE id = ?",
        (text, model, media_file_id),
    )


def _merge_raw_metadata(conn: sqlite3.Connection, item_id: int, updates: dict) -> None:
    """Merge `updates` into `items.raw_metadata_json` (a free-form JSON blob
    already in the shared schema for "anything unmapped"), preserving keys
    not touched here. `updates[key] = None` deletes that key."""
    row = conn.execute("SELECT raw_metadata_json FROM items WHERE id = ?", (item_id,)).fetchone()
    raw: dict = {}
    if row and row["raw_metadata_json"]:
        try:
            raw = json.loads(row["raw_metadata_json"])
        except (json.JSONDecodeError, TypeError):
            raw = {}
    for key, value in updates.items():
        if value is None:
            raw.pop(key, None)
        else:
            raw[key] = value
    conn.execute(
        "UPDATE items SET raw_metadata_json = ? WHERE id = ?", (json.dumps(raw), item_id)
    )


def _set_item_status(
    conn: sqlite3.Connection, item_id: int, status: EnrichmentStatus, error: str | None = None
) -> None:
    conn.execute("UPDATE items SET enrichment_status = ? WHERE id = ?", (status.value, item_id))
    if status == EnrichmentStatus.FAILED and error is not None:
        _merge_raw_metadata(conn, item_id, {"enrichment_error": error})
    elif status == EnrichmentStatus.DONE:
        # Clear any stale error recorded by a previous failed attempt.
        _merge_raw_metadata(conn, item_id, {"enrichment_error": None})


# --- per-media-file steps -----------------------------------------------------


async def _caption_photo(config: Config, media_file: MediaFile) -> str:
    path = _resolve_media_path(config, media_file.file_path)
    provider, model = get_provider("vision", config)
    return await provider.caption_image(model, path, DEFAULT_CAPTION_PROMPT)


async def _caption_video(config: Config, media_file: MediaFile) -> str:
    assert media_file.id is not None
    path = _resolve_media_path(config, media_file.file_path)
    out_dir = _keyframes_dir(config, media_file.id)
    frames = await asyncio.to_thread(keyframes.extract_keyframes, path, out_dir, config)
    if not frames:
        return ""
    provider, model = get_provider("vision", config)
    captions: list[str] = []
    for frame in frames:
        caption = await provider.caption_image(model, frame, DEFAULT_CAPTION_PROMPT)
        if caption:
            captions.append(caption.strip())
    return " | ".join(captions)


async def _transcribe_video(
    config: Config, media_file: MediaFile, caption: str | None = None
) -> transcription.TranscriptionResult:
    """Returns the full result, not just `.text` — R11: the timed segments
    faster-whisper already produces are worth keeping (a caption deep-link,
    a WebVTT track), not just the flattened text."""
    path = _resolve_media_path(config, media_file.file_path)
    hint = transcription.language_hint(caption)
    return await asyncio.to_thread(transcription.transcribe, path, hint, config)


# --- on-screen text (OCR) ----------------------------------------------------


def _vision_model_id(config: Config) -> str:
    """`provider:model` string stored as OCR / caption provenance, so a
    later 'retry with a stronger model' run knows what produced a result."""
    provider, model = get_provider("vision", config)
    return f"{provider.name}:{model}"


def _should_ocr(media_file: MediaFile, item: Item, scope: OcrScope) -> bool:
    """Per-media-file decision for the OCR step, given the requested scope.

    All scopes except `retry_discarded` only touch media that has never been
    attempted (`ocr_attempted_at IS NULL`); `retry_discarded` is the inverse
    — media attempted before but discarded (Cyrillic / refusal), for
    re-running once a better vision model is configured.
    """
    if scope == "retry_discarded":
        return media_file.ocr_attempted_at is not None and not media_file.ocr_text
    if media_file.ocr_attempted_at is not None:
        return False
    if scope == "all_media":
        return True
    # all_silent / silent_thin_caption: videos with no speech track.
    if media_file.media_type != "video" or media_file.transcript:
        return False
    if scope == "all_silent":
        return True
    return _caption_substance(item.caption) < 40  # silent_thin_caption


async def _ocr_media(config: Config, media_file: MediaFile) -> str | None:
    """Read on-screen text from one media file. Photos (and carousel slides)
    are read directly; a video is sampled with a single frame ~2 s in. The
    raw model reply is passed through `ocr.clean_output`, which returns None
    for an unusable read (Cyrillic, a refusal, a scene description)."""
    provider, model = get_provider("vision", config)
    path = _resolve_media_path(config, media_file.file_path)
    if media_file.media_type == "photo":
        frame: Path | None = path
    else:
        assert media_file.id is not None
        out_dir = _keyframes_dir(config, media_file.id)
        out_dir.mkdir(parents=True, exist_ok=True)
        frame = await asyncio.to_thread(ocr.frame_for_ocr, path, out_dir / "ocr.jpg")
    if frame is None:
        return None
    raw = await provider.caption_image(model, frame, ocr.OCR_PROMPT)
    return ocr.clean_output(raw)


# --- embedding -----------------------------------------------------------------


async def _embed_and_upsert(config: Config, item: Item) -> None:
    document = document_builder.build_content_document(item)
    chunks = (
        document_builder.chunk_text(
            document, config.chunking.chunk_size, config.chunking.chunk_overlap
        )
        if document
        else []
    )
    provider, model = get_provider("embedding", config)
    for index, chunk in enumerate(chunks):
        embedding = await provider.embed(model, chunk)
        await asyncio.to_thread(
            embedding_store.upsert_item,
            item.id,
            embedding,
            chunk,
            None,  # media_file_id: this document merges all of the item's media
            index,
            {"media_type": str(item.media_type)},
            config,
        )
    # Upserts overwrite chunks 0..n-1 only; drop any a previous, longer
    # version of this item's document left behind.
    await asyncio.to_thread(embedding_store.prune_item_chunks, item.id, len(chunks), config)


# --- per-item orchestration ----------------------------------------------------


def _resolve_steps(steps: PipelineSteps | None, skip_vision: bool) -> PipelineSteps:
    """Fold the legacy `skip_vision` flag into a `PipelineSteps`. `steps`
    wins when given; `skip_vision=True` only ever *removes* the caption
    pass, never adds one."""
    resolved = steps or DEFAULT_STEPS
    if skip_vision and resolved.vision_caption:
        resolved = PipelineSteps(
            transcribe=resolved.transcribe,
            ocr=resolved.ocr,
            vision_caption=False,
            embed=resolved.embed,
        )
    return resolved


async def process_item(
    item_id: int,
    config: Config | None = None,
    *,
    skip_vision: bool = False,
    steps: PipelineSteps | None = None,
    ocr_scope: OcrScope = "silent_thin_caption",
) -> None:
    """Run the enrichment pipeline for one item, resumably (see the module
    docstring). Never raises — failures are recorded on the item
    (`enrichment_status='failed'` + `raw_metadata_json.enrichment_error`)
    rather than propagated, so a batch run continues past one bad item.

    `steps` selects which of the four independent passes run (transcribe /
    ocr / vision_caption / embed); the default runs everything except OCR.
    Each pass is separately resumable — a populated `transcript` /
    `vision_caption` / `ocr_attempted_at` is what makes a re-run skip it —
    so the same item can be OCR'd today and captioned next week.

    `skip_vision` is the legacy spelling of `steps.vision_caption=False`:
    embed transcripts and the original caption without writing image
    captions you don't trust (a wrong caption is worse than none once it's
    embedded). The `enrichment_status` column is only touched when
    `steps.embed` is on — an OCR-only pass over already-done items leaves
    their status alone.
    """
    config = config or get_config()
    steps = _resolve_steps(steps, skip_vision)

    with session_scope(config) as conn:
        items = fetch_items(conn, [item_id])
        item = items.get(item_id)
        if item is None:
            logger.warning("process_item: item %s not found, skipping", item_id)
            return
        if steps.embed:
            conn.execute(
                "UPDATE items SET enrichment_status = ? WHERE id = ?",
                (EnrichmentStatus.RUNNING.value, item_id),
            )

    try:
        if steps.embed:
            embed_provider, embed_model = get_provider("embedding", config)
            await embed_provider.ensure_ready(embed_model)

        needs_caption = steps.vision_caption and any(
            not mf.vision_caption for mf in item.media_files
        )
        needs_ocr = steps.ocr and any(
            _should_ocr(mf, item, ocr_scope) for mf in item.media_files
        )
        vision_model_id: str | None = None
        if needs_caption or needs_ocr:
            vision_provider, vision_model = get_provider("vision", config)
            await vision_provider.ensure_ready(vision_model)
            vision_model_id = f"{vision_provider.name}:{vision_model}"

        for media_file in item.media_files:
            if steps.vision_caption and not media_file.vision_caption:
                caption = (
                    await _caption_photo(config, media_file)
                    if media_file.media_type == "photo"
                    else await _caption_video(config, media_file)
                )
                with session_scope(config) as conn:
                    _update_media_file(
                        conn,
                        media_file.id,
                        vision_caption=caption,
                        vision_model=vision_model_id,
                    )
                media_file.vision_caption = caption

            if steps.ocr and _should_ocr(media_file, item, ocr_scope):
                text = await _ocr_media(config, media_file)
                with session_scope(config) as conn:
                    _record_ocr(conn, media_file.id, text, vision_model_id or "")
                media_file.ocr_text = text

            if (
                steps.transcribe
                and media_file.media_type == "video"
                and media_file.transcript is None
            ):
                result = await _transcribe_video(config, media_file, item.caption)
                with session_scope(config) as conn:
                    _update_media_file(
                        conn,
                        media_file.id,
                        transcript=result.text,
                        transcript_model=config.transcription.model_size,
                    )
                    # transcribe() keeps `result.segments` even when `text`
                    # came back "" (is_noise() filtered a hallucination,
                    # e.g. "Thanks for watching!" over a silent reel) — that
                    # discard decision must not be silently reversed here by
                    # storing the very segments it filtered out.
                    if result.text:
                        _record_transcript_segments(conn, media_file.id, result.segments)
                media_file.transcript = result.text

        if steps.embed:
            await _embed_and_upsert(config, item)
            with session_scope(config) as conn:
                _set_item_status(conn, item_id, EnrichmentStatus.DONE)

    except Exception as exc:  # noqa: BLE001 - broad on purpose: one bad item mustn't end the batch
        logger.exception("Enrichment failed for item %s", item_id)
        # Only the embed pass "owns" `enrichment_status`. An OCR- or
        # transcribe-only run that trips over a transient error (Ollama
        # briefly unreachable while a model loads) must not flip a
        # previously-`done` item to `failed` — the un-stamped
        # `ocr_attempted_at` / null `transcript` already makes the next
        # run retry exactly the media that didn't get processed.
        if steps.embed:
            with session_scope(config) as conn:
                _set_item_status(conn, item_id, EnrichmentStatus.FAILED, error=str(exc))
        else:
            raise _ItemStepError(item_id) from exc


async def process_items(
    item_ids: list[int],
    config: Config | None = None,
    *,
    skip_vision: bool = False,
    steps: PipelineSteps | None = None,
    ocr_scope: OcrScope = "silent_thin_caption",
    cancel_check: Callable[[], bool] | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """Sequentially process a batch of items — the "worker" side of the
    job queue, scheduled as a background task by `routes_enrich.run_enrichment`.

    `cancel_check`, if given, is polled before each item; returning True
    stops the batch (already-processed items keep their results).
    `progress_cb(done, total)`, if given, is called after each item.
    """
    config = config or get_config()
    total = len(item_ids)
    consecutive_failures = 0
    for index, item_id in enumerate(item_ids):
        if cancel_check is not None and cancel_check():
            logger.info("process_items: cancellation requested, stopping after %d/%d", index, total)
            break
        try:
            await process_item(
                item_id, config, skip_vision=skip_vision, steps=steps, ocr_scope=ocr_scope
            )
            consecutive_failures = 0
        except _ItemStepError as exc:
            consecutive_failures += 1
            logger.warning("process_items: %s (%d in a row)", exc, consecutive_failures)
            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                raise RuntimeError(
                    f"aborting batch: {consecutive_failures} items failed in a row "
                    f"(last: {exc}) — is the AI provider reachable?"
                ) from exc
        if progress_cb is not None:
            progress_cb(index + 1, total)

    # Enrichment just rewrote transcripts / vision captions / OCR text —
    # refresh the FTS keyword index for the batch (best-effort). Off the
    # event loop (R10): the DELETE + INSERT...SELECT rebuilds every one of
    # this batch's rows (each with a GROUP_CONCAT over its tags/transcripts
    # /captions), real synchronous SQLite time for a large batch. Runs in
    # its own thread with its own connection — session_scope() opens a
    # fresh one, so there's no cross-thread sqlite3.Connection use.
    try:
        await asyncio.to_thread(_reindex_batch, config, list(item_ids))
    except Exception:  # noqa: BLE001 - never fail a finished batch on index upkeep
        logger.warning("process_items: FTS reindex failed", exc_info=True)


def _reindex_batch(config: Config, item_ids: list[int]) -> None:
    with session_scope(config) as conn:
        fts.reindex(conn, item_ids)


# --- queue resolution helpers (used by routes_enrich.py) -----------------------


def resolve_target_item_ids(conn: sqlite3.Connection, item_ids: list[int] | None) -> list[int]:
    """Resolve the target item id list for a `/run` request: the explicit
    `item_ids` if given, otherwise every item currently `pending`."""
    if item_ids is not None:
        return list(item_ids)
    rows = conn.execute(
        "SELECT id FROM items WHERE enrichment_status = ?", (EnrichmentStatus.PENDING.value,)
    ).fetchall()
    return [row["id"] for row in rows]


_OCR_PENDING_PREDICATE: dict[str, str] = {
    "retry_discarded": "m.ocr_attempted_at IS NOT NULL AND m.ocr_text IS NULL",
    "all_media": "m.ocr_attempted_at IS NULL",
    "all_silent": (
        "m.ocr_attempted_at IS NULL AND m.media_type = 'video' "
        "AND (m.transcript IS NULL OR m.transcript = '')"
    ),
    # caption thinness is judged in Python (`_should_ocr`); the SQL side
    # approximates it with the silent-video predicate — an over-broad
    # `only_missing` set just means the pipeline no-ops a few items.
    "silent_thin_caption": (
        "m.ocr_attempted_at IS NULL AND m.media_type = 'video' "
        "AND (m.transcript IS NULL OR m.transcript = '')"
    ),
}


def resolve_enrich_targets(
    conn: sqlite3.Connection,
    *,
    item_ids: list[int] | None = None,
    category: str | None = None,
    only_missing: bool = True,
    steps: PipelineSteps = DEFAULT_STEPS,
    ocr_scope: OcrScope = "silent_thin_caption",
) -> list[int]:
    """Resolve a scoped `/run` request into a concrete item-id list.

    Scope: explicit `item_ids`, else every item in `category` (by name),
    else the whole library. `only_missing` then keeps just the items that
    still have outstanding work for at least one of the selected `steps`.
    """
    if item_ids is not None:
        candidates = list(item_ids)
    elif category is not None:
        rows = conn.execute(
            "SELECT i.id FROM items i JOIN categories c ON c.id = i.category_id "
            "WHERE c.name = ?",
            (category,),
        ).fetchall()
        candidates = [r["id"] for r in rows]
    else:
        candidates = [r["id"] for r in conn.execute("SELECT id FROM items")]

    if not candidates or not only_missing:
        return candidates

    placeholders = ",".join("?" for _ in candidates)
    ocr_pred = _OCR_PENDING_PREDICATE[ocr_scope]
    clauses: list[str] = []
    if steps.embed:
        clauses.append(f"i.enrichment_status != '{EnrichmentStatus.DONE.value}'")
    if steps.transcribe:
        clauses.append(
            "EXISTS (SELECT 1 FROM media_files m WHERE m.item_id = i.id "
            "AND m.media_type = 'video' AND m.transcript IS NULL)"
        )
    if steps.vision_caption:
        clauses.append(
            "EXISTS (SELECT 1 FROM media_files m WHERE m.item_id = i.id "
            "AND (m.vision_caption IS NULL OR m.vision_caption = ''))"
        )
    if steps.ocr:
        clauses.append(
            f"EXISTS (SELECT 1 FROM media_files m WHERE m.item_id = i.id AND ({ocr_pred}))"
        )
    if not clauses:
        return []
    rows = conn.execute(
        f"SELECT i.id FROM items i WHERE i.id IN ({placeholders}) "
        f"AND ({' OR '.join(clauses)})",
        candidates,
    ).fetchall()
    return [r["id"] for r in rows]


def mark_items_pending(conn: sqlite3.Connection, item_ids: list[int]) -> None:
    """Flip the given (existing) items to `pending` immediately, so
    `/api/enrich/progress` reflects them as queued right away rather than
    waiting for the background task to actually start."""
    if not item_ids:
        return
    placeholders = ",".join("?" for _ in item_ids)
    conn.execute(
        f"UPDATE items SET enrichment_status = ? WHERE id IN ({placeholders})",
        (EnrichmentStatus.PENDING.value, *item_ids),
    )
