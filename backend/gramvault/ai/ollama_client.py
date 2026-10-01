"""Thin async HTTP wrapper around a local Ollama server.

`embed()`, `chat_completion()`, and `stream_chat()` are the stable contract
`gramvault.chat.*` depends on for retrieval and RAG responses; changing
their signatures means updating those call sites too.

Everything here talks to `config.ollama.host` (default
http://localhost:11434) using Ollama's native HTTP API:
    GET  /api/tags       -> list locally-pulled models (health + pull check)
    POST /api/embeddings -> {model, prompt} -> {embedding: [...]}
    POST /api/chat       -> {model, messages, stream} -> chat completion
    POST /api/generate   -> {model, prompt, images} -> vision/text generation
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from gramvault.ai.errors import ProviderError, ProviderNotReadyError
from gramvault.config import Config, get_config


class OllamaError(ProviderNotReadyError):
    """Base class for all Ollama-related failures. Subclasses
    `ProviderNotReadyError` so provider-agnostic call sites can catch the
    one exception, while existing `except OllamaNotRunningError` code
    keeps working."""


class OllamaNotRunningError(OllamaError):
    """Raised when the Ollama server can't be reached at all."""

    def __init__(self, host: str, cause: Exception | None = None) -> None:
        super().__init__(
            f"Could not reach Ollama at {host}. Is `ollama serve` running? "
            f"(underlying error: {cause})"
        )
        self.host = host
        self.cause = cause


class ModelNotPulledError(OllamaError):
    """Raised when a required model isn't in `ollama list` yet."""

    def __init__(self, model: str) -> None:
        super().__init__(
            f"Model '{model}' is not pulled. Run `ollama pull {model}` and try again."
        )
        self.model = model


def _client(config: Config, timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=config.ollama.host, timeout=timeout)


async def check_health(config: Config | None = None) -> bool:
    """Return True if the Ollama server responds at all. Does not raise —
    use `ensure_running()` if you want the friendly exception instead."""
    config = config or get_config()
    try:
        async with _client(config, timeout=5.0) as client:
            resp = await client.get("/api/tags")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def ensure_running(config: Config | None = None) -> None:
    """Raise `OllamaNotRunningError` if the server can't be reached."""
    config = config or get_config()
    try:
        async with _client(config, timeout=5.0) as client:
            resp = await client.get("/api/tags")
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise OllamaNotRunningError(config.ollama.host, exc) from exc


async def is_model_pulled(model: str, config: Config | None = None) -> bool:
    """Check whether `model` is present in the local Ollama model list.

    Wraps connection failures into `OllamaNotRunningError` (matching
    `ensure_running()`) rather than letting a raw `httpx.ConnectError`
    escape — otherwise callers that call `ensure_model_pulled()` without an
    `ensure_running()` first would get an unfriendly stack trace instead of
    the intended friendly exception.
    """
    config = config or get_config()
    try:
        async with _client(config, timeout=10.0) as client:
            resp = await client.get("/api/tags")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError as exc:
        raise OllamaNotRunningError(config.ollama.host, exc) from exc
    names = {_with_tag(m["name"]) for m in data.get("models", []) if m.get("name")}
    # Ollama resolves an untagged name to `:latest`, so that's the only
    # loosening that's safe. Matching on the bare name alone treated
    # `llama3.1:70b` (or plain `llama3.1`) as pulled whenever `llama3.1:8b`
    # was — the readiness check passed and then every request 404'd.
    return _with_tag(model) in names


def _with_tag(name: str) -> str:
    """`name` with Ollama's implicit `:latest` tag made explicit."""
    return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"


async def ensure_model_pulled(model: str, config: Config | None = None) -> None:
    """Raise `ModelNotPulledError` if `model` isn't pulled locally."""
    if not await is_model_pulled(model, config):
        raise ModelNotPulledError(model)


async def list_local_models(config: Config | None = None) -> list[dict[str, Any]]:
    """Every locally-pulled model as reported by `GET /api/tags`
    (`{name, size, modified_at, ...}`). Empty list if Ollama isn't up."""
    config = config or get_config()
    try:
        async with _client(config, timeout=10.0) as client:
            resp = await client.get("/api/tags")
            resp.raise_for_status()
            return resp.json().get("models", [])
    except httpx.HTTPError:
        return []


