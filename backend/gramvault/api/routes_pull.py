"""Instagram "pull my saved posts" API (integration plan §F).

Opt-in and off by default (`config.pull.enabled`). The flow the Pull page
drives:

    GET  /api/pull/session        -> {enabled, configured, username, ...}
    POST /api/pull/connect        -> validate pasted cookies, cache a session
    POST /api/pull/connect-local  -> same, reading this box's Firefox store
    DELETE /api/pull/session      -> forget the cached session
    POST /api/pull/run            -> jobs.kind='pull' background walk+import
    GET  /api/pull/progress       -> what the page polls (mirrors categorize)

The scrape + download + ingest logic all lives in
`gramvault.ingestion.instagram`; this module is just the HTTP surface
(gating, error translation, job wiring).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from gramvault.api import jobs
from gramvault.api.deps import get_config_dependency
from gramvault.config import Config
from gramvault.db.session import session_scope
from gramvault.ingestion import instagram
from gramvault.models.schemas import JobKind, JobStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pull", tags=["pull"])


# --- schemas -----------------------------------------------------------


class PullSessionResponse(BaseModel):
    # Whether pulling is switched on at all (config.pull.enabled).
    enabled: bool
    # Whether a validated Instagram session is cached on disk.
    configured: bool = False
    username: str | None = None
    last_verified_at: str | None = None


class ConnectRequest(BaseModel):
    # A cookie export: JSON object, Cookie-Editor array, or Netscape text.
    cookies: str = Field(min_length=1)


class ConnectLocalRequest(BaseModel):
    # Explicit opt-in required (audit finding S5): connect-local reads
    # this box's Firefox cookie jar, which is sensitive even for a
    # deliberate, authenticated caller — a bare POST with no body must
    # not be enough to trigger it.
    confirm: bool = False


class PullRunRequest(BaseModel):
    # How many saved posts to walk back through before stopping. None -> the
    # configured default (`pull.max_default`).
    max_count: int | None = Field(default=None, ge=1, le=5000)
    # Stop once this many already-imported posts are seen in a row (you've
    # caught up). Ignored when `full` is true.
    stop_after_known: int = Field(default=5, ge=1, le=100)
    # Walk the whole feed (up to `max_count`) even past posts already saved.
    full: bool = False


class PullRunResponse(BaseModel):
    job_id: int | None = None
    # Echoes the effective walk cap so the UI can show it.
    max_count: int


class PullProgress(BaseModel):
    enabled: bool
    configured: bool
    job_id: int | None = None
    # Live counters from the running job (or the last finished one).
    scanned: int = 0
    new: int = 0
    downloaded: int = 0
    imported: int = 0
    linked: int = 0
    failed: int = 0
    stopped_reason: str | None = None
    last_status: JobStatus | None = None
    error_message: str | None = None
    new_item_ids: list[int] = Field(default_factory=list)


# --- helpers -----------------------------------------------------------


def _require_enabled(config: Config) -> None:
    if not config.pull.enabled:
        raise HTTPException(
            status_code=403,
            detail=(
                "Pulling from Instagram is off. Set `pull.enabled: true` in config.yaml "
                "to turn it on (and install the extra: pip install -e \".[instagram]\")."
            ),
        )


def _session_response(config: Config) -> PullSessionResponse:
    state = instagram.get_session_state(config)
    return PullSessionResponse(
        enabled=config.pull.enabled,
        configured=state.configured,
        username=state.username,
        last_verified_at=state.last_verified_at,
    )


# --- routes ------------------------------------------------------------


@router.get("/session", response_model=PullSessionResponse)
def get_session(config: Config = Depends(get_config_dependency)) -> PullSessionResponse:
    return _session_response(config)


@router.post("/connect", response_model=PullSessionResponse)
def connect(
    body: ConnectRequest, config: Config = Depends(get_config_dependency)
) -> PullSessionResponse:
    _require_enabled(config)
    try:
        jar = instagram.parse_cookies(body.cookies)
        instagram.connect_from_cookies(jar, config)
    except instagram.InstagramDependencyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except instagram.InstagramAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _session_response(config)


@router.post("/connect-local", response_model=PullSessionResponse)
def connect_local(
    body: ConnectLocalRequest = ConnectLocalRequest(),
    config: Config = Depends(get_config_dependency),
) -> PullSessionResponse:
    _require_enabled(config)
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Pass {\"confirm\": true} to read this box's Firefox cookie jar.",
        )
    try:
        instagram.connect_from_local_browser(config)
    except instagram.InstagramDependencyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except instagram.InstagramAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _session_response(config)


@router.delete("/session", response_model=PullSessionResponse)
def delete_session(config: Config = Depends(get_config_dependency)) -> PullSessionResponse:
    instagram.disconnect(config)
    return _session_response(config)


@router.post("/run", response_model=PullRunResponse, status_code=202)
async def run_pull(
    body: PullRunRequest,
    background_tasks: BackgroundTasks,
    config: Config = Depends(get_config_dependency),
) -> PullRunResponse:
    _require_enabled(config)

    state = instagram.get_session_state(config)
    if not state.configured:
        raise HTTPException(
            status_code=503,
            detail="Not connected to Instagram — add a session on the Pull page first.",
        )

    max_count = body.max_count or config.pull.max_default
    stop_after_known = body.stop_after_known
    download_all = body.full

    with session_scope(config) as conn:
        try:
            job = jobs.create(
                conn,
                JobKind.PULL,
                params={"max_count": max_count, "full": download_all},
            )
        except jobs.JobConflict as exc:
            raise HTTPException(
                status_code=409, detail="A pull job is already running"
            ) from exc

    assert job.id is not None

    async def _work(ctx: jobs.JobContext) -> dict:
        ctx.progress(scanned=0, new=0, downloaded=0)
        result = await asyncio.to_thread(
            instagram.pull_saved,
            config,
            max_count=max_count,
            stop_after_known=stop_after_known,
            download_all=download_all,
            progress_cb=lambda fields: ctx.progress(**fields),
            cancel_check=lambda: ctx.cancelled,
        )
        return result.as_dict()

    background_tasks.add_task(jobs.run, job.id, _work, config)
    return PullRunResponse(job_id=job.id, max_count=max_count)


@router.get("/progress", response_model=PullProgress)
def get_progress(config: Config = Depends(get_config_dependency)) -> PullProgress:
    state = instagram.get_session_state(config)
    with session_scope(config) as conn:
        active = jobs.active(conn, JobKind.PULL)
        last = jobs.list_jobs(conn, kind=JobKind.PULL, limit=1)
    job = active or (last[0] if last else None)

    out = PullProgress(
        enabled=config.pull.enabled,
        configured=state.configured,
        job_id=active.id if active else None,
    )
    if job is None:
        return out

    out.last_status = job.status
    out.error_message = job.error_message
    # While running: the live progress dict. Once finished: the result dict
    # (a cancelled pull still imports and reports what it managed to fetch).
    finished = job.status in (JobStatus.DONE, JobStatus.CANCELLED)
    source = job.result if finished and job.result else (job.progress or {})
    for key in ("scanned", "new", "downloaded", "imported", "linked", "failed"):
        if isinstance(source.get(key), int):
            setattr(out, key, source[key])
    if isinstance(source.get("stopped_reason"), str):
        out.stopped_reason = source["stopped_reason"]
    if isinstance(source.get("new_item_ids"), list):
        out.new_item_ids = [i for i in source["new_item_ids"] if isinstance(i, int)]
    return out
