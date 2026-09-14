"""Retry/backoff for hosted-provider HTTP calls (R6).

Applies only to the non-streaming request/response calls (`/v1/messages`,
`/chat/completions`, `/embeddings`) — never to an open `client.stream(...)`,
since retrying after tokens have already been yielded would duplicate
output. A `httpx.ConnectError` (server unreachable) is not retried here:
that's mapped straight to `ProviderNotReadyError` by the caller, and three
backoffs before saying "can't reach the server" would only slow down a
failure the user needs to act on, not the request succeeding.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

import httpx

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BASE_DELAY = 0.5
_MAX_DELAY = 20.0


def _backoff_delay(attempt: int) -> float:
    return min(_BASE_DELAY * (2**attempt), _MAX_DELAY) + random.uniform(0, 0.25)


def _retry_after_delay(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if value is None:
        return None
    try:
        return min(float(value), _MAX_DELAY)
    except ValueError:
        return None


async def send_with_retry(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    max_retries: int = _MAX_RETRIES,
) -> httpx.Response:
    """Call `send()` (one HTTP attempt), retrying on 429/5xx and on a
    request timeout with exponential backoff (honouring `Retry-After` on
    429/503 when present). Returns the last response once retries are
    exhausted or a non-retryable status comes back."""
    attempt = 0
    while True:
        try:
            resp = await send()
        except httpx.TimeoutException:
            if attempt >= max_retries:
                raise
            await asyncio.sleep(_backoff_delay(attempt))
            attempt += 1
            continue
        if resp.status_code in _RETRYABLE_STATUS and attempt < max_retries:
            await asyncio.sleep(_retry_after_delay(resp) or _backoff_delay(attempt))
            attempt += 1
            continue
        return resp
