"""Anthropic provider (Messages API).

Chat + vision only — Anthropic has no embeddings endpoint, so `embed()`
raises. `httpx` only, no SDK.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from gramvault.ai.providers.base import (
    ChatResult,
    Message,
    ProviderCapabilityError,
    ProviderError,
    ProviderNotReadyError,
)
from gramvault.ai.providers.retry import send_with_retry

_DEFAULT_BASE_URL = "https://api.anthropic.com"
_API_VERSION = "2023-06-01"
# R5: the old hard-coded 4096 silently truncated large categorize/digest
# batches. This is only the *default* now — classifier/digest pass their
# own budget via complete(max_tokens=...).
_DEFAULT_MAX_TOKENS = 8192


class AnthropicProvider:
    name = "anthropic"
    kind = "api"

    def __init__(self, api_key: str | None, base_url: str | None = None) -> None:
        self.api_key = api_key
        self.base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")

    def _client(self, timeout: float | None = 120.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "x-api-key": self.api_key or "",
                "anthropic-version": _API_VERSION,
                "content-type": "application/json",
            },
            timeout=timeout,
        )

    def _require_key(self) -> None:
        if not self.api_key:
            raise ProviderNotReadyError(
                "No Anthropic API key configured. Add it on the Models settings page."
            )

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise ProviderNotReadyError(f"Anthropic API key rejected ({resp.status_code}).")
        if resp.status_code == 404:
            raise ProviderNotReadyError("Anthropic model not found (404).")
        resp.raise_for_status()

    # --- protocol ------------------------------------------------------

    async def ensure_ready(self, model: str) -> None:  # noqa: ARG002
        self._require_key()

    async def embed(self, model: str, text: str) -> list[float]:  # noqa: ARG002
        raise ProviderCapabilityError(
            "Anthropic has no embeddings API — point the `embedding` task at "
            "Ollama or an OpenAI-compatible provider."
        )

    def _split_system(self, messages: list[Message]) -> tuple[str | None, list[dict]]:
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        rest = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m["role"] in ("user", "assistant")
        ]
        return ("\n\n".join(system_parts) or None), rest

    async def caption_image(self, model: str, image_path: Path, prompt: str) -> str:
        self._require_key()
        raw = Path(image_path).read_bytes()
        mime = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": base64.b64encode(raw).decode("ascii"),
                },
            },
            {"type": "text", "text": prompt},
        ]
        return (await self._messages(model, None, [{"role": "user", "content": content}])).text

    async def chat(self, model: str, messages: list[Message]) -> str:
        self._require_key()
        system, rest = self._split_system(messages)
        return (await self._messages(model, system, rest)).text

    async def complete(
        self,
        model: str,
        messages: list[Message],
        *,
        max_tokens: int | None = None,
        json_mode: bool = False,  # noqa: ARG002 - Anthropic has no native JSON mode
    ) -> ChatResult:
        self._require_key()
        system, rest = self._split_system(messages)
        return await self._messages(model, system, rest, max_tokens=max_tokens)

    async def _messages(
        self,
        model: str,
        system: str | None,
        messages: list[dict],
        *,
        max_tokens: int | None = None,
    ) -> ChatResult:
        body: dict = {
            "model": model,
            "max_tokens": max_tokens or _DEFAULT_MAX_TOKENS,
            "messages": messages,
        }
        if system:
            body["system"] = system
        try:
            async with self._client() as client:
                resp = await send_with_retry(lambda: client.post("/v1/messages", json=body))
                self._raise_for_status(resp)
                data = resp.json()
        except httpx.ConnectError as exc:
            raise ProviderNotReadyError(f"Could not reach {self.base_url}: {exc}") from exc
        try:
            text = "".join(
                block["text"] for block in data["content"] if block.get("type") == "text"
            )
        except (KeyError, TypeError) as exc:  # pragma: no cover - defensive
            raise ProviderError(f"Unexpected Anthropic response shape: {data}") from exc
        usage = data.get("usage") or {}
        return ChatResult(
            text=text,
            truncated=data.get("stop_reason") == "max_tokens",
            tokens_in=usage.get("input_tokens"),
            tokens_out=usage.get("output_tokens"),
        )

    async def stream_chat(self, model: str, messages: list[Message]) -> AsyncIterator[str]:
        self._require_key()
        system, rest = self._split_system(messages)
        body: dict = {
            "model": model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "messages": rest,
            "stream": True,
        }
        if system:
            body["system"] = system
        client = self._client(timeout=None)
        try:
            async with client.stream("POST", "/v1/messages", json=body) as resp:
                self._raise_for_status(resp)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[len("data:") :].strip())
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "content_block_delta":
                        text = event.get("delta", {}).get("text")
                        if text:
                            yield text
        except httpx.ConnectError as exc:
            raise ProviderNotReadyError(f"Could not reach {self.base_url}: {exc}") from exc
        finally:
            await client.aclose()
