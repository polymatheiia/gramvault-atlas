"""Background job lifecycle for the Pipeline UI.

One `jobs` table row per run + cooperative cancellation + a database-level
single-flight guard (at most one active job per kind).

Reference use (see `api/routes_enrich.py`):

    from gramvault.api import jobs
    from gramvault.models.schemas import JobKind

    try:
        job = jobs.create(conn, JobKind.ENRICH, params={"item_ids": [...]})
    except jobs.JobConflict:
        raise HTTPException(409, "An enrichment job is already running")

    async def _work(ctx: jobs.JobContext) -> dict:
        for i, item_id in enumerate(item_ids):
            if ctx.cancelled:
                break
            await do_one(item_id)
            ctx.progress(done=i + 1, total=len(item_ids))
        return {"processed": i + 1}

    background_tasks.add_task(jobs.run, job.id, _work, config)

`jobs.run` moves the row pending -> running -> done/failed/cancelled and
records `result` / `error_message`. A job left `running` by a crashed
process is reclaimed to `failed` on the next app startup
(`gramvault.main`), and the `jobs` background task itself never raises.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from gramvault.config import Config, get_config
from gramvault.db.session import session_scope
from gramvault.models.schemas import Job, JobKind, JobStatus

logger = logging.getLogger(__name__)

_ACTIVE = (JobStatus.PENDING.value, JobStatus.RUNNING.value)

# "Heavy" = model-bound: these all drive Ollama + faster-whisper hard and
# must not overlap on a small box (INTEGRATION-PLAN.md §H2). Enforced in the
# DB by the partial UNIQUE index `idx_jobs_one_active_heavy` (migration
# 005) — keep this set identical to that index's kind list. `pull` /
# `model_pull` are network-bound and may still run alongside anything.
_HEAVY_KINDS = frozenset(
    {JobKind.ENRICH, JobKind.CATEGORIZE, JobKind.DIGEST, JobKind.REEMBED}
)


class JobConflict(RuntimeError):
    """Raised by `create()` when a job of the same kind is already active,
    or when a `_HEAVY_KINDS` job is requested while another heavy job runs.
    The message names the blocking job's kind."""


class JobNotFound(RuntimeError):
    """Raised when a job id doesn't exist."""


# --- CRUD ------------------------------------------------------------------


def create(
    conn: sqlite3.Connection, kind: JobKind, params: dict[str, Any] | None = None
) -> Job:
    """Insert a new `pending` job. Raises `JobConflict` if a job of the
    same kind is already pending or running (partial UNIQUE index
    `idx_jobs_one_active_per_kind`), or — for a `_HEAVY_KINDS` kind — if
    any other heavy job is active (`idx_jobs_one_active_heavy`, migration
    005). Both constraints are DB-enforced, so two racing requests can't
    both slip through."""
    try:
        cursor = conn.execute(
            "INSERT INTO jobs (kind, status, params_json) VALUES (?, 'pending', ?)",
            (kind.value, json.dumps(params) if params is not None else None),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise JobConflict(_conflict_reason(conn, kind)) from exc
    job = get(conn, cursor.lastrowid)
    assert job is not None  # just inserted
    return job


def _conflict_reason(conn: sqlite3.Connection, kind: JobKind) -> str:
    """Message for the `JobConflict` an INSERT just tripped — tells a
    same-kind conflict apart from a cross-kind heavy-job one. Routes pass
    this straight through as the 409 detail."""
    if active(conn, kind) is not None:
        return (
            f"There's already a running {kind.value} job — wait for it to "
            "finish or cancel it first."
        )
    blocker = active_heavy(conn)
    if blocker is not None and blocker.kind != kind:
        return (
            f"There's already a running {blocker.kind.value} job — enrich, "
            "categorize, digest and re-embed can't run at the same time on this "
            "box. Wait for it to finish or cancel it first."
        )
    return (
        f"There's already a running {kind.value} job — wait for it to finish "
        "or cancel it first."
    )


def get(conn: sqlite3.Connection, job_id: int | None) -> Job | None:
    if job_id is None:
        return None
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return Job.from_row(row) if row is not None else None


def list_jobs(
    conn: sqlite3.Connection,
    *,
    kind: JobKind | None = None,
    status: JobStatus | None = None,
    limit: int = 50,
) -> list[Job]:
    clauses: list[str] = []
    params: list[object] = []
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind.value)
    if status is not None:
        clauses.append("status = ?")
        params.append(status.value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM jobs {where} ORDER BY id DESC LIMIT ?", [*params, limit]
    ).fetchall()
    return [Job.from_row(row) for row in rows]


