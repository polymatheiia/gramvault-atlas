"""Resolve an AI task into a `(Provider, model_name)` pair.

    from gramvault.ai.providers import get_provider

    provider, model = get_provider("embedding")
    await provider.ensure_ready(model)
    vector = await provider.embed(model, "some text")

Providers are cheap to construct (they just hold a host/base_url/key), so
this doesn't cache — a config change or a rotated key takes effect on the
next call once `get_config()` is refreshed.
"""

from __future__ import annotations

from gramvault.ai.providers.anthropic_impl import AnthropicProvider
from gramvault.ai.providers.base import Provider, ProviderError
from gramvault.ai.providers.ollama_impl import OllamaProvider
from gramvault.ai.providers.openai_impl import OpenAICompatProvider
from gramvault.config import Config, ProviderConfig, get_config


def build_provider(pc: ProviderConfig, config: Config | None = None) -> Provider:
    """Construct a provider from its config (with the Ollama host filled
    in from the top-level `ollama:` block when not overridden)."""
    config = config or get_config()
    if pc.kind == "ollama":
        return OllamaProvider(host=pc.base_url or config.ollama.host)
    if pc.kind == "openai":
        return OpenAICompatProvider(api_key=pc.api_key, base_url=pc.base_url)
    if pc.kind == "anthropic":
        return AnthropicProvider(api_key=pc.api_key, base_url=pc.base_url)
    raise ProviderError(f"unknown provider kind {pc.kind!r}")


def get_provider(task: str, config: Config | None = None) -> tuple[Provider, str]:
    config = config or get_config()
    pc, model = config.resolve_task(task)
    return build_provider(pc, config), model


async def list_ollama_models(config: Config | None = None) -> list[dict]:
    """Locally-pulled Ollama models (name/size/modified). Empty if Ollama
    isn't reachable. Convenience for the Models settings page."""
    from gramvault.ai import ollama_client

    return await ollama_client.list_local_models(config or get_config())