async def pull_model(model: str, config: Config | None = None) -> AsyncIterator[dict[str, Any]]:
    """Pull `model`, yielding Ollama's streamed progress objects
    (`{status, completed?, total?}`). Raises `OllamaNotRunningError` if the
    server can't be reached before the stream starts."""
    config = config or get_config()
    import json as _json

    client = _client(config, timeout=None)
    try:
        async with client.stream(
            "POST", "/api/pull", json={"model": model, "stream": True}
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.strip():
                    yield _json.loads(line)
    except httpx.ConnectError as exc:
        raise OllamaNotRunningError(config.ollama.host, exc) from exc
    finally:
        await client.aclose()


async def delete_model(model: str, config: Config | None = None) -> None:
    """Delete a locally-pulled model (`DELETE /api/delete`)."""
    config = config or get_config()
    try:
        async with _client(config) as client:
            resp = await client.request("DELETE", "/api/delete", json={"model": model})
            resp.raise_for_status()
    except httpx.ConnectError as exc:
        raise OllamaNotRunningError(config.ollama.host, exc) from exc


async def embed(text: str, model: str | None = None, config: Config | None = None) -> list[float]:
    """Embed `text` using the configured embedding model (nomic-embed-text
    by default). Raises `OllamaNotRunningError`/`ModelNotPulledError` on
    failure, wrapping the raw httpx error otherwise."""
    config = config or get_config()
    model = model or config.models.embedding_model
    try:
        async with _client(config) as client:
            resp = await client.post("/api/embeddings", json={"model": model, "prompt": text})
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError as exc:
        raise OllamaNotRunningError(config.ollama.host, exc) from exc
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise ModelNotPulledError(model) from exc
        raise
    return data["embedding"]


async def generate_caption(
    prompt: str,
    image_b64: str | None = None,
    model: str | None = None,
    config: Config | None = None,
) -> str:
    """Generate a text (optionally vision-grounded) caption via
    `/api/generate` — used for llava captioning."""
    config = config or get_config()
    model = model or config.models.vision_model
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
    if image_b64 is not None:
        payload["images"] = [image_b64]
    try:
        async with _client(config, timeout=120.0) as client:
            resp = await client.post("/api/generate", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError as exc:
        raise OllamaNotRunningError(config.ollama.host, exc) from exc
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise ModelNotPulledError(model) from exc
        raise
    return data.get("response", "")


# --- Image captioning --------------------------------------------------
#
# `generate_caption()` above is a generic passthrough to `/api/generate`.
# `caption_image()` is the real vision-captioning entry point used by
# `gramvault.ai.pipeline` — it owns reading + base64-encoding the image
# file and supplying a sensible default prompt, so callers just pass a
# path.

DEFAULT_CAPTION_PROMPT = (
    "Describe this image in 2-3 concise, factual sentences. Focus on the "
    "main subject(s), setting, and any clearly visible text. Do not "
    "speculate about anything you can't actually see."
)


async def caption_image(
    image_path: Path,
    prompt: str = DEFAULT_CAPTION_PROMPT,
    model: str | None = None,
    config: Config | None = None,
) -> str:
    """Caption a single image file with the configured vision model
    (llava by default). Thin convenience wrapper over `generate_caption()`
    that handles reading + base64-encoding `image_path`."""
    image_bytes = Path(image_path).read_bytes()
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    return await generate_caption(prompt, image_b64=image_b64, model=model, config=config)


# --- Text chat completion -----------------------------------------------
#
# `gramvault.chat.service` needs a non-streaming and a streaming chat
# completion function against `/api/chat` (distinct from the
# embedding/vision helpers above).


async def chat_completion(
    messages: list[dict[str, str]],
    model: str | None = None,
    config: Config | None = None,
) -> str:
    """Non-streaming chat completion. `messages` is a list of
    `{"role": "system"|"user"|"assistant", "content": str}` dicts, mirroring
    Ollama's `/api/chat` request shape. Returns the full response text."""
    config = config or get_config()
    model = model or config.models.chat_model
    try:
        async with _client(config, timeout=120.0) as client:
            resp = await client.post(
                "/api/chat",
                json={"model": model, "messages": messages, "stream": False},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError as exc:
        raise OllamaNotRunningError(config.ollama.host, exc) from exc
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise ModelNotPulledError(model) from exc
        raise
    return data.get("message", {}).get("content", "")


async def chat_completion_full(
    messages: list[dict[str, str]],
    model: str | None = None,
    config: Config | None = None,
    *,
    max_tokens: int | None = None,
    json_mode: bool = False,
) -> dict[str, Any]:
    """Like `chat_completion()`, but returns the raw response dict instead
    of just the text, so callers can read `done_reason` (`"length"` means
    the reply was cut off at `num_predict`) and the `prompt_eval_count`
    /`eval_count` token counts Ollama reports for local models too."""
    config = config or get_config()
    model = model or config.models.chat_model
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    if max_tokens is not None:
        payload["options"] = {"num_predict": max_tokens}
    if json_mode:
        payload["format"] = "json"
    try:
        async with _client(config, timeout=120.0) as client:
            resp = await client.post("/api/chat", json=payload)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError as exc:
        raise OllamaNotRunningError(config.ollama.host, exc) from exc
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise ModelNotPulledError(model) from exc
        raise


async def stream_chat(
    messages: list[dict[str, str]],
    model: str | None = None,
    config: Config | None = None,
) -> AsyncIterator[str]:
    """Streaming chat completion. Yields response text tokens/fragments as
    they arrive from Ollama's newline-delimited-JSON `/api/chat` stream.

    Raises `OllamaNotRunningError`/`ModelNotPulledError` before yielding
    anything if the initial connection/request fails; once streaming has
    started, transport errors propagate as-is (the caller decides how to
    surface a mid-stream failure).
    """
    config = config or get_config()
    model = model or config.models.chat_model
    import json as _json

    client = _client(config, timeout=None)
    try:
        async with client.stream(
            "POST",
            "/api/chat",
            json={"model": model, "messages": messages, "stream": True},
        ) as resp:
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    raise ModelNotPulledError(model) from exc
                raise
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                chunk = _json.loads(line)
                if chunk.get("error"):
                    # A failure after the 200 (e.g. the model ran out of
                    # memory mid-reply) arrives as an `{"error": ...}` line;
                    # ignoring it saved a truncated reply as if complete.
                    raise ProviderError(f"Ollama error: {chunk['error']}")
                content = chunk.get("message", {}).get("content", "")
                if content:
                    yield content
                if chunk.get("done"):
                    break
    except httpx.ConnectError as exc:
        raise OllamaNotRunningError(config.ollama.host, exc) from exc
    finally:
        await client.aclose()
