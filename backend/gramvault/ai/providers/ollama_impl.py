"""Ollama provider — a thin adapter over `gramvault.ai.ollama_client`
(which owns the actual HTTP calls to a local Ollama server)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from gramvault.ai import ollama_client
from gramvault.ai.providers.base import Message
from gramvault.config import Config


class OllamaProvider:
    name = "ollama"
    kind = "local"

    def __init__(self, host: str) -> None:
        self.host = host
        # ollama_client reads the host off a Config; give it one pinned to
        # this provider's host so a per-task host override is honoured.
        self._config = Config.model_validate({"ollama": {"host": host}})

    async def ensure_ready(self, model: str) -> None:
        await ollama_client.ensure_running(self._config)
        await ollama_client.ensure_model_pulled(model, self._config)

    async def embed(self, model: str, text: str) -> list[float]:
        return await ollama_client.embed(text, model=model, config=self._config)

    async def caption_image(self, model: str, image_path: Path, prompt: str) -> str:
        return await ollama_client.caption_image(
            image_path, prompt=prompt, model=model, config=self._config
        )

    async def chat(self, model: str, messages: list[Message]) -> str:
        return await ollama_client.chat_completion(messages, model=model, config=self._config)

    def stream_chat(self, model: str, messages: list[Message]) -> AsyncIterator[str]:
        return ollama_client.stream_chat(messages, model=model, config=self._config)
