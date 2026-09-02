"""GramVault CLI entry point.

Installed as the `gramvault` console script (see `pyproject.toml`
`[project.scripts]`). Subcommands are wired up here; feature agents fill
in the bodies that need real logic (import).
"""

from __future__ import annotations

from pathlib import Path

import typer

from gramvault.config import get_config
from gramvault.db.session import get_connection, init_db
from gramvault.ingestion import import_zip, link_local_media
from gramvault.models.schemas import JobStatus

app = typer.Typer(
    name="gramvault",
    help="Private, local-first library for saved Instagram content.",
    add_completion=False,
)


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Override config.yaml server.host"),
    port: int | None = typer.Option(None, help="Override config.yaml server.port"),
    reload: bool = typer.Option(False, help="Enable uvicorn auto-reload (dev only)"),
) -> None:
    """Run the FastAPI server."""
    import uvicorn

    config = get_config()
    uvicorn.run(
        "gramvault.main:app",
        host=host or config.server.host,
        port=port or config.server.port,
        reload=reload,
    )


@app.command(name="import")
def import_export(
    zip_path: Path = typer.Argument(..., help="Path to an Instagram data export ZIP file"),
) -> None:
    """Import an Instagram data export from the command line.

    Calls the same `gramvault.ingestion.import_zip` used by
    `POST /api/import/upload` (backend/gramvault/api/routes_import.py), so
    there's a single implementation shared between the API and the CLI.
    """
    config = get_config()
    if not zip_path.exists():
        typer.echo(f"No such file: {zip_path}")
        raise typer.Exit(code=1)

    job = import_zip(zip_path, config)

    if job.status == JobStatus.FAILED:
        typer.echo(f"Import failed: {job.error_message}")
        raise typer.Exit(code=1)

    typer.echo(
        f"Import job #{job.id} {job.status.value}: "
        f"{job.processed_items}/{job.total_items} items processed "
        f"({job.failed_items} failed)."
    )


