"""Categorize API (the reels-workflow D2/D3 passes as a first-class step).

`POST /api/categorize/run` runs the keyword vote and/or the LLM re-label
over a scope of items as a `jobs.kind='categorize'` background job;
`GET /api/categorize/progress` is what the Categorize page polls. The
review queue itself is served by `GET /api/library/items?needs_review=1`.

The classifier logic lives in `gramvault.ai.classifier` — this module is
just the HTTP surface (scope resolution, readiness check, job wiring).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from gramvault.ai import classifier
from gramvault.ai.classifier import CategorizeMethod
from gramvault.ai.errors import ProviderNotReadyError
from gramvault.ai.providers import get_provider
from gramvault.api import jobs
from gramvault.api.deps import get_config_dependency
from gramvault.config import Config
from gramvault.db.session import session_scope
from gramvault.models.schemas import JobKind

router = APIRouter(prefix="/api/categorize", tags=["categorize"])

# An automatic label below this confidence is "needs review". Mirrors
# gramvault.ai.classifier.REVIEW_CONFIDENCE — kept as a literal here so the
# SQL reads plainly.
_NOT_MANUAL_WEAK = (
    "COALESCE(items.category_source, '') != 'manual' "
    "AND COALESCE(items.category_confidence, 0) < 0.6"
)

ScopeName = Literal["uncategorized", "needs_review", "all"]


class CategorizeRunRequest(BaseModel):
    # A named scope, or an explicit id list ({"item_ids": [...]}).
    scope: ScopeName | dict[str, list[int]] = "uncategorized"
    method: CategorizeMethod = "keyword_then_llm"


class CategorizeRunResponse(BaseModel):
    queued_count: int
    # Poll /api/jobs/{job_id} for progress/cancel. None when nothing matched.
    job_id: int | None = None


class CategorizeProgress(BaseModel):
    total: int
    categorized: int
    uncategorized: int
    # Items that got an auto label the classifier isn't confident about.
    needs_review: int
    by_source: dict[str, int]
    job_id: int | None = None


def _resolve_scope(conn, request: CategorizeRunRequest) -> list[int]:  # noqa: ANN001
    scope = request.scope
    if isinstance(scope, dict):
        return list(scope.get("item_ids") or [])
    if scope == "uncategorized":
        sql = "SELECT id FROM items WHERE category_id IS NULL"
    elif scope == "needs_review":
        sql = f"SELECT id FROM items WHERE category_id IS NOT NULL AND {_NOT_MANUAL_WEAK}"
    else:  # all
        sql = "SELECT id FROM items WHERE COALESCE(category_source, '') != 'manual'"
    return [row["id"] for row in conn.execute(sql)]


@router.post("/run", response_model=CategorizeRunResponse, status_code=202)
async def run_categorize(
    body: CategorizeRunRequest,
    background_tasks: BackgroundTasks,
    config: Config = Depends(get_config_dependency),
) -> CategorizeRunResponse:
    """Enqueue a categorize run. When the method uses the LLM, the
    `categorize` provider's readiness is checked up front so a
    misconfigured setup fails fast with a friendly 503."""
    with session_scope(config) as conn:
        item_ids = _resolve_scope(conn, body)

    if body.method in ("llm", "keyword_then_llm"):
        try:
            provider, model = get_provider("categorize", config)
            await provider.ensure_ready(model)
        except ProviderNotReadyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not item_ids:
        return CategorizeRunResponse(queued_count=0)

    with session_scope(config) as conn:
        try:
            job = jobs.create(
                conn,
                JobKind.CATEGORIZE,
                params={"count": len(item_ids), "method": body.method},
            )
        except jobs.JobConflict as exc:
            raise HTTPException(
                status_code=409, detail="A categorize job is already running"
            ) from exc

    assert job.id is not None
    method = body.method

    async def _work(ctx: jobs.JobContext) -> dict:
        ctx.progress(done=0, total=len(item_ids))
        return await classifier.categorize_items(
            item_ids,
            method,
            config,
            progress_cb=lambda done, total: ctx.progress(done=done, total=total),
            cancel_check=lambda: ctx.cancelled,
        )

    background_tasks.add_task(jobs.run, job.id, _work, config)
    return CategorizeRunResponse(queued_count=len(item_ids), job_id=job.id)


@router.get("/progress", response_model=CategorizeProgress)
async def get_categorize_progress(
    config: Config = Depends(get_config_dependency),
) -> CategorizeProgress:
    with session_scope(config) as conn:
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        uncategorized = conn.execute(
            "SELECT COUNT(*) FROM items WHERE category_id IS NULL"
        ).fetchone()[0]
        needs_review = conn.execute(
            f"SELECT COUNT(*) FROM items WHERE category_id IS NOT NULL AND {_NOT_MANUAL_WEAK}"
        ).fetchone()[0]
        source_rows = conn.execute(
            "SELECT category_source AS source, COUNT(*) AS n FROM items "
            "WHERE category_id IS NOT NULL GROUP BY category_source"
        ).fetchall()
        active = jobs.active(conn, JobKind.CATEGORIZE)

    return CategorizeProgress(
        total=total,
        categorized=total - uncategorized,
        uncategorized=uncategorized,
        needs_review=needs_review,
        by_source={row["source"] or "unknown": row["n"] for row in source_rows},
        job_id=active.id if active else None,
    )
