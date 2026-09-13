"""Cross-site request rejection for the API (audit finding S2).

The documented default configuration binds `127.0.0.1` with no auth
token and treats loopback as a security boundary — it isn't, for a
browser-driven app. A page on any other origin can fire "simple"
POST/PUT/DELETE requests at `127.0.0.1:8000` (no CORS preflight, since a
`Blob`/`FormData` body without an explicit `Content-Type` stays
CORS-"simple", and FastAPI's body parser falls back to `request.json()`
when the header is absent). The browser can't read the response, but the
side effect — dropping the vector store, spending API budget, writing
files into the vault, planting library items, ... — happens anyway.

This middleware structurally refuses that class of request on every
non-safe `/api/*` call, and also on `/media/*` GETs (a cross-site
`<img src>`/`<video src>` load still sends `Sec-Fetch-Site: cross-site`,
and bearer auth can't gate `/media` at all — see `gramvault.api.auth`):
  1. `Sec-Fetch-Site: cross-site` (sent by every modern browser) -> reject.
  2. An `Origin` header present but not same-origin/allowlisted -> reject.
  3. No `X-GramVault-Client` header on a non-GET/HEAD/OPTIONS request ->
     reject. This header forces a real CORS preflight (custom headers
     aren't "simple"), which the CORS policy then denies for any origin
     not on the allowlist. The SPA sets it on every request in
     `api/client.ts`; a script/cron caller adds it manually.

Same-origin is compared by host (`Origin`'s host:port vs. the request's
`Host` header) rather than scheme+host: behind a TLS-terminating reverse
proxy (Caddy, nginx) the browser's `Origin` is `https://...` while
`request.url.scheme` is `http` unless uvicorn is told to trust forwarded
headers — comparing scheme too would reject every request in that setup.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

CLIENT_HEADER = "x-gramvault-client"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_GUARDED_PREFIXES = ("/api/", "/media/")


class CrossSiteGuard:
    """Pure-ASGI middleware — cheap enough to run in front of every
    request, and doesn't need to buffer the body (unlike
    `BaseHTTPMiddleware`), which matters for `/api/import/upload` and the
    chat SSE stream."""

    def __init__(self, app: ASGIApp, *, allowed_origins: frozenset[str]) -> None:
        self.app = app
        self.allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(_GUARDED_PREFIXES):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        if self._is_cross_site(request):
            response = JSONResponse({"detail": "cross-site request rejected"}, status_code=403)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _is_cross_site(self, request: Request) -> bool:
        if request.headers.get("sec-fetch-site") == "cross-site":
            return True

        origin = request.headers.get("origin")
        if origin is not None:
            same_origin = urlsplit(origin).netloc == request.headers.get("host", "")
            if not same_origin and origin not in self.allowed_origins:
                return True

        return (
            request.method not in _SAFE_METHODS
            and request.headers.get(CLIENT_HEADER) != "1"
        )
