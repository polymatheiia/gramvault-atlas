"""Security headers (audit finding S11) and defensive `/media` serving
(audit finding S3).

`/media` serves whatever `organizer.py` accepted into the library. That's
now gated to a real image/video allowlist at import time (see
`gramvault.ingestion.organizer`), but this is the belt for that
suspenders: even if something slipped through, `SafeMedia` makes sure it
can't execute as a document on the app's own origin.
"""

from __future__ import annotations

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_MEDIA_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    # A sandboxed document has no origin and can't run script, so even a
    # served HTML/SVG file is inert.
    "Content-Security-Policy": "sandbox; default-src 'none'",
}

_APP_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
        "frame-ancestors 'none'"
    ),
}


class SafeMedia(StaticFiles):
    """`StaticFiles` for `/media` that never lets a served file execute as
    a document on the app's origin, whatever its content-type."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.update(_MEDIA_HEADERS)
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith(("image/", "video/")):
            response.headers["Content-Disposition"] = "attachment"
        return response


class SecurityHeadersMiddleware:
    """Pure-ASGI middleware adding baseline security headers to every
    response (clickjacking, MIME-sniffing, referrer leakage)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # SafeMedia already sets its own (stricter) CSP/nosniff for
        # /media/* — don't layer a second, conflicting CSP on top of it.
        if scope["type"] != "http" or scope["path"].startswith("/media/"):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                for key, value in _APP_SECURITY_HEADERS.items():
                    headers.append((key.lower().encode(), value.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
