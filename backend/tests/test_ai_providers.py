"""Provider abstraction: task -> (provider, model) resolution, legacy
config fallback, secrets.yaml loading, and the OpenAI/Anthropic adapters
(HTTP mocked with respx)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import respx
from httpx import Response

from gramvault.ai.providers import get_provider
from gramvault.ai.providers.anthropic_impl import AnthropicProvider
from gramvault.ai.providers.base import ProviderCapabilityError, ProviderNotReadyError
from gramvault.ai.providers.ollama_impl import OllamaProvider
from gramvault.ai.providers.openai_impl import OpenAICompatProvider
from gramvault.config import Config, load_config


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TestResolution:
    def test_legacy_config_routes_every_task_to_ollama(self) -> None:
        cfg = Config.model_validate(
            {
                "models": {
                    "chat_model": "llama3.2:3b",
                    "vision_model": "minicpm-v",
                    "embedding_model": "bge-m3",
                },
                "ollama": {"host": "http://box:11434"},
            }
        )
        for task, model in (
            ("chat", "llama3.2:3b"),
            ("categorize", "llama3.2:3b"),
            ("digest", "llama3.2:3b"),
            ("vision", "minicpm-v"),
            ("embedding", "bge-m3"),
        ):
            provider, resolved = get_provider(task, cfg)
            assert isinstance(provider, OllamaProvider)
            assert provider.host == "http://box:11434"
            assert resolved == model

    def test_ai_block_overrides_a_single_task(self) -> None:
        cfg = Config.model_validate(
            {
                "ai": {"chat": {"provider": "anthropic", "model": "claude-sonnet-5"}},
                "providers": {"anthropic": {"kind": "anthropic", "api_key": "sk-test"}},
            }
        )
        chat, model = get_provider("chat", cfg)
        assert isinstance(chat, AnthropicProvider) and model == "claude-sonnet-5"
        # untouched task still falls back to ollama
        emb, _ = get_provider("embedding", cfg)
        assert isinstance(emb, OllamaProvider)

    def test_provider_name_with_no_providers_block_assumes_local_ollama(self) -> None:
        cfg = Config.model_validate(
            {"ai": {"chat": {"provider": "lmstudio", "model": "whatever"}}}
        )
        provider, _ = get_provider("chat", cfg)
        assert isinstance(provider, OllamaProvider)

    def test_unknown_task_raises(self) -> None:
        with pytest.raises(ValueError):
            get_provider("nonsense")


class TestSecretsLoading:
    def test_secrets_yaml_merges_over_config(self, tmp_path: Path) -> None:
        (tmp_path / "config.yaml").write_text(
            "providers:\n  anthropic:\n    kind: anthropic\n", encoding="utf-8"
        )
        (tmp_path / "secrets.yaml").write_text(
            "providers:\n  anthropic:\n    api_key: sk-secret\nauth:\n  token: hunter2\n",
            encoding="utf-8",
        )
        cfg = load_config(tmp_path / "config.yaml")
        assert cfg.providers["anthropic"].api_key == "sk-secret"
        assert cfg.providers["anthropic"].kind == "anthropic"
        assert cfg.auth.token == "hunter2"

    def test_api_key_env_is_used_when_no_explicit_key(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        (tmp_path / "config.yaml").write_text(
            "providers:\n  openai:\n    kind: openai\n    api_key_env: MY_OPENAI_KEY\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("MY_OPENAI_KEY", "sk-from-env")
        cfg = load_config(tmp_path / "config.yaml")
        assert cfg.providers["openai"].api_key == "sk-from-env"


class TestOpenAIProvider:
    @pytest.mark.anyio
    async def test_missing_key_for_remote_endpoint_is_not_ready(self) -> None:
        with pytest.raises(ProviderNotReadyError):
            await OpenAICompatProvider(api_key=None).ensure_ready("gpt-4o")

    @pytest.mark.anyio
    async def test_localhost_endpoint_needs_no_key(self) -> None:
        await OpenAICompatProvider(
            api_key=None, base_url="http://localhost:1234/v1"
        ).ensure_ready("local-model")

    @respx.mock
    @pytest.mark.anyio
    async def test_chat_hits_chat_completions(self) -> None:
        route = respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=Response(200, json={"choices": [{"message": {"content": "hi there"}}]})
        )
        out = await OpenAICompatProvider(api_key="sk-x").chat(
            "gpt-4o", [{"role": "user", "content": "hi"}]
        )
        assert out == "hi there"
        assert route.calls.last.request.headers["authorization"] == "Bearer sk-x"

    @respx.mock
    @pytest.mark.anyio
    async def test_401_becomes_not_ready(self) -> None:
        respx.post("https://api.openai.com/v1/embeddings").mock(return_value=Response(401))
        with pytest.raises(ProviderNotReadyError):
            await OpenAICompatProvider(api_key="bad").embed("text-embedding-3-small", "x")

    @respx.mock
    @pytest.mark.anyio
    async def test_complete_reports_truncation_and_usage(self) -> None:
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "cut off"}, "finish_reason": "length"}
                    ],
                    "usage": {"prompt_tokens": 42, "completion_tokens": 7},
                },
            )
        )
        result = await OpenAICompatProvider(api_key="sk-x").complete(
            "gpt-4o", [{"role": "user", "content": "hi"}], max_tokens=8
        )
        assert result.text == "cut off"
        assert result.truncated is True
        assert (result.tokens_in, result.tokens_out) == (42, 7)

    @respx.mock
    @pytest.mark.anyio
    async def test_json_mode_falls_back_when_backend_rejects_response_format(self) -> None:
        route = respx.post("http://localhost:8080/v1/chat/completions")
        route.side_effect = [
            Response(400, json={"error": "unknown field response_format"}),
            Response(200, json={"choices": [{"message": {"content": "{}"}}]}),
        ]
        result = await OpenAICompatProvider(
            api_key="sk-x", base_url="http://localhost:8080/v1"
        ).complete("local-model", [{"role": "user", "content": "hi"}], json_mode=True)
        assert result.text == "{}"
        assert route.call_count == 2
        assert "response_format" not in json.loads(route.calls[1].request.content)

    @respx.mock
    @pytest.mark.anyio
    async def test_retries_on_429_then_succeeds(self, monkeypatch) -> None:
        from gramvault.ai.providers import retry as retry_mod

        monkeypatch.setattr(retry_mod.asyncio, "sleep", AsyncMock())
        route = respx.post("https://api.openai.com/v1/chat/completions")
        route.side_effect = [
            Response(429, headers={"retry-after": "0"}),
            Response(200, json={"choices": [{"message": {"content": "ok"}}]}),
        ]
        out = await OpenAICompatProvider(api_key="sk-x").chat(
            "gpt-4o", [{"role": "user", "content": "hi"}]
        )
        assert out == "ok"
        assert route.call_count == 2


class TestAnthropicProvider:
    @pytest.mark.anyio
    async def test_embed_is_unsupported(self) -> None:
        with pytest.raises(ProviderCapabilityError):
            await AnthropicProvider(api_key="k").embed("claude-sonnet-5", "x")

    @respx.mock
    @pytest.mark.anyio
    async def test_chat_splits_system_and_reads_text_blocks(self) -> None:
        route = respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=Response(200, json={"content": [{"type": "text", "text": "answer"}]})
        )
        out = await AnthropicProvider(api_key="sk-ant").chat(
            "claude-sonnet-5",
            [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "q?"},
            ],
        )
        assert out == "answer"
        body = json.loads(route.calls.last.request.content)
        assert body["system"] == "be terse"
        assert body["messages"] == [{"role": "user", "content": "q?"}]
        assert route.calls.last.request.headers["x-api-key"] == "sk-ant"

    @respx.mock
    @pytest.mark.anyio
    async def test_403_becomes_not_ready(self) -> None:
        respx.post("https://api.anthropic.com/v1/messages").mock(return_value=Response(403))
        with pytest.raises(ProviderNotReadyError):
            await AnthropicProvider(api_key="bad").chat("claude-sonnet-5", [{"role": "user", "content": "x"}])

    @respx.mock
    @pytest.mark.anyio
    async def test_complete_reports_truncation_and_usage(self) -> None:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=Response(
                200,
                json={
                    "content": [{"type": "text", "text": "cut off"}],
                    "stop_reason": "max_tokens",
                    "usage": {"input_tokens": 100, "output_tokens": 8192},
                },
            )
        )
        result = await AnthropicProvider(api_key="sk-ant").complete(
            "claude-sonnet-5", [{"role": "user", "content": "q?"}], max_tokens=8192
        )
        assert result.text == "cut off"
        assert result.truncated is True
        assert (result.tokens_in, result.tokens_out) == (100, 8192)

    @respx.mock
    @pytest.mark.anyio
    async def test_retries_on_503_then_succeeds(self, monkeypatch) -> None:
        from gramvault.ai.providers import retry as retry_mod

        monkeypatch.setattr(retry_mod.asyncio, "sleep", AsyncMock())
        route = respx.post("https://api.anthropic.com/v1/messages")
        route.side_effect = [
            Response(503),
            Response(200, json={"content": [{"type": "text", "text": "ok"}]}),
        ]
        out = await AnthropicProvider(api_key="sk-ant").chat(
            "claude-sonnet-5", [{"role": "user", "content": "q?"}]
        )
        assert out == "ok"
        assert route.call_count == 2
