"""The `Provider` protocol + shared exceptions.

A provider is a stateless adapter around one model backend. Not every
backend supports every task (Anthropic has no embeddings API, for
example) — the unsupported methods raise `ProviderError`. The config /
registry is responsible for never routing a task to a provider that can't
serve it; the raise is a backstop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from gramvault.ai.errors import (
    ProviderCapabilityError,
    ProviderError,
    ProviderNotReadyError,
)

__all__ = [
    "ChatResult",
    "Message",
    "Provider",
    "ProviderCapabilityError",
    "ProviderError",
    "ProviderNotReadyError",
]

# A chat message: {"role": "system"|"user"|"assistant", "content": str}.
Message = dict[str, str]


@dataclass
class ChatResult:
    """A `complete()` reply, with the metadata `chat()` throws away.

    `tokens_in`/`tokens_out` are the provider's own usage accounting when it
    reports one (exact), else `None` — callers fall back to their own
    char/4 estimate in that case. `truncated` is True when the provider cut
    the reply off for hitting `max_tokens` (Anthropic `stop_reason ==
    "max_tokens"`, OpenAI-compatible `finish_reason == "length"`, Ollama
    `done_reason == "length"`) rather than for reaching a natural stop.
    """

    text: str
    truncated: bool = False
    tokens_in: int | None = None
    tokens_out: int | None = None


@runtime_checkable
class Provider(Protocol):
    """One model backend. Construct via `providers.get_provider(task)`."""

    name: str  # 'ollama' | 'openai' | 'anthropic' | a custom label
    kind: str  # 'local' | 'api'

    async def ensure_ready(self, model: str) -> None:
        """Raise `ProviderNotReadyError` if a request for `model` would
        fail for a setup reason (server down, model absent, no API key)."""
        ...

    async def embed(self, model: str, text: str) -> list[float]:
        ...

    async def caption_image(self, model: str, image_path: Path, prompt: str) -> str:
        ...

    async def chat(self, model: str, messages: list[Message]) -> str:
        ...

    async def complete(
        self,
        model: str,
        messages: list[Message],
        *,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> ChatResult:
        """Like `chat()`, but for callers (classifier, digest) that need to
        know whether the reply was truncated and how many tokens it cost.
        `max_tokens` overrides the provider's default cap; `json_mode` is
        best-effort (silently ignored where the backend has no such mode,
        e.g. Anthropic) rather than a hard requirement."""
        ...

    def stream_chat(self, model: str, messages: list[Message]) -> AsyncIterator[str]:
        ...
