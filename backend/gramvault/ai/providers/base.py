"""The `Provider` protocol + shared exceptions.

A provider is a stateless adapter around one model backend. Not every
backend supports every task (Anthropic has no embeddings API, for
example) — the unsupported methods raise `ProviderError`. The config /
registry is responsible for never routing a task to a provider that can't
serve it; the raise is a backstop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol, runtime_checkable

from gramvault.ai.errors import (
    ProviderCapabilityError,
    ProviderError,
    ProviderNotReadyError,
)

__all__ = [
    "Message",
    "Provider",
    "ProviderCapabilityError",
    "ProviderError",
    "ProviderNotReadyError",
]

# A chat message: {"role": "system"|"user"|"assistant", "content": str}.
Message = dict[str, str]


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

    def stream_chat(self, model: str, messages: list[Message]) -> AsyncIterator[str]:
        ...
