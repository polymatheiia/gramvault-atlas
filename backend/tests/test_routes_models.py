"""`/api/models` — overview, Ollama pull/delete jobs, task routing writes
to secrets.yaml, write-only secret storage, task self-test, re-embed."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from fastapi.testclient import TestClient

from gramvault.api.deps import get_config_dependency, get_config_path_dependency
from gramvault.config import Config, PathsConfig, load_config
from gramvault.db.session import session_scope
from gramvault.main import create_app


@pytest.fixture
def models_client(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    """A client whose config + config-path both resolve into tmp_path, so
    `PUT /tasks` / `PUT /secrets` write a tmp secrets.yaml, never the real
    one."""
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("paths:\n  db_path: " + str(tmp_path / "data" / "gv.db") + "\n")
    base = Config(
        paths=PathsConfig(
            library_dir=str(tmp_path / "library"),
            db_path=str(tmp_path / "data" / "gv.db"),
            chroma_dir=str(tmp_path / "data" / "chroma"),
        )
    )

    app = create_app(base)
    # `get_config_dependency` re-reads from disk so it reflects writes.
    app.dependency_overrides[get_config_dependency] = lambda: load_config(config_yaml)
    app.dependency_overrides[get_config_path_dependency] = lambda: config_yaml
    with TestClient(
        app, base_url="http://127.0.0.1", headers={"X-GramVault-Client": "1"}
    ) as client:
        yield client, tmp_path


class TestOverview:
    def test_lists_tasks_providers_and_ollama_state(self, models_client) -> None:
        client, _ = models_client
        with patch(
            "gramvault.api.routes_models.list_ollama_models",
            new_callable=AsyncMock,
            return_value=[{"name": "bge-m3:latest", "size": 1200000000}],
        ):
            body = client.get("/api/models").json()

        assert set(body["tasks"]) == {"chat", "vision", "embedding", "categorize", "digest"}
        assert body["tasks"]["embedding"]["source"] == "default"
        assert body["tasks"]["embedding"]["provider"] == "ollama"
        assert body["ollama_models"][0]["name"] == "bge-m3:latest"
        assert body["ollama_reachable"] is True
        assert body["auth_token_set"] is False

    def test_suggested_is_static(self, models_client) -> None:
        client, _ = models_client
        rows = client.get("/api/models/suggested").json()
        assert any(r["model"] == "bge-m3" for r in rows)
        assert all("tasks" in r and "note" in r for r in rows)


class TestTaskRouting:
    def test_pointing_chat_at_anthropic_persists_to_secrets_yaml(self, models_client) -> None:
        client, tmp_path = models_client
        with patch(
            "gramvault.api.routes_models.list_ollama_models", new_callable=AsyncMock, return_value=[]
        ):
            resp = client.put(
                "/api/models/tasks",
                json={
                    "task": "chat",
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "provider_kind": "anthropic",
                },
            )
        assert resp.status_code == 200
        assert resp.json()["tasks"]["chat"] == {
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "source": "configured",
        }
        assert resp.json()["needs_reembed"] is False

        written = yaml.safe_load((tmp_path / "secrets.yaml").read_text())
        assert written["ai"]["chat"] == {"provider": "anthropic", "model": "claude-sonnet-5"}
        assert written["providers"]["anthropic"]["kind"] == "anthropic"

    def test_changing_base_url_clears_the_stored_key(self, models_client) -> None:
        # Regression for audit finding S6: re-pointing a provider's
        # base_url must clear its stored key, or the next call sends that
        # key to whatever host base_url now names.
        client, tmp_path = models_client
        with patch(
            "gramvault.api.routes_models.list_ollama_models", new_callable=AsyncMock, return_value=[]
        ):
            client.put(
                "/api/models/tasks",
                json={
                    "task": "chat",
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "provider_kind": "anthropic",
                },
            )
            client.put(
                "/api/models/secrets", json={"provider": "anthropic", "api_key": "sk-secret"}
            )
            overview_before = client.get("/api/models").json()
            provider_before = next(
                p for p in overview_before["providers"] if p["name"] == "anthropic"
            )
            assert provider_before["api_key_set"] is True

            resp = client.put(
                "/api/models/tasks",
                json={
                    "task": "chat",
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "provider_kind": "anthropic",
                    "base_url": "https://attacker.example",
                },
            )
        assert resp.status_code == 200
        provider_after = next(
            p for p in resp.json()["providers"] if p["name"] == "anthropic"
        )
        assert provider_after["api_key_set"] is False
        assert provider_after["base_url"] == "https://attacker.example"

        written = yaml.safe_load((tmp_path / "secrets.yaml").read_text())
        assert "api_key" not in written["providers"]["anthropic"]

    def test_changing_embedding_model_flags_needs_reembed(self, models_client) -> None:
        client, _ = models_client
        with patch(
            "gramvault.api.routes_models.list_ollama_models", new_callable=AsyncMock, return_value=[]
        ):
            resp = client.put(
                "/api/models/tasks",
                json={"task": "embedding", "provider": "ollama", "model": "bge-m3"},
            )
        assert resp.status_code == 200
        assert resp.json()["needs_reembed"] is True  # default was nomic-embed-text

    def test_bad_task_is_422(self, models_client) -> None:
        client, _ = models_client
        resp = client.put(
            "/api/models/tasks", json={"task": "translate", "provider": "ollama", "model": "x"}
        )
        assert resp.status_code == 422


class TestSecrets:
    def test_api_key_is_write_only(self, models_client) -> None:
        client, tmp_path = models_client
        assert (
            client.put(
                "/api/models/secrets", json={"provider": "anthropic", "api_key": "sk-ant-xyz"}
            ).status_code
            == 204
        )
        written = yaml.safe_load((tmp_path / "secrets.yaml").read_text())
        assert written["providers"]["anthropic"]["api_key"] == "sk-ant-xyz"

        # the key never comes back out
        with patch(
            "gramvault.api.routes_models.list_ollama_models", new_callable=AsyncMock, return_value=[]
        ):
            overview = client.get("/api/models").json()
        anthropic = next(p for p in overview["providers"] if p["name"] == "anthropic")
        assert anthropic["api_key_set"] is True
        assert "api_key" not in anthropic

    def test_auth_token(self, models_client) -> None:
        client, tmp_path = models_client
        assert (
            client.put(
                "/api/models/secrets", json={"set_auth_token": True, "auth_token": "s3cr3t"}
            ).status_code
            == 204
        )
        assert yaml.safe_load((tmp_path / "secrets.yaml").read_text())["auth"]["token"] == "s3cr3t"

    def test_must_target_exactly_one_thing(self, models_client) -> None:
        client, _ = models_client
        assert client.put("/api/models/secrets", json={}).status_code == 422
        assert (
            client.put(
                "/api/models/secrets",
                json={"provider": "x", "api_key": "y", "set_auth_token": True},
            ).status_code
            == 422
        )


class TestPull:
    def test_pull_creates_a_job_and_streams_progress(self, models_client) -> None:
        client, _ = models_client

        async def fake_pull(model, config):  # noqa: ARG001
            yield {"status": "pulling", "completed": 10, "total": 100}
            yield {"status": "success"}

        with patch("gramvault.api.routes_models.ollama_client.pull_model", side_effect=fake_pull):
            resp = client.post("/api/models/pull", json={"model": "qwen2.5:3b"})
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["kind"] == "model_pull"
        assert job["status"] == "done"
        assert job["result"]["status"] == "success"

    def test_pull_error_event_fails_the_job(self, models_client) -> None:
        """A failed pull arrives as an `{"error": ...}` event on a 200
        stream; it used to finish the job as `done`."""
        client, _ = models_client

        async def fake_pull(model, config):  # noqa: ARG001
            yield {"status": "pulling manifest"}
            yield {"error": "pull model manifest: file does not exist"}

        with patch("gramvault.api.routes_models.ollama_client.pull_model", side_effect=fake_pull):
            resp = client.post("/api/models/pull", json={"model": "no-such-model"})
        job = client.get(f"/api/jobs/{resp.json()['job_id']}").json()

        assert job["status"] == "failed"
        assert "file does not exist" in job["error_message"]

    def test_second_pull_conflicts(self, models_client) -> None:
        client, _ = models_client
        from gramvault.api import jobs
        from gramvault.models.schemas import JobKind

        cfg = client.app.dependency_overrides[get_config_dependency]()
        with session_scope(cfg) as conn:
            jobs.create(conn, JobKind.MODEL_PULL)  # a pull already running
        resp = client.post("/api/models/pull", json={"model": "x"})
        assert resp.status_code == 409


class TestTest:
    def test_embedding_task_does_a_real_tiny_embed(self, models_client) -> None:
        client, _ = models_client
        with patch("gramvault.ai.ollama_client.ensure_running", new_callable=AsyncMock), patch(
            "gramvault.ai.ollama_client.ensure_model_pulled", new_callable=AsyncMock
        ), patch(
            "gramvault.ai.ollama_client.embed", new_callable=AsyncMock, return_value=[0.0] * 8
        ):
            resp = client.post("/api/models/test", json={"task": "embedding"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "8 dimensions" in body["detail"]

    def test_failure_is_reported_not_raised(self, models_client) -> None:
        client, _ = models_client
        from gramvault.ai.ollama_client import OllamaNotRunningError

        with patch(
            "gramvault.ai.ollama_client.ensure_running",
            new_callable=AsyncMock,
            side_effect=OllamaNotRunningError("http://x"),
        ):
            resp = client.post("/api/models/test", json={"task": "chat"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
