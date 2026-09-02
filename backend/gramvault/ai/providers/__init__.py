"""Pluggable model providers.

GramVault runs its per-task models (chat, vision, embedding, categorize,
digest) through a small `Provider` protocol so each task can point at a
local Ollama model *or* a hosted API (OpenAI-compatible, Anthropic)
independently. `get_provider(task)` resolves the config into a
`(provider, model_name)` pair; callers never construct a provider
directly.

`config.yaml` shape:

    ai:
      chat:      {provider: anthropic, model: claude-sonnet-5}
      embedding: {provider: ollama,    model: bge-m3}
    providers:
      anthropic: {kind: anthropic, api_key_env: ANTHROPIC_API_KEY}
      ollama:    {kind: ollama}

If `ai:` doesn't cover a task, the legacy flat `models:` / `ollama:`
config is used, so an existing config.yaml keeps working unchanged.
API keys come from `secrets.yaml` (gitignored) or an env var, never from
`config.yaml`.
"""

from gramvault.ai.providers.base import (
    Provider,
    ProviderError,
    ProviderNotReadyError,
)
from gramvault.ai.providers.registry import (
    build_provider,
    get_provider,
    list_ollama_models,
)

__all__ = [
    "Provider",
    "ProviderError",
    "ProviderNotReadyError",
    "build_provider",
    "get_provider",
    "list_ollama_models",
]
