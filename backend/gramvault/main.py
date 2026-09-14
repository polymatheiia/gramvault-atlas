"""FastAPI application factory for GramVault.

Wires together all feature routers (see `gramvault.api`) and, in
production, serves the built frontend (`frontend/dist/`) as static files.

Run via `gramvault serve` (see `gramvault.cli`) or directly with uvicorn:
    uvicorn gramvault.main:app --reload
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from gramvault import __version__
from gramvault.api import (
    auth,
    jobs,
    routes_categorize,
    routes_chat,
    routes_digests,
    routes_enrich,
    routes_export,
    routes_import,
    routes_jobs,
    routes_library,
    routes_models,
    routes_pull,
    routes_system,
)
from gramvault.api.csrf import CrossSiteGuard
from gramvault.api.headers import SafeMedia, SecurityHeadersMiddleware
from gramvault.api.routes_system import HealthResponse, build_health_response
from gramvault.config import Config, get_config
from gramvault.db.session import get_connection, init_db

logger = logging.getLogger(__name__)

# Set GRAMVAULT_DEV=1 to run against the Vite dev server (`npm run dev`)
# instead of a production build: relaxes CORS to the dev origin below and
# re-enables /docs, /redoc, /openapi.json (audit findings S11, S12).
_DEV_MODE = os.environ.get("GRAMVAULT_DEV") == "1"

# Vite's default dev server origin — allowed for local frontend development
# only, see `_DEV_MODE` above.
_DEV_FRONTEND_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

# Where `gramvault.cli`'s frontend build step is expected to output static
# assets — matches Vite's default `build.outDir` of `dist/` (see
# frontend/vite.config.ts).
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
        title="GramVault Atlas",
        description="Private, local-first pipeline for saved Instagram content.",
        version=__version__,
        lifespan=lifespan,
        # Swagger UI / ReDoc / the raw schema are pointless in prod and
        # unauthenticated (they live outside /api/*) — dev-only (S11).
        docs_url="/docs" if _DEV_MODE else None,
        redoc_url="/redoc" if _DEV_MODE else None,
        openapi_url="/openapi.json" if _DEV_MODE else None,
    )

    # Host header allowlist — closes DNS rebinding (a page on an attacker
    # domain re-pointed at 127.0.0.1 becomes "same-origin" from the
    # browser's point of view once the Host check is gone). Outermost
    # middleware: a bad Host is rejected before anything else runs.
    _hosts = {"localhost", "127.0.0.1", "[::1]", config.server.host, *config.server.allowed_hosts}
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=[h for h in _hosts if h and h != "0.0.0.0"]
    )
    app.add_middleware(SecurityHeadersMiddleware)
    # Structural CSRF defense: rejects cross-site /api/* requests before
    # they reach a route, independent of whether a token is configured
    # (audit finding S2 — auth is off by default, and loopback is not a
    # security boundary for a browser-driven app).
    app.add_middleware(
        CrossSiteGuard,
        allowed_origins=frozenset(_DEV_FRONTEND_ORIGINS) if _DEV_MODE else frozenset(),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DEV_FRONTEND_ORIGINS if _DEV_MODE else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Bearer-token gate on /api/* + /media/* + /docs/* — a no-op unless
    # `config.auth.token` is set (see gramvault.api.auth). Added
    # unconditionally so setting the token via the API needs no restart.
    app.add_middleware(auth.BearerAuthMiddleware)

    app.state.config = config

    # --- feature routers ---
    app.include_router(routes_import.router)
    app.include_router(routes_pull.router)
    app.include_router(routes_library.router)
    app.include_router(routes_enrich.router)
    app.include_router(routes_categorize.router)
    app.include_router(routes_digests.router)
    app.include_router(routes_jobs.router)
    app.include_router(routes_models.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_export.router)
    app.include_router(routes_system.router)

    @app.get("/api/health", tags=["meta"], response_model=HealthResponse)
    async def health() -> HealthResponse:
        return await build_health_response(config)

    # --- serve imported media (photos/videos/keyframes) referenced by
    # MediaFile.file_path, which the frontend resolves via mediaUrl() as
    # "/media/<file_path>". Created on first request if the library hasn't
    # been imported into yet, so this mount never fails at startup.
    library_dir = config.resolved_library_dir
    library_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media", SafeMedia(directory=str(library_dir)), name="media")

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
        dist = _FRONTEND_DIST_DIR.resolve()
        assets_dir = dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="frontend-assets")

        index_path = dist / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str) -> FileResponse:
            if full_path.startswith(("api/", "media/")):
                raise HTTPException(status_code=404, detail="Not Found")
            try:
                candidate = (dist / full_path).resolve()
            except (OSError, RuntimeError):
                return FileResponse(index_path)
            # Only serve files that really resolve inside frontend/dist
            # (blocks `..`/absolute segments and symlink escapes) — anything
            # else falls back to the SPA shell, same as an unknown route.
            if full_path and candidate.is_relative_to(dist) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index_path)

    return app


app = create_app()
