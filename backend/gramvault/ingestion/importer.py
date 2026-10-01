"""Import orchestration: parse a ZIP export, organize any embedded media,
and write authors/items/media_files rows — all tracked through an
`import_jobs` row (pending -> running -> done/failed) so progress can be
polled while it runs and so a re-run on the same ZIP is a cheap no-op for
items already imported.

This module is the single implementation shared by both:
  - `POST /api/import/upload` (`gramvault.api.routes_import`)
  - `gramvault import <zip>` (`gramvault.cli`)
so behavior never diverges between the two entry points.

Resumability: items are deduped by `external_id`, which is unique in the
`items` table. For saved (link-only) items this is the Instagram
shortcode parsed out of the post URL (or a stable hash of the URL if no
shortcode is present); for the user's own posts it's a stable hash of the
first media file's zip path (or of the caption+timestamp if no media
uri is available). Either way, re-running `run_import` against a job
whose items already exist in the DB skips them rather than erroring or
duplicating rows.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import zipfile
from collections.abc import Callable
from pathlib import Path

from gramvault.chat import fts
from gramvault.config import Config, get_config
from gramvault.db.session import session_scope
from gramvault.ingestion import mediainfo
from gramvault.ingestion.mediainfo import MediaDimensions
from gramvault.ingestion.organizer import OrganizedMediaFile, organize_zip_member
from gramvault.ingestion.parser import (
    ExportFormatError,
    OwnMediaFile,
    OwnPost,
    SavedItem,
    parse_export,
)
from gramvault.models.schemas import ImportJob, JobStatus

logger = logging.getLogger(__name__)

# --- import_jobs CRUD helpers (used by both this module and the API routes) --


def _row_to_job(row: sqlite3.Row) -> ImportJob:
    return ImportJob.model_validate(dict(row))


def create_import_job(zip_path: Path, config: Config | None = None) -> ImportJob:
    """Insert a new `import_jobs` row (status=pending) for `zip_path`."""
    config = config or get_config()
    with session_scope(config) as conn:
        cursor = conn.execute(
            "INSERT INTO import_jobs (source_path, status) VALUES (?, ?)",
            (str(zip_path), JobStatus.PENDING.value),
        )
        row = conn.execute(
            "SELECT * FROM import_jobs WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return _row_to_job(row)


def get_import_job(job_id: int, config: Config | None = None) -> ImportJob | None:
    config = config or get_config()
    with session_scope(config) as conn:
        row = conn.execute("SELECT * FROM import_jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row is not None else None


def list_import_jobs(config: Config | None = None) -> list[ImportJob]:
    config = config or get_config()
    with session_scope(config) as conn:
        rows = conn.execute(
            "SELECT * FROM import_jobs ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return [_row_to_job(row) for row in rows]


def cancel_import_job(job_id: int, config: Config | None = None) -> ImportJob | None:
    """Request cancellation of an in-progress import.

    Import now runs in a background thread (`run_import` is invoked via
    `asyncio.to_thread`), so this can no longer finalize the job itself —
    it only flips `cancel_requested`, which `run_import`'s loops check
    between items and honour by stopping and marking the job `failed`
    with `error_message='cancelled by user'` (see module docstring: the
    `status` CHECK constraint predates a real `'cancelled'` value). A job
    that isn't pending/running is returned unchanged; one left `running`
    by a crashed process is reclaimed at the next startup
    (`gramvault.main`), same as the generic `jobs` table.
    """
    config = config or get_config()
    with session_scope(config) as conn:
        row = conn.execute("SELECT * FROM import_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        job = _row_to_job(row)
        if job.status in (JobStatus.DONE, JobStatus.FAILED):
            return job
        conn.execute("UPDATE import_jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
        row = conn.execute("SELECT * FROM import_jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row)


def _cancel_requested(job_id: int, config: Config) -> bool:
    with session_scope(config) as conn:
        row = conn.execute(
            "SELECT cancel_requested FROM import_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return bool(row and row["cancel_requested"])


# --- DB write helpers ------------------------------------------------------


def _get_or_create_author(
    conn: sqlite3.Connection, username: str | None, full_name: str | None = None
) -> int | None:
    if not username:
        return None
    conn.execute("INSERT OR IGNORE INTO authors (username) VALUES (?)", (username,))
    if full_name:
        # Backfill only — don't let a later export's blank overwrite a name
        # we already have.
        conn.execute(
            "UPDATE authors SET full_name = ? WHERE username = ? AND full_name IS NULL",
            (full_name, username),
        )
    row = conn.execute("SELECT id FROM authors WHERE username = ?", (username,)).fetchone()
    return row["id"] if row is not None else None


def _attach_hashtags(conn: sqlite3.Connection, item_id: int, hashtags: list[str]) -> None:
    """Record the export's hashtags as `kind='hashtag'` tags, keeping them
    distinct from 'auto' (AI-pipeline enrichment) and 'manual' (user-entered) so
    each source stays attributable."""
    for name in hashtags:
        conn.execute("INSERT OR IGNORE INTO tags (name, kind) VALUES (?, 'hashtag')", (name,))
        row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        if row is None:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?)",
            (item_id, row["id"]),
        )


def _item_exists(conn: sqlite3.Connection, external_id: str | None) -> bool:
    if not external_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM items WHERE external_id = ?", (external_id,)
    ).fetchone()
    return row is not None


def _import_saved_item(conn: sqlite3.Connection, saved: SavedItem, job_id: int) -> None:
    if _item_exists(conn, saved.external_id):
        return  # already imported in a previous run of this (or another) job
    author_id = _get_or_create_author(conn, saved.author_username, saved.author_full_name)
    cursor = conn.execute(
        """
        INSERT INTO items
            (external_id, author_id, media_type, caption, permalink, taken_at,
             import_job_id, raw_metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            saved.external_id,
            author_id,
            saved.media_type_guess.value,
            # Older exports carry no real caption for someone else's post, so
            # `caption` stays NULL there rather than misrepresenting the
            # title as one. Newer (`label_values`) exports do include it.
            saved.caption,
            saved.instagram_url,
            saved.saved_at.isoformat() if saved.saved_at else None,
            job_id,
            json.dumps(saved.raw, default=str),
        ),
    )
    if saved.hashtags and cursor.lastrowid is not None:
        _attach_hashtags(conn, cursor.lastrowid, saved.hashtags)


