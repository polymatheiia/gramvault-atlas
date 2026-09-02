"""Digests API — generate a Markdown recommendation/notes doc from a
selection of items via a template (the reels-workflow §D map/reduce step
as a first-class feature).

    GET  /api/digests/templates        the bundled + user templates
    POST /api/digests/preflight        item count / token / cost estimate
    POST /api/digests                  create + run (jobs.kind='digest')
    GET  /api/digests                  history (newest first, no markdown)
    GET  /api/digests/{id}             one digest with its markdown + manifest
    GET  /api/digests/{id}/download    the markdown as a file attachment

The engine is `gramvault.ai.digest`; this module is scope resolution,
readiness check, and job wiring.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from gramvault.ai import digest as digest_engine
from gramvault.ai.digest import DigestError, Selection
from gramvault.ai.errors import ProviderNotReadyError
from gramvault.ai.providers import get_provider
from gramvault.api import jobs
from gramvault.api.deps import get_config_dependency
from gramvault.config import Config
from gramvault.db.session import session_scope
from gramvault.models.schemas import Digest, JobKind

router = APIRouter(prefix="/api/digests", tags=["digests"])


class TemplateInfo(BaseModel):
    name: str
    description: str
    extract_prompt: str
    reduce_prompt: str
    version: str
    source: str
    default_task: str


class SelectionRequest(BaseModel):
    category: str | None = None
    query: str | None = None
    item_ids: list[int] | None = None

    def to_selection(self) -> Selection:
        return Selection(
            category=self.category, query=self.query, item_ids=list(self.item_ids or [])
        )


class PreflightRequest(SelectionRequest):
    template: str


class PreflightResponse(BaseModel):
    item_count: int
    batches: int
    estimated_tokens_in: int
    estimated_tokens_out: int
    estimated_cost: float | None
    provider: str
    model: str


class DigestCreateRequest(PreflightRequest):
    name: str | None = None


class DigestCreateResponse(BaseModel):
    digest_id: int
    job_id: int
    item_count: int


class DigestProgress(BaseModel):
    job_id: int | None = None
    step: int = 0
    total: int = 0


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "digest"


async def _resolve(config: Config, selection: Selection) -> list[int]:
    with session_scope(config) as conn:
        return await digest_engine.select_items(conn, selection, config)


# --- templates --------------------------------------------------------


@router.get("/templates", response_model=list[TemplateInfo])
async def list_templates(
    config: Config = Depends(get_config_dependency),
) -> list[TemplateInfo]:
    templates = digest_engine.load_templates(config)
    return [
        TemplateInfo(
            name=t.name,
            description=t.description,
            extract_prompt=t.extract_prompt,
            reduce_prompt=t.reduce_prompt,
            version=t.version,
            source=t.source,
            default_task=t.default_task,
        )
        for t in sorted(templates.values(), key=lambda t: t.name)
    ]


# --- preflight -------------------------------------------------------


@router.post("/preflight", response_model=PreflightResponse)
async def digest_preflight(
    body: PreflightRequest,
    config: Config = Depends(get_config_dependency),
) -> PreflightResponse:
    try:
        template = digest_engine.get_template(body.template, config)
        item_ids = await _resolve(config, body.to_selection())
    except DigestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    estimate = await digest_engine.preflight(item_ids, template, config)
    return PreflightResponse(
        item_count=estimate.item_count,
        batches=estimate.batches,
        estimated_tokens_in=estimate.tokens_in,
        estimated_tokens_out=estimate.tokens_out,
        estimated_cost=estimate.cost_estimate,
        provider=estimate.provider,
        model=estimate.model,
    )


# --- create + run ---------------------------------------------------


@router.post("", response_model=DigestCreateResponse, status_code=202)
async def create_digest(
    body: DigestCreateRequest,
    background_tasks: BackgroundTasks,
    config: Config = Depends(get_config_dependency),
) -> DigestCreateResponse:
    try:
        template = digest_engine.get_template(body.template, config)
        selection = body.to_selection()
        item_ids = await _resolve(config, selection)
    except DigestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not item_ids:
        raise HTTPException(status_code=422, detail="the selection matched no items")

    try:
        provider, model = get_provider(template.default_task, config)
        await provider.ensure_ready(model)
    except ProviderNotReadyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    name = (body.name or "").strip() or f"{template.name} — {len(item_ids)} items"

    with session_scope(config) as conn:
        try:
            job = jobs.create(conn, JobKind.DIGEST, params={"template": template.name})
        except jobs.JobConflict as exc:
            raise HTTPException(status_code=409, detail="A digest job is already running") from exc
        assert job.id is not None
        cursor = conn.execute(
            "INSERT INTO digests (name, template, template_version, status, selection_json, "
            "item_ids_json, job_id) VALUES (?, ?, ?, 'pending', ?, ?, ?)",
            (
                name,
                template.name,
                template.version,
                json.dumps(selection.to_json()),
                json.dumps(item_ids),
                job.id,
            ),
        )
        digest_id = cursor.lastrowid
    assert digest_id is not None

    async def _work(ctx: jobs.JobContext) -> dict:
        ctx.progress(step=0, total=1)
        try:
            return await digest_engine.run_digest(
                digest_id,
                config,
                progress_cb=lambda step, total: ctx.progress(step=step, total=total),
                cancel_check=lambda: ctx.cancelled,
            )
        except Exception as exc:
            with session_scope(config) as conn:
                conn.execute(
                    "UPDATE digests SET status = 'failed', error_message = ?, "
                    "finished_at = datetime('now') WHERE id = ? AND status != 'done'",
                    (str(exc), digest_id),
                )
            raise

    background_tasks.add_task(jobs.run, job.id, _work, config)
    return DigestCreateResponse(digest_id=digest_id, job_id=job.id, item_count=len(item_ids))


# --- read -----------------------------------------------------------


@router.get("", response_model=list[Digest])
async def list_digests(
    limit: int = 50,
    config: Config = Depends(get_config_dependency),
) -> list[Digest]:
    """History, newest first. `markdown` is omitted here — fetch a single
    digest for the body."""
    with session_scope(config) as conn:
        rows = conn.execute(
            "SELECT * FROM digests ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)
        ).fetchall()
    digests = []
    for row in rows:
        d = Digest.from_row(row)
        d.markdown = None
        digests.append(d)
    return digests


def _load_digest(config: Config, digest_id: int) -> Digest:
    with session_scope(config) as conn:
        row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Digest {digest_id} not found")
    return Digest.from_row(row)


@router.get("/{digest_id}", response_model=Digest)
async def get_digest(
    digest_id: int,
    config: Config = Depends(get_config_dependency),
) -> Digest:
    return _load_digest(config, digest_id)


@router.get("/{digest_id}/download", response_class=PlainTextResponse)
async def download_digest(
    digest_id: int,
    config: Config = Depends(get_config_dependency),
) -> PlainTextResponse:
    digest = _load_digest(config, digest_id)
    if not digest.markdown:
        raise HTTPException(status_code=409, detail="This digest has no markdown yet")
    filename = f"{_slugify(digest.name)}.md"
    return PlainTextResponse(
        digest.markdown,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
