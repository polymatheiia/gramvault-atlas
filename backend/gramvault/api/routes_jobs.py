"""Jobs API — one polling surface for every Pipeline background job
(enrich / categorize / digest / pull / model_pull / reembed).

The job lifecycle logic lives in `gramvault.api.jobs`; this module is just
the HTTP surface.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from gramvault.api import jobs
from gramvault.api.deps import get_config_dependency
from gramvault.config import Config
from gramvault.db.session import session_scope
from gramvault.models.schemas import Job, JobKind, JobStatus

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("", response_model=list[Job])
async def list_jobs(
    kind: JobKind | None = Query(default=None),
    status: JobStatus | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    config: Config = Depends(get_config_dependency),
) -> list[Job]:
    """Recent jobs, newest first, optionally filtered by kind and status."""
    with session_scope(config) as conn:
        return jobs.list_jobs(conn, kind=kind, status=status, limit=limit)


@router.get("/{job_id}", response_model=Job)
async def get_job(
    job_id: int,
    config: Config = Depends(get_config_dependency),
) -> Job:
    with session_scope(config) as conn:
        job = jobs.get(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


@router.post("/{job_id}/cancel", response_model=Job)
async def cancel_job(
    job_id: int,
    config: Config = Depends(get_config_dependency),
) -> Job:
    """Request cooperative cancellation. Idempotent, and a no-op on a job
    that already finished — the job runner checks the flag between units
    of work, so cancellation isn't instantaneous."""
    with session_scope(config) as conn:
        try:
            return jobs.request_cancel(conn, job_id)
        except jobs.JobNotFound as exc:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found") from exc
