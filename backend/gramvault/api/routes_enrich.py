"""Enrichment API (Agent A3).

Owns: triggering the AI enrichment pipeline (keyframe extraction + vision
captions for photos/videos, on-screen-text OCR for silent reels,
faster-whisper transcription for videos, chunking + embeddings written to
ChromaDB) and exposing progress so the frontend can poll a progress bar.

The actual pipeline logic lives in `gramvault.ai.pipeline` — this module is
just the HTTP surface: resolving the request into a target item id list,
scheduling background processing, and translating the AI providers'
friendly exceptions into clean HTTP responses instead of raw stack traces.

The `/run` body has two shapes:
  - legacy: `{"item_ids": [1, 2] | null}` — null means "all pending".
  - scoped: `{"scope": {...}, "steps": {...}, "ocr_scope": "..."}` — pick
    which of the four passes run (transcribe / ocr / vision_caption /
    embed) over a scope (explicit ids, a category, or the whole library,
    optionally narrowed to items with outstanding work).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from gramvault.ai import pipeline
from gramvault.ai.errors import ProviderNotReadyError
from gramvault.ai.pipeline import DEFAULT_STEPS, OcrScope, PipelineSteps
from gramvault.ai.providers import get_provider
from gramvault.api import jobs
from gramvault.api.deps import get_config_dependency
from gramvault.chat.retrieval import fetch_items
from gramvault.config import Config
from gramvault.db.session import session_scope
from gramvault.models.schemas import EnrichmentStatus, Item, JobKind

router = APIRouter(prefix="/api/enrich", tags=["enrich"])


class EnrichScope(BaseModel):
    item_ids: list[int] | None = None
    category: str | None = None
    only_missing: bool = True


class EnrichSteps(BaseModel):
    transcribe: bool = True
    ocr: bool = False
    vision_caption: bool = True
    embed: bool = True

    def to_pipeline(self) -> PipelineSteps:
        return PipelineSteps(
            transcribe=self.transcribe,
            ocr=self.ocr,
            vision_caption=self.vision_caption,
            embed=self.embed,
        )


class EnrichmentRunRequest(BaseModel):
    # Legacy shape: None means "enqueue all items currently pending".
    item_ids: list[int] | None = None
    # Scoped shape (takes precedence when present).
    scope: EnrichScope | None = None
    steps: EnrichSteps | None = None
    ocr_scope: OcrScope = "silent_thin_caption"


class EnrichmentRunResponse(BaseModel):
    queued_count: int
    # The `jobs` row tracking this run — poll `/api/jobs/{job_id}` for
    # progress/cancellation. None when there was nothing to enqueue.
    job_id: int | None = None


class StepProgress(BaseModel):
    done: int
    pending: int


class EnrichmentProgress(BaseModel):
    total: int
    pending: int
    running: int
    done: int
    failed: int
    # Per-pass media/item counts, independent of item enrichment_status —
    # what the Enrich page shows next to each step toggle.
    steps: dict[str, StepProgress]
    # The id of the currently-active enrich job, if one is running.
    job_id: int | None = None


def _mark_pending_ids(body: EnrichmentRunRequest) -> list[int] | None:
    """The explicit id list to flip to `pending` up front (so progress
    shows them queued immediately), or None when the scope is implicit."""
    if body.scope is not None:
        return body.scope.item_ids
    return body.item_ids


@router.post("/run", response_model=EnrichmentRunResponse, status_code=202)
async def run_enrichment(
    body: EnrichmentRunRequest,
    background_tasks: BackgroundTasks,
    config: Config = Depends(get_config_dependency),
) -> EnrichmentRunResponse:
    """Enqueue items for AI enrichment.

    Readiness (Ollama up + required models pulled) is checked synchronously
    up front so a misconfigured setup fails fast with a friendly 503. Only
    the providers the selected steps actually use are checked: an
    embed-only run doesn't need the vision model, and a link-only library
    (metadata without media bytes) doesn't need it either.
    """
    steps = body.steps.to_pipeline() if body.steps is not None else DEFAULT_STEPS
    ocr_scope = body.ocr_scope

    with session_scope(config) as conn:
        if body.scope is not None:
            item_ids = pipeline.resolve_enrich_targets(
                conn,
                item_ids=body.scope.item_ids,
                category=body.scope.category,
                only_missing=body.scope.only_missing,
                steps=steps,
                ocr_scope=ocr_scope,
            )
        else:
            item_ids = pipeline.resolve_target_item_ids(conn, body.item_ids)

        needs_vision = False
        if item_ids and (steps.vision_caption or steps.ocr):
            placeholders = ",".join("?" for _ in item_ids)
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM media_files WHERE item_id IN ({placeholders})",
                item_ids,
            ).fetchone()
            needs_vision = row["count"] > 0

    try:
        if steps.embed:
            embed_provider, embed_model = get_provider("embedding", config)
            await embed_provider.ensure_ready(embed_model)
        if needs_vision:
            vision_provider, vision_model = get_provider("vision", config)
            await vision_provider.ensure_ready(vision_model)
    except ProviderNotReadyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not item_ids:
        return EnrichmentRunResponse(queued_count=0)

    with session_scope(config) as conn:
        mark_ids = _mark_pending_ids(body)
        if mark_ids is not None and steps.embed:
            pipeline.mark_items_pending(conn, item_ids)
        try:
            job = jobs.create(
                conn,
                JobKind.ENRICH,
                params={
                    "count": len(item_ids),
                    "steps": {
                        "transcribe": steps.transcribe,
                        "ocr": steps.ocr,
                        "vision_caption": steps.vision_caption,
                        "embed": steps.embed,
                    },
                    "ocr_scope": ocr_scope,
                },
            )
        except jobs.JobConflict as exc:
            raise HTTPException(
                status_code=409, detail="An enrichment job is already running"
            ) from exc

    assert job.id is not None

    async def _work(ctx: jobs.JobContext) -> dict:
        ctx.progress(done=0, total=len(item_ids))
        done = 0

        def _bump(done_count: int, total: int) -> None:
            nonlocal done
            done = done_count
            ctx.progress(done=done_count, total=total)

        await pipeline.process_items(
            item_ids,
            config,
            steps=steps,
            ocr_scope=ocr_scope,
            cancel_check=lambda: ctx.cancelled,
            progress_cb=_bump,
        )
        return {"processed": done, "total": len(item_ids)}

    background_tasks.add_task(jobs.run, job.id, _work, config)

    return EnrichmentRunResponse(queued_count=len(item_ids), job_id=job.id)


def _step_counts(conn) -> dict[str, StepProgress]:  # noqa: ANN001 - sqlite3.Connection
    def pair(done_sql: str, pending_sql: str) -> StepProgress:
        done = conn.execute(f"SELECT COUNT(*) FROM media_files WHERE {done_sql}").fetchone()[0]
        pending = conn.execute(
            f"SELECT COUNT(*) FROM media_files WHERE {pending_sql}"
        ).fetchone()[0]
        return StepProgress(done=done, pending=pending)

    embed_done = conn.execute(
        "SELECT COUNT(*) FROM items WHERE enrichment_status = ?",
        (EnrichmentStatus.DONE.value,),
    ).fetchone()[0]
    embed_pending = conn.execute(
        "SELECT COUNT(*) FROM items WHERE enrichment_status != ?",
        (EnrichmentStatus.DONE.value,),
    ).fetchone()[0]

    return {
        "transcribe": pair(
            "media_type = 'video' AND transcript IS NOT NULL",
            "media_type = 'video' AND transcript IS NULL",
        ),
        "ocr": pair("ocr_attempted_at IS NOT NULL", "ocr_attempted_at IS NULL"),
        "vision_caption": pair(
            "vision_caption IS NOT NULL AND vision_caption != ''",
            "vision_caption IS NULL OR vision_caption = ''",
        ),
        "embed": StepProgress(done=embed_done, pending=embed_pending),
    }


@router.get("/progress", response_model=EnrichmentProgress)
async def get_enrichment_progress(
    config: Config = Depends(get_config_dependency),
) -> EnrichmentProgress:
    """Aggregate enrichment progress: item status counts, per-pass
    media/item counts, and the active job id (for the Enrich page)."""
    with session_scope(config) as conn:
        rows = conn.execute(
            "SELECT enrichment_status, COUNT(*) AS count FROM items GROUP BY enrichment_status"
        ).fetchall()
        step_counts = _step_counts(conn)
        active = jobs.active(conn, JobKind.ENRICH)
    counts = {row["enrichment_status"]: row["count"] for row in rows}
    total = sum(counts.values())
    return EnrichmentProgress(
        total=total,
        pending=counts.get(EnrichmentStatus.PENDING.value, 0),
        running=counts.get(EnrichmentStatus.RUNNING.value, 0),
        done=counts.get(EnrichmentStatus.DONE.value, 0),
        failed=counts.get(EnrichmentStatus.FAILED.value, 0),
        steps=step_counts,
        job_id=active.id if active else None,
    )


class EnrichmentFailure(BaseModel):
    item_id: int
    caption: str | None
    error: str | None


@router.get("/failures", response_model=list[EnrichmentFailure])
async def list_enrichment_failures(
    limit: int = 100,
    config: Config = Depends(get_config_dependency),
) -> list[EnrichmentFailure]:
    """Items whose last enrichment attempt failed, with the error recorded
    in `raw_metadata_json.enrichment_error` — the Enrich page's retry list."""
    with session_scope(config) as conn:
        rows = conn.execute(
            "SELECT id, caption, raw_metadata_json FROM items WHERE enrichment_status = ? "
            "ORDER BY id DESC LIMIT ?",
            (EnrichmentStatus.FAILED.value, limit),
        ).fetchall()

    out: list[EnrichmentFailure] = []
    for row in rows:
        error = None
        if row["raw_metadata_json"]:
            try:
                error = json.loads(row["raw_metadata_json"]).get("enrichment_error")
            except (json.JSONDecodeError, TypeError, AttributeError):
                error = None
        out.append(EnrichmentFailure(item_id=row["id"], caption=row["caption"], error=error))
    return out


@router.get("/progress/{item_id}", response_model=Item)
async def get_item_enrichment_status(
    item_id: int,
    config: Config = Depends(get_config_dependency),
) -> Item:
    """Fetch a single item (including `enrichment_status` and any populated
    `vision_caption`/`ocr_text`/`transcript` on its media files) — useful
    for polling one item's progress after a targeted `run_enrichment`."""
    with session_scope(config) as conn:
        items = fetch_items(conn, [item_id])
    item = items.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
    return item


# Re-exported for convenience so callers can filter on status without a
# second import from gramvault.models.schemas.
__all__ = ["router", "EnrichmentStatus"]
