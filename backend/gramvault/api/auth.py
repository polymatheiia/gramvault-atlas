"""Optional bearer-token auth for the API.

When `config.auth.token` is set (via `secrets.yaml` / the Models settings
page), every `/api/*`, `/media/*`, `/docs`, `/redoc` and `/openapi.json`
request must carry `Authorization: Bearer <token>`. `/api/health` stays
open so a monitor can poll it. The SPA shell (`/`) and its built assets
(`/assets/*`) are intentionally left open since the browser can't attach
an Authorization header to a plain navigation — the frontend's AuthGate
prompts for the token before it calls the API. When no token is
configured the middleware is a no-op — which is only safe on a loopback
or trusted-tailnet bind.

The token is read from `get_config()` on each request, so setting or
clearing it through the API takes effect without a restart.
"""

from __future__ import annotations

import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from gramvault.config import get_config

_OPEN_PATHS = frozenset({"/api/health"})
_PROTECTED_PREFIXES = ("/api/", "/media/", "/docs", "/redoc", "/openapi.json")


def _is_protected(path: str) -> bool:
    if path in _OPEN_PATHS:
        return False
    return path.startswith(_PROTECTED_PREFIXES)


class BearerAuthMiddleware:
    """Pure-ASGI middleware (not BaseHTTPMiddleware, which buffers the body
    and breaks SSE streaming on `/api/chat/...`)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        expected = get_config().auth.token
        request = Request(scope, receive)
        if expected and _is_protected(request.url.path):
            header = request.headers.get("authorization", "")
            supplied = header[7:] if header.startswith("Bearer ") else ""
            if not supplied or not secrets.compare_digest(supplied, expected):
                response: Response = JSONResponse(
                    {"detail": "Missing or invalid API token"}, status_code=401
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)