@app.command(name="ocr")
def ocr_silent_videos(
    all_silent: bool = typer.Option(
        False, "--all-silent", help="Every silent video, not only those with a thin caption"
    ),
    limit: int | None = typer.Option(None, help="Stop after this many videos (for testing)"),
) -> None:
    """Read on-screen text from silent videos into `media_files.ocr_text`.

    A reel with no narration and a hashtag-only caption has no text about
    it anywhere — but it usually has an overlay carrying the whole point of
    the post. By default this targets exactly those items; `--all-silent`
    widens it to every video without a transcript.

    Only Latin-script results are kept. Cyrillic transcription from the
    local models is unreliable enough to be worse than nothing (see
    `gramvault.ai.ocr`), so those are dropped — but `ocr_attempted_at` is
    still stamped so the row isn't retried until a stronger vision model is
    configured. This is the same OCR pass the Enrich page runs.
    """
    import asyncio
    import re
    import tempfile
    from pathlib import Path as _Path

    from gramvault.ai import ocr
    from gramvault.ai.providers import get_provider
    from gramvault.db.session import session_scope

    config = get_config()
    vision_provider, vision_model = get_provider("vision", config)

    def substance(caption: str | None) -> int:
        text = re.sub(r"https?://\S+", "", caption or "")
        text = re.sub(r"[#@]\w+", "", text)
        return len(re.sub(r"[^\w\s]", "", text).strip())

    with session_scope(config) as conn:
        rows = conn.execute(
            """
            SELECT media_files.id, media_files.file_path, items.id AS item_id, items.caption
            FROM media_files JOIN items ON items.id = media_files.item_id
            WHERE media_files.media_type = 'video'
              AND (media_files.transcript = '' OR media_files.transcript IS NULL)
              AND media_files.ocr_attempted_at IS NULL
            ORDER BY media_files.id
            """
        ).fetchall()
    queue = [dict(r) for r in rows]
    if not all_silent:
        queue = [r for r in queue if substance(r["caption"]) < 40]
    queue = queue[: limit or None]

    if not queue:
        typer.echo("Nothing to do — every silent video already has a vision_caption.")
        return

    typer.echo(f"Reading on-screen text from {len(queue)} silent video(s). Resumable.")
    library_dir = config.resolved_library_dir
    model_id = f"{vision_provider.name}:{vision_model}"
    kept = dropped = unreadable = 0

    async def run() -> None:
        nonlocal kept, dropped, unreadable
        await vision_provider.ensure_ready(vision_model)
        with tempfile.TemporaryDirectory() as tmp:
            for index, row in enumerate(queue, start=1):
                video = _Path(row["file_path"])
                if not video.is_absolute():
                    video = library_dir / video
                frame = ocr.frame_for_ocr(video, _Path(tmp) / "frame.jpg")
                if frame is None:
                    unreadable += 1
                    typer.echo(f"[{index}/{len(queue)}] no frame from {video.name}")
                    continue
                try:
                    raw = await vision_provider.caption_image(
                        vision_model, frame, ocr.OCR_PROMPT
                    )
                except Exception as exc:  # noqa: BLE001 — one bad frame mustn't end the run
                    unreadable += 1
                    typer.echo(f"[{index}/{len(queue)}] failed: {exc}")
                    continue

                text = ocr.clean_output(raw)
                # Stamp `ocr_attempted_at` even for a rejected read (text is
                # None) so the row isn't retried on every subsequent run.
                with session_scope(config) as write_conn:
                    write_conn.execute(
                        "UPDATE media_files SET ocr_text = ?, "
                        "ocr_attempted_at = datetime('now'), ocr_model = ? WHERE id = ?",
                        (text, model_id, row["id"]),
                    )
                if text:
                    kept += 1
                    typer.echo(f"[{index}/{len(queue)}] {text[:70]}")
                else:
                    dropped += 1
                    typer.echo(f"[{index}/{len(queue)}] discarded (no text / Cyrillic)")

    asyncio.run(run())
    typer.echo(
        f"\nDone. {kept} with usable text, {dropped} discarded, {unreadable} unreadable.\n"
        "Re-run `gramvault enrich --skip-vision` to embed the new text."
    )


@app.command(name="enrich")
def enrich_items(
    skip_vision: bool = typer.Option(
        False, "--skip-vision", help="Skip image captioning; still transcribe and embed"
    ),
    retry_failed: bool = typer.Option(
        False, "--retry-failed", help="Re-queue items previously marked failed"
    ),
    limit: int | None = typer.Option(None, help="Stop after this many items (for testing)"),
) -> None:
    """Run AI enrichment over pending items and embed them for search.

    `--skip-vision` is the useful mode when the local vision model isn't
    good enough for your library: transcripts, captions and tags still get
    built into the content document and embedded, so chat and search work,
    without writing image captions you don't trust. Running again later
    without the flag fills the captions in.
    """
    import asyncio

    from gramvault.ai import pipeline
    from gramvault.db.session import session_scope
    from gramvault.models.schemas import EnrichmentStatus

    config = get_config()
    with session_scope(config) as conn:
        if retry_failed:
            requeued = conn.execute(
                "UPDATE items SET enrichment_status = ? WHERE enrichment_status = ?",
                (EnrichmentStatus.PENDING.value, EnrichmentStatus.FAILED.value),
            ).rowcount
            if requeued:
                typer.echo(f"Re-queued {requeued} previously failed item(s).")
        item_ids = pipeline.resolve_target_item_ids(conn, None)

    item_ids = item_ids[: limit or None]
    if not item_ids:
        typer.echo("Nothing pending. Use --retry-failed to re-queue failed items.")
        return

    typer.echo(
        f"Enriching {len(item_ids)} item(s)"
        + (" (vision skipped)" if skip_vision else "")
        + ". Needs Ollama running for embeddings. Safe to interrupt and resume."
    )
    asyncio.run(pipeline.process_items(item_ids, config, skip_vision=skip_vision))

    with session_scope(config) as conn:
        rows = conn.execute(
            "SELECT enrichment_status, COUNT(*) AS n FROM items GROUP BY enrichment_status"
        ).fetchall()
    typer.echo("\nDone. " + ", ".join(f"{r['n']} {r['enrichment_status']}" for r in rows))


