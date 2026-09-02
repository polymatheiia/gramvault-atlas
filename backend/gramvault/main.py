"""FastAPI application factory for GramVault.

Wires together all feature routers (see `gramvault.api`) and, in
production, serves the built frontend (`frontend/dist/`) as static files.

Run via `gramvault serve` (see `gramvault.cli`) or directly with uvicorn:
    uvicorn gramvault.main:app --reload
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from gramvault.api import (
    auth,
    jobs,
    routes_categorize,
    routes_chat,
    routes_enrich,
    routes_export,
    routes_import,
    routes_jobs,
    routes_library,
    routes_models,
)
from gramvault.config import Config, get_config
from gramvault.db.session import get_connection, init_db

logger = logging.getLogger(__name__)

# Vite's default dev server origin — allowed for local frontend development.
# TODO(A5): adjust/remove once the real frontend dev workflow is settled.
_DEV_FRONTEND_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

# Where `gramvault.cli`'s frontend build step is expected to output static
# assets. TODO(A5): confirm this matches your Vite `build.outDir`.
_FRONTEND_DIST_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app(config: Config | None = None) -> FastAPI:
    """Build and return a configured FastAPI app instance.

    Accepting an optional `config` (rather than always calling
    `get_config()` internally) makes it easy for tests to construct an
    app pointed at a temp config/db.
    """
    config = config or get_config()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Bring the schema up to date (fresh DB -> schema.sql; existing DB
        # -> pending migrations) and reclaim work abandoned by a previous
        # process: an item left 'running' or a job left 'pending'/'running'
        # by a crash/restart would otherwise never be picked up again.
        conn = get_connection(config)
        try:
            init_db(conn)
            items_reset = conn.execute(
                "UPDATE items SET enrichment_status = 'pending' "
                "WHERE enrichment_status = 'running'"
            ).rowcount
            jobs_reset = jobs.reclaim_orphans(conn)
            for table in ("import_jobs", "export_jobs"):
                conn.execute(
                    f"UPDATE {table} SET status = 'failed', "
                    "error_message = COALESCE(error_message, 'interrupted by restart'), "
                    "finished_at = datetime('now') "
                    "WHERE status IN ('pending', 'running')"
                )
            conn.commit()
            if items_reset or jobs_reset:
                logger.info(
                    "startup: reclaimed %d running item(s) and %d job(s) from a previous process",
                    items_reset,
                    jobs_reset,
                )
        finally:
            conn.close()
        yield

    app = FastAPI(
        title="GramVault",
        description="Private, local-first library for saved Instagram content.",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DEV_FRONTEND_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Bearer-token gate on /api/* + /media/* — a no-op unless
    # `config.auth.token` is set (see gramvault.api.auth). Added
    # unconditionally so setting the token via the API needs no restart.
    app.add_middleware(auth.BearerAuthMiddleware)

    app.state.config = config

    # --- feature routers ---
    app.include_router(routes_import.router)
    app.include_router(routes_library.router)
    app.include_router(routes_enrich.router)
    app.include_router(routes_categorize.router)
    app.include_router(routes_jobs.router)
    app.include_router(routes_models.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_export.router)

    @app.get("/api/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # --- serve imported media (photos/videos/keyframes) referenced by
    # MediaFile.file_path, which the frontend resolves via mediaUrl() as
    # "/media/<file_path>". Created on first request if the library hasn't
    # been imported into yet, so this mount never fails at startup.
    library_dir = config.resolved_library_dir
    library_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(library_dir)), name="media")

    # --- serve the built frontend in production, if present ---
    # Expects a Vite production build at frontend/dist/ with an index.html
    # entrypoint. If absent (frontend not built yet), the app runs API-only.
    #
    # A plain `StaticFiles(html=True)` mount can't serve client-side routes
    # like /chat or /items/42 on direct navigation/refresh — it 404s because
    # no such file exists on disk. So hashed build assets are served as
    # static files, and every other non-API/non-media path falls back to
    # index.html, letting react-router handle routing client-side.
    if _FRONTEND_DIST_DIR.is_dir():
        assets_dir = _FRONTEND_DIST_DIR / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="frontend-assets")

        index_path = _FRONTEND_DIST_DIR / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str) -> FileResponse:
            if full_path.startswith(("api/", "media/")):
                raise HTTPException(status_code=404, detail="Not Found")
            candidate = _FRONTEND_DIST_DIR / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index_path)

    return app


app = create_app()