def active(conn: sqlite3.Connection, kind: JobKind) -> Job | None:
    """The current pending/running job of `kind`, if any."""
    row = conn.execute(
        "SELECT * FROM jobs WHERE kind = ? AND status IN ('pending', 'running') "
        "ORDER BY id DESC LIMIT 1",
        (kind.value,),
    ).fetchone()
    return Job.from_row(row) if row is not None else None


def active_heavy(conn: sqlite3.Connection) -> Job | None:
    """The current pending/running `_HEAVY_KINDS` job, if any. At most one
    can exist (`idx_jobs_one_active_heavy`)."""
    placeholders = ", ".join("?" * len(_HEAVY_KINDS))
    row = conn.execute(
        f"SELECT * FROM jobs WHERE status IN ('pending', 'running') "
        f"AND kind IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        tuple(k.value for k in _HEAVY_KINDS),
    ).fetchone()
    return Job.from_row(row) if row is not None else None


def request_cancel(conn: sqlite3.Connection, job_id: int) -> Job:
    """Flag a job for cooperative cancellation. Raises `JobNotFound`.
    A no-op on a job that already finished (returns it unchanged)."""
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise JobNotFound(str(job_id))
    if row["status"] in _ACTIVE:
        conn.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
        conn.commit()
    job = get(conn, job_id)
    assert job is not None
    return job


# --- execution -----------------------------------------------------------


@dataclass
class JobContext:
    """Handed to a job's `work` callable. `progress()` persists a
    kind-specific dict; `cancelled` re-reads the DB so a cancel request
    that arrives mid-run is seen."""

    job_id: int
    config: Config

    def progress(self, **fields: Any) -> None:
        with session_scope(self.config) as conn:
            conn.execute(
                "UPDATE jobs SET progress_json = ? WHERE id = ?",
                (json.dumps(fields), self.job_id),
            )

    @property
    def cancelled(self) -> bool:
        with session_scope(self.config) as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (self.job_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])


WorkFn = Callable[[JobContext], Awaitable[dict[str, Any] | None]]


async def run(job_id: int, work: WorkFn, config: Config | None = None) -> None:
    """Execute `work` for job `job_id`, moving the row through
    running -> done/failed/cancelled. Never raises — a failure is
    recorded on the row so a batch UI can show it."""
    config = config or get_config()

    with session_scope(config) as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', started_at = datetime('now') WHERE id = ?",
            (job_id,),
        )

    ctx = JobContext(job_id=job_id, config=config)
    status = JobStatus.DONE
    result: dict[str, Any] | None = None
    error: str | None = None
    try:
        result = await work(ctx)
        if ctx.cancelled:
            status = JobStatus.CANCELLED
    except Exception as exc:  # noqa: BLE001 - any failure marks the job failed
        logger.exception("job %s failed", job_id)
        status = JobStatus.FAILED
        error = str(exc)

    with session_scope(config) as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, result_json = ?, error_message = ?, "
            "finished_at = datetime('now') WHERE id = ?",
            (
                status.value,
                json.dumps(result) if result is not None else None,
                error,
                job_id,
            ),
        )


def reclaim_orphans(conn: sqlite3.Connection) -> int:
    """Mark every pending/running `jobs` row as failed — call once at app
    startup to clear jobs abandoned by a previous process. Returns the
    number reclaimed."""
    cursor = conn.execute(
        "UPDATE jobs SET status = 'failed', "
        "error_message = COALESCE(error_message, 'interrupted by restart'), "
        "finished_at = datetime('now') "
        "WHERE status IN ('pending', 'running')"
    )
    return cursor.rowcount