@app.command(name="transcribe")
def transcribe_media(
    limit: int | None = typer.Option(None, help="Stop after this many files (for testing)"),
) -> None:
    """Transcribe every video that doesn't have a transcript yet.

    Split out from `enrich` so the audio pass — which is accurate and
    needs no network — can run on its own, without waiting on the vision
    model. Resumable: the queue is just `transcript IS NULL`, so a killed
    run picks up where it stopped.
    """
    from gramvault.ai import transcription
    from gramvault.db.session import session_scope

    config = get_config()
    with session_scope(config) as conn:
        rows = conn.execute(
            """
            SELECT media_files.id, media_files.file_path, items.caption
            FROM media_files JOIN items ON items.id = media_files.item_id
            WHERE media_files.media_type = 'video' AND media_files.transcript IS NULL
            ORDER BY media_files.id
            """
        ).fetchall()
    queue = [dict(row) for row in rows][: limit or None]

    if not queue:
        typer.echo("Nothing to transcribe — every video already has a transcript.")
        return

    typer.echo(f"Transcribing {len(queue)} video(s) with Whisper "
               f"'{transcription._whisper_model_size()}'. Safe to interrupt and resume.")

    library_dir = config.resolved_library_dir
    with_speech = silent = failed = 0
    for index, row in enumerate(queue, start=1):
        path = Path(row["file_path"])
        if not path.is_absolute():
            path = library_dir / path
        try:
            result = transcription.transcribe(path, transcription.language_hint(row["caption"]))
        except Exception as exc:  # noqa: BLE001 — one bad file must not end the run
            failed += 1
            typer.echo(f"[{index}/{len(queue)}] failed {path.name}: {exc}")
            continue

        # Store "" rather than leaving NULL for a music-only reel, so it
        # counts as done and the next run doesn't retry it forever.
        with session_scope(config) as conn:
            conn.execute(
                "UPDATE media_files SET transcript = ? WHERE id = ?", (result.text, row["id"])
            )
        if result.text:
            with_speech += 1
            typer.echo(f"[{index}/{len(queue)}] {result.language or '??'}: {result.text[:70]}")
        else:
            silent += 1
            typer.echo(f"[{index}/{len(queue)}] no speech")

    typer.echo(f"\nDone. {with_speech} transcribed, {silent} music-only/silent, {failed} failed.")


@app.command(name="link-media")
def link_media(
    source_dir: Path = typer.Argument(
        ..., help="Directory of downloaded media whose filenames contain the shortcode"
    ),
    copy: bool = typer.Option(
        False, "--copy", help="Copy files into the library instead of hardlinking them"
    ),
) -> None:
    """Attach separately downloaded media to imported saved items.

    Instagram's export contains no media for other people's posts, so
    saved reels import as link-only metadata. Point this at a directory
    downloaded with instaloader (or anything else whose filenames carry
    the post shortcode) and the files are matched to those items by
    shortcode and brought into the library.
    """
    config = get_config()
    try:
        report = link_local_media(source_dir, config, copy=copy)
    except NotADirectoryError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(report.summary())
    if report.unmatched_examples:
        typer.echo(
            "Files matched no imported item, e.g.: "
            + ", ".join(report.unmatched_examples)
            + "\nImport the matching export first, or check that filenames contain "
            "the post shortcode."
        )


@app.command(name="init-db")
def init_db_command() -> None:
    """Create the SQLite database and tables if they don't already exist."""
    config = get_config()
    conn = get_connection(config)
    try:
        init_db(conn)
    finally:
        conn.close()
    typer.echo(f"Database ready at {config.resolved_db_path}")


