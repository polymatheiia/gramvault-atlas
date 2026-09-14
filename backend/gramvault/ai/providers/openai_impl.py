"""OpenAI-compatible provider.

Covers OpenAI itself and every service that speaks the same wire format:
OpenRouter, Groq, Together, DeepInfra, and local servers (llama.cpp
`--server`, LM Studio, vLLM, Ollama's own `/v1`). Pick the flavour with
`base_url` in the provider config.

Only `httpx` (already a dependency) — no vendor SDK.
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
    ProviderError,
    ProviderNotReadyError,
)
from gramvault.ai.providers.retry import send_with_retry

_DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAICompatProvider:
    name = "openai"
    kind = "api"

    def __init__(self, api_key: str | None, base_url: str | None = None) -> None:
        self.api_key = api_key
        self.base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")

    # --- helpers ---------------------------------------------------------

    def _client(self, timeout: float | None = 120.0) -> httpx.AsyncClient:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=timeout)

    def _require_key(self) -> None:
        # A localhost base_url (llama.cpp / LM Studio / vLLM) usually needs
        # no key; a remote one does.
        if not self.api_key and "localhost" not in self.base_url and "127.0.0.1" not in self.base_url:
            raise ProviderNotReadyError(
                f"No API key configured for the OpenAI-compatible provider at {self.base_url}. "
                "Add it on the Models settings page."
            )

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise ProviderNotReadyError(f"API key rejected ({resp.status_code}).")
        if resp.status_code == 404:
            raise ProviderNotReadyError(
                f"Model not found at this endpoint ({resp.status_code})."
            )
        resp.raise_for_status()

    # --- protocol ------------------------------------------------------

    async def ensure_ready(self, model: str) -> None:  # noqa: ARG002 - model unused, key is the gate
        self._require_key()

    async def embed(self, model: str, text: str) -> list[float]:
        self._require_key()
        try:
            async with self._client(timeout=60.0) as client:
                resp = await send_with_retry(
                    lambda: client.post("/embeddings", json={"model": model, "input": text})
                )
                self._raise_for_status(resp)
                data = resp.json()
        except httpx.ConnectError as exc:
            raise ProviderNotReadyError(f"Could not reach {self.base_url}: {exc}") from exc
        return data["data"][0]["embedding"]

    async def caption_image(self, model: str, image_path: Path, prompt: str) -> str:
        self._require_key()
        raw = Path(image_path).read_bytes()
        mime = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
        data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]
        return (await self._chat_request(model, messages)).text

    async def chat(self, model: str, messages: list[Message]) -> str:
        self._require_key()
        return (await self._chat_request(model, messages)).text

    async def complete(
        self,
        model: str,
        messages: list[Message],
        *,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> ChatResult:
        self._require_key()
        return await self._chat_request(model, messages, max_tokens=max_tokens, json_mode=json_mode)

    async def _chat_request(
        self,
        model: str,
        messages: list,
        *,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> ChatResult:
        payload: dict = {"model": model, "messages": messages}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            async with self._client() as client:
                resp = await send_with_retry(
                    lambda: client.post("/chat/completions", json=payload)
                )
                if json_mode and resp.status_code == 400:
                    # Best-effort: many OpenAI-compatible backends
                    # (llama.cpp, LM Studio, vLLM, older Ollama /v1) 400 on
                    # an unrecognised response_format. Retry once without
                    # it rather than hard-failing the request.
                    payload.pop("response_format", None)
                    resp = await send_with_retry(
                        lambda: client.post("/chat/completions", json=payload)
                    )
                self._raise_for_status(resp)
                data = resp.json()
        except httpx.ConnectError as exc:
            raise ProviderNotReadyError(f"Could not reach {self.base_url}: {exc}") from exc
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:  # pragma: no cover - defensive
            raise ProviderError(f"Unexpected chat response shape: {data}") from exc
        usage = data.get("usage") or {}
        return ChatResult(
            text=text,
            truncated=choice.get("finish_reason") == "length",
            tokens_in=usage.get("prompt_tokens"),
            tokens_out=usage.get("completion_tokens"),
        )

    async def stream_chat(self, model: str, messages: list[Message]) -> AsyncIterator[str]:
        self._require_key()
        client = self._client(timeout=None)
        try:
            async with client.stream(
                "POST",
                "/chat/completions",
                json={"model": model, "messages": messages, "stream": True},
            ) as resp:
                self._raise_for_status(resp)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:") :].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        delta = json.loads(payload)["choices"][0]["delta"].get("content")
                    except (KeyError, IndexError, json.JSONDecodeError):
                        continue
                    if delta:
                        yield delta
        except httpx.ConnectError as exc:
            raise ProviderNotReadyError(f"Could not reach {self.base_url}: {exc}") from exc
        finally:
            await client.aclose()
