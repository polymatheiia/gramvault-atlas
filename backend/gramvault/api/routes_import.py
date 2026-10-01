"""Import/ingestion API.

Owns: uploading an Instagram data export ZIP, unpacking it, creating
authors/items/media_files rows, and tracking progress via `import_jobs`
so a failed/interrupted import can resume rather than restart.

The actual parsing/organizing/DB-writing logic lives in
`gramvault.ingestion` (shared with the `gramvault import` CLI command —
see `gramvault.cli.import_export`) so there's exactly one implementation
behind both entry points.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from gramvault.api.deps import get_config_dependency
from gramvault.config import Config
from gramvault.ingestion import importer
from gramvault.ingestion.parser import ExportFormatError
from gramvault.models.schemas import ImportJob, JobStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/import", tags=["import"])

# Read/write chunk size while streaming an upload to disk.
_UPLOAD_CHUNK_BYTES = 1024 * 1024


class ImportJobListResponse(BaseModel):
    jobs: list[ImportJob]


async def _stream_upload_to_disk(file: UploadFile, dest: Path, max_bytes: int) -> None:
    """Write `file` to `dest` in chunks, refusing once `max_bytes` is
    exceeded rather than buffering an unbounded upload (audit finding S9
    covered ZIP members; an upload with no cap at all is the same class
    of problem one level up). Raises HTTPException(413) and removes the
    partial file if the cap is hit."""
    total = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Upload exceeds the {max_bytes} byte limit (import.max_upload_bytes).",
                    )
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


async def _run_import_in_background(job_id: int, dest: Path, config: Config) -> None:
    """Background-task entry point: runs the (blocking) import in a worker
    thread so it never holds up the event loop (audit finding R2), then
    cleans up the staged upload on success. Never raises — a failure is
    recorded on the job row, same contract as `api.jobs.run`."""
    try:
        job = await asyncio.to_thread(importer.run_import, job_id, dest, config)
    except Exception as exc:  # noqa: BLE001 - any failure marks the job failed
        logger.exception("import job %s failed", job_id)
        job = importer.fail_job(job_id, f"Unexpected error during import: {exc}", config)
    if job.status == JobStatus.DONE:
        dest.unlink(missing_ok=True)


@router.post("/upload", response_model=ImportJob, status_code=202)
async def upload_export(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Instagram data export ZIP file"),
    config: Config = Depends(get_config_dependency),
) -> ImportJob:
    """Upload an Instagram export ZIP and start the import in the background.

    The upload is saved under `config.resolved_imports_dir` (never
    trusting the client-supplied filename beyond its basename) — outside
    `library_dir`, since that's served at `/media` and a "Download Your
    Information" export can contain far more than saved posts (audit
    finding S8).

    Returns immediately with the job in `pending`/`running` state; the
    actual parse-and-write work runs in a background thread
    (`asyncio.to_thread`) so a large media-bearing export no longer
    blocks every other request — including `/api/health` and job
    polling — for its whole duration (audit finding R2). Poll
    `GET /api/import/jobs/{id}` for progress, or
    `POST /api/import/jobs/{id}/cancel` to stop it.

    A ZIP that doesn't look like a real Instagram export doesn't raise an
    HTTP error here — the job ends up `status="failed"` with a friendly
    `error_message`, so the frontend can show it inline rather than
    having to special-case a 4xx/5xx. The uploaded ZIP is deleted after a
    successful import; kept on failure so it can be inspected/retried.
    """
    safe_name = Path(file.filename or "export.zip").name or "export.zip"
    imports_dir = config.resolved_imports_dir
    imports_dir.mkdir(parents=True, exist_ok=True)
    dest = imports_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"

    await _stream_upload_to_disk(file, dest, config.import_.max_upload_bytes)

    job = importer.create_import_job(dest, config)
    assert job.id is not None
    background_tasks.add_task(_run_import_in_background, job.id, dest, config)
    return job


@router.get("/jobs", response_model=ImportJobListResponse)
async def list_import_jobs(
    config: Config = Depends(get_config_dependency),
) -> ImportJobListResponse:
    """List all import jobs, most recent first."""
    return ImportJobListResponse(jobs=importer.list_import_jobs(config))


@router.get("/jobs/{job_id}", response_model=ImportJob)
async def get_import_job(
    job_id: int,
    config: Config = Depends(get_config_dependency),
) -> ImportJob:
    """Fetch a single import job's current status/progress. Used by the
    frontend to poll progress after `upload_export`."""
    job = importer.get_import_job(job_id, config)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Import job {job_id} not found")
    return job


@router.post("/jobs/{job_id}/cancel", response_model=ImportJob)
async def cancel_import_job(
    job_id: int,
    config: Config = Depends(get_config_dependency),
) -> ImportJob:
    """Request cancellation of an in-progress import job.

    Sets `cancel_requested`; the background import checks it every
    `_CHECKPOINT_ITEMS` items and stops there, ending the job `failed`
    with `error_message='cancelled by user'`. A no-op on a job that
    already finished.
    """
    job = importer.cancel_import_job(job_id, config)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Import job {job_id} not found")
    return job


class LinkMediaRequest(BaseModel):
    source_dir: str
    # "copy" would shadow pydantic's BaseModel.copy(); copy_files avoids
    # the warning while keeping the CLI's --copy semantics.
    copy_files: bool = False


class LinkMediaResponse(BaseModel):
    files_scanned: int
    files_matched: int
    files_linked: int
    items_linked: int
    items_already_linked: int
    unmatched_files: int
    failed_files: int
    unmatched_examples: list[str]
    summary: str


@router.post("/link-media", response_model=LinkMediaResponse)
async def link_media(
    body: LinkMediaRequest,
    config: Config = Depends(get_config_dependency),
) -> LinkMediaResponse:
    """Attach separately downloaded media to already-imported items whose
    export carried no media (someone else's saved post) — the server-side
    counterpart of `gramvault link-media` (audit §5.1). Runs in a worker
    thread (file scanning + hashing) rather than on the event loop, same
    reasoning as import (R2); unlike import there's no natural chunked
    progress to report mid-run, so this is request/response, not a job."""
    from gramvault.ingestion.linker import link_local_media

    try:
        report = await asyncio.to_thread(
            link_local_media, Path(body.source_dir), config, copy=body.copy_files
        )
    except NotADirectoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return LinkMediaResponse(
        files_scanned=report.files_scanned,
        files_matched=report.files_matched,
        files_linked=report.files_linked,
        items_linked=report.items_linked,
        items_already_linked=report.items_already_linked,
        unmatched_files=report.unmatched_files,
        failed_files=report.failed_files,
        unmatched_examples=report.unmatched_examples,
        summary=report.summary(),
    )


# Re-exported for readability at call sites that only need the enum, e.g.
# tests asserting on job.status without importing gramvault.models.schemas.
__all__ = ["router", "ExportFormatError", "JobStatus"]