@app.command(name="import-categories")
def import_categories_command(
    csv_path: Path = typer.Argument(
        ..., help="CSV with at least `id` (gramvault item id) and `category` (name) columns"
    ),
) -> None:
    """Backfill item categories from a CSV of hand-labelled assignments.

    Each matched item is set to `category_source='manual'` /
    `category_confidence=1.0`, so the automatic classifier will never
    overwrite it. Category names in the CSV must already exist in the
    `categories` table (they're seeded on first run).
    """
    import csv as _csv

    from gramvault.db.session import session_scope

    config = get_config()
    if not csv_path.exists():
        typer.echo(f"No such file: {csv_path}")
        raise typer.Exit(code=1)

    with session_scope(config) as conn:
        name_to_id = {
            row["name"]: row["id"] for row in conn.execute("SELECT id, name FROM categories")
        }
        applied = missing_item = 0
        unknown: dict[str, int] = {}
        with csv_path.open(newline="", encoding="utf-8") as handle:
            reader = _csv.DictReader(handle)
            if reader.fieldnames is None or "id" not in reader.fieldnames or "category" not in reader.fieldnames:
                typer.echo("CSV must have `id` and `category` columns.")
                raise typer.Exit(code=1)
            for row in reader:
                raw_id, name = (row.get("id") or "").strip(), (row.get("category") or "").strip()
                if not raw_id.isdigit():
                    continue
                category_id = name_to_id.get(name)
                if category_id is None:
                    unknown[name] = unknown.get(name, 0) + 1
                    continue
                changed = conn.execute(
                    "UPDATE items SET category_id = ?, category_source = 'manual', "
                    "category_confidence = 1.0, category_updated_at = datetime('now') "
                    "WHERE id = ?",
                    (category_id, int(raw_id)),
                ).rowcount
                if changed:
                    applied += 1
                else:
                    missing_item += 1

    typer.echo(f"Categorised {applied} item(s).")
    if missing_item:
        typer.echo(f"  {missing_item} row(s) skipped — no such item in the library.")
    for name, n in sorted(unknown.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  {n} row(s) skipped — unknown category '{name}'.")


@app.command(name="categorize")
def categorize_command(
    method: str = typer.Option(
        "keyword_then_llm",
        help="keyword | llm | keyword_then_llm (LLM needs the `categorize` provider ready)",
    ),
    scope: str = typer.Option(
        "uncategorized", help="uncategorized | needs_review | all"
    ),
) -> None:
    """Sort items into categories (the reels-workflow D2/D3 passes).

    `keyword` is free and offline; `llm` re-labels every item through the
    configured `categorize` model; `keyword_then_llm` (the default) runs
    the keyword vote first and only sends its low-confidence guesses to the
    model. Items assigned a category by hand are never touched. Safe to
    interrupt and re-run.
    """
    import asyncio

    from gramvault.ai import classifier
    from gramvault.db.session import session_scope

    if method not in ("keyword", "llm", "keyword_then_llm"):
        typer.echo(f"Unknown method: {method}")
        raise typer.Exit(code=1)

    config = get_config()
    with session_scope(config) as conn:
        if scope == "uncategorized":
            sql = "SELECT id FROM items WHERE category_id IS NULL"
        elif scope == "needs_review":
            sql = (
                "SELECT id FROM items WHERE category_id IS NOT NULL "
                "AND COALESCE(category_source, '') != 'manual' "
                "AND COALESCE(category_confidence, 0) < 0.6"
            )
        elif scope == "all":
            sql = "SELECT id FROM items WHERE COALESCE(category_source, '') != 'manual'"
        else:
            typer.echo(f"Unknown scope: {scope}")
            raise typer.Exit(code=1)
        item_ids = [row["id"] for row in conn.execute(sql)]

    if not item_ids:
        typer.echo(f"No items match scope '{scope}'.")
        return

    typer.echo(f"Categorising {len(item_ids)} item(s) with '{method}'. Safe to interrupt.")
    summary = asyncio.run(classifier.categorize_items(item_ids, method, config))

    typer.echo(
        f"\nDone. {summary['processed']} labelled "
        f"({summary['keyword']} keyword, {summary['llm']} LLM), "
        f"{summary['needs_review']} need review, {summary['skipped_manual']} left (manual)."
    )
    for name, count in sorted(summary["by_category"].items(), key=lambda kv: -kv[1]):
        typer.echo(f"  {name:20s} {count}")


@app.command(name="digest")
def digest_command(
    template: str = typer.Argument(..., help="Template name (see `ai/digest_templates/`)"),
    category: str | None = typer.Option(None, help="Digest every item in this category"),
    query: str | None = typer.Option(None, help="Digest the semantic-search hits for this query"),
    name: str | None = typer.Option(None, help="Name for the digest (defaults to template + count)"),
    out: Path | None = typer.Option(None, help="Write the Markdown here (default: stdout)"),
) -> None:
    """Generate a Markdown recommendation/notes doc (the reels-workflow §D
    map/reduce step). Runs synchronously and needs the `digest` provider
    ready; the result is also saved to the `digests` table.
    """
    import asyncio
    import json as _json

    from gramvault.ai import digest as digest_engine
    from gramvault.db.session import session_scope

    config = get_config()
    selection = digest_engine.Selection(category=category, query=query)

    async def run() -> str:
        with session_scope(config) as conn:
            item_ids = await digest_engine.select_items(conn, selection, config)
        if not item_ids:
            raise RuntimeError("the selection matched no items")
        tmpl = digest_engine.get_template(template, config)
        digest_name = (name or "").strip() or f"{tmpl.name} — {len(item_ids)} items"
        with session_scope(config) as conn:
            cursor = conn.execute(
                "INSERT INTO digests (name, template, template_version, status, "
                "selection_json, item_ids_json) VALUES (?, ?, ?, 'pending', ?, ?)",
                (
                    digest_name,
                    tmpl.name,
                    tmpl.version,
                    _json.dumps(selection.to_json()),
                    _json.dumps(item_ids),
                ),
            )
            digest_id = cursor.lastrowid
        typer.echo(f"Digesting {len(item_ids)} item(s) with '{tmpl.name}'…", err=True)
        summary = await digest_engine.run_digest(digest_id, config)
        typer.echo(
            f"Done: digest #{digest_id}, {summary['entries']} entries, "
            f"~{summary['tokens_in'] + summary['tokens_out']} tokens.",
            err=True,
        )
        with session_scope(config) as conn:
            row = conn.execute(
                "SELECT markdown FROM digests WHERE id = ?", (digest_id,)
            ).fetchone()
        return row["markdown"] or ""

    try:
        markdown = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - surface as a clean CLI error
        typer.echo(f"Error: {exc}")
        raise typer.Exit(code=1) from exc

    if out:
        out.write_text(markdown, encoding="utf-8")
        typer.echo(f"Wrote {out}", err=True)
    else:
        typer.echo(markdown)


@app.command(name="migrate")
def migrate_command(
    status_only: bool = typer.Option(
        False, "--status", help="Only print the current/target schema version, don't apply anything"
    ),
) -> None:
    """Apply any pending schema migrations to the configured database."""
    from gramvault.db.session import latest_migration_version, migrate, schema_version

    config = get_config()
    conn = get_connection(config)
    try:
        current = schema_version(conn)
        target = latest_migration_version()
        if status_only:
            typer.echo(f"schema version: {current} (latest available: {target})")
            return
        if current >= target:
            typer.echo(f"Already up to date (schema version {current}).")
            return
        typer.echo(f"Migrating {config.resolved_db_path}: {current} -> {target}")
        final = migrate(conn)
        typer.echo(f"Done. Schema version is now {final}.")
    finally:
        conn.close()


def main() -> None:
    """Console-script entry point (see pyproject.toml [project.scripts])."""
    app()


if __name__ == "__main__":
    main()