def _import_own_post(
    conn: sqlite3.Connection,
    zf: zipfile.ZipFile,
    post: OwnPost,
    job_id: int,
    library_dir: Path,
    config: Config,
) -> None:
    if _item_exists(conn, post.external_id):
        return

    # File work first, before the first INSERT opens a write transaction:
    # extracting + hashing a large video and ffprobing it can take seconds,
    # and SQLite's single write lock held that long stalls every other
    # writer in the app (cancel requests, enrich progress, tag edits).
    # R11: width/height/duration_seconds were in the schema but never
    # populated — nothing called ffprobe. Best-effort (None on
    # failure/missing ffprobe): metadata, not required for the row.
    prepared: list[tuple[OwnMediaFile, OrganizedMediaFile, MediaDimensions | None]] = []
    for media in post.media_files:
        if media.zip_member_name is None:
            continue  # media bytes weren't found in this export; item metadata is still kept
        organized = organize_zip_member(
            zf,
            media.zip_member_name,
            library_dir,
            max_bytes=config.import_.max_member_bytes,
            max_ratio=config.import_.max_compression_ratio,
        )
        if organized is None:
            continue
        prepared.append((media, organized, mediainfo.probe(library_dir / organized.file_path)))

    cursor = conn.execute(
        """
        INSERT INTO items
            (external_id, author_id, media_type, caption, permalink, taken_at,
             import_job_id, raw_metadata_json)
        VALUES (?, NULL, ?, ?, NULL, ?, ?, ?)
        """,
        (
            post.external_id,
            post.media_type.value,
            post.caption,
            post.posted_at.isoformat() if post.posted_at else None,
            job_id,
            json.dumps(post.raw, default=str),
        ),
    )
    item_id = cursor.lastrowid
    for media, organized, dims in prepared:
        conn.execute(
            """
            INSERT INTO media_files
                (item_id, file_path, media_type, sequence_index, checksum,
                 width, height, duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                organized.file_path,
                media.file_type.value,
                media.sequence_index,
                organized.checksum,
                dims.width if dims else None,
                dims.height if dims else None,
                dims.duration_seconds if dims else None,
            ),
        )


# --- orchestration ----------------------------------------------------------

# How often (in items) the main loops persist progress and re-check
# `cancel_requested` — frequent enough that a poll or a cancel request
# sees a response within a couple of seconds on a real export, without a
# DB write on every single item.
_CHECKPOINT_ITEMS = 25


def _import_one(conn: sqlite3.Connection, import_fn: Callable[..., None], *args: object) -> bool:
    """Import one item as its own transaction. Committing per item keeps
    SQLite's single write lock held only for that item's INSERTs — one
    transaction around the whole loop used to hide progress until the end
    and block every other writer (including the cancel request itself) for
    the entire import. A failed item is rolled back on its own, without
    leaving partial rows behind. Returns False if the item failed."""
    try:
        import_fn(conn, *args)
        conn.commit()
    except Exception:  # noqa: BLE001 - one bad item mustn't end the import
        conn.rollback()
        logger.warning("import: skipping an item that failed to import", exc_info=True)
        return False
    return True


def _record_progress(conn: sqlite3.Connection, job_id: int, processed: int, failed: int) -> None:
    conn.execute(
        "UPDATE import_jobs SET processed_items = ?, failed_items = ? WHERE id = ?",
        (processed, failed, job_id),
    )
    conn.commit()


def run_import(job_id: int, zip_path: Path, config: Config | None = None) -> ImportJob:
    """Run (or resume) the import for an existing `import_jobs` row.

    Safe to call more than once for the same job/ZIP: items already
    present (matched by `external_id`) are skipped rather than
    duplicated, and `processed_items`/`failed_items` are recomputed each
    run rather than accumulated across runs.

    Intended to run off the event loop (`asyncio.to_thread` from
    `api.routes_import`) — the caller gets a `pending`/`running` job back
    immediately and polls `GET /api/import/jobs/{id}` for progress
    (audit finding R2: a media-bearing export used to block every other
    request, including `/api/health`, for the whole run). Cooperatively
    cancellable via `cancel_import_job()`'s `cancel_requested` flag,
    checked every `_CHECKPOINT_ITEMS` items.
    """
    config = config or get_config()

    with session_scope(config) as conn:
        conn.execute(
            "UPDATE import_jobs SET status = ?, "
            "started_at = COALESCE(started_at, datetime('now')) WHERE id = ?",
            (JobStatus.RUNNING.value, job_id),
        )

    try:
        parsed = parse_export(zip_path, max_metadata_bytes=config.import_.max_metadata_bytes)
    except ExportFormatError as exc:
        return fail_job(job_id, str(exc), config)
    except (zipfile.BadZipFile, OSError) as exc:
        return fail_job(job_id, f"Couldn't read '{zip_path}': {exc}", config)

    total = len(parsed.saved_items) + len(parsed.own_posts)
    with session_scope(config) as conn:
        conn.execute("UPDATE import_jobs SET total_items = ? WHERE id = ?", (total, job_id))

    processed = 0
    failed = 0
    cancelled = False

    with session_scope(config) as conn:
        for saved in parsed.saved_items:
            if processed % _CHECKPOINT_ITEMS == 0 and _cancel_requested(job_id, config):
                cancelled = True
                break
            if not _import_one(conn, _import_saved_item, saved, job_id):
                failed += 1
            processed += 1
            if processed % _CHECKPOINT_ITEMS == 0:
                _record_progress(conn, job_id, processed, failed)
        _record_progress(conn, job_id, processed, failed)

    if not cancelled and parsed.own_posts:
        library_dir = config.resolved_library_dir
        library_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf, session_scope(config) as conn:
            for post in parsed.own_posts:
                if processed % _CHECKPOINT_ITEMS == 0 and _cancel_requested(job_id, config):
                    cancelled = True
                    break
                if not _import_one(
                    conn, _import_own_post, zf, post, job_id, library_dir, config
                ):
                    failed += 1
                processed += 1
                if processed % _CHECKPOINT_ITEMS == 0:
                    _record_progress(conn, job_id, processed, failed)
            _record_progress(conn, job_id, processed, failed)

    if cancelled:
        final_status = JobStatus.FAILED.value
        error_message = "cancelled by user"
    elif total > 0 and failed == total:
        final_status = JobStatus.FAILED.value
        error_message = "All items in this export failed to import — see server logs for details."
    else:
        final_status = JobStatus.DONE.value
        error_message = None

    with session_scope(config) as conn:
        conn.execute(
            "UPDATE import_jobs SET status = ?, error_message = ?, finished_at = datetime('now') "
            "WHERE id = ?",
            (final_status, error_message, job_id),
        )
        row = conn.execute("SELECT * FROM import_jobs WHERE id = ?", (job_id,)).fetchone()

    if final_status == JobStatus.DONE.value and processed > failed:
        # New rows landed — refresh the FTS keyword index (best-effort).
        try:
            with session_scope(config) as conn:
                fts.reindex(conn, None)
        except Exception:  # noqa: BLE001 - index upkeep must not fail the import
            logger.warning("import: FTS reindex failed", exc_info=True)

    return _row_to_job(row)


def fail_job(job_id: int, message: str, config: Config) -> ImportJob:
    with session_scope(config) as conn:
        conn.execute(
            "UPDATE import_jobs SET status = ?, error_message = ?, finished_at = datetime('now') "
            "WHERE id = ?",
            (JobStatus.FAILED.value, message, job_id),
        )
        row = conn.execute("SELECT * FROM import_jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row)


def import_zip(zip_path: Path, config: Config | None = None) -> ImportJob:
    """Convenience wrapper: create a job, run it synchronously, return the
    final job state. This is the one function both the CLI and the API
    upload route call, so their behavior never diverges."""
    config = config or get_config()
    job = create_import_job(zip_path, config)
    assert job.id is not None
    return run_import(job.id, zip_path, config)
