"""Bearer-token middleware (`gramvault.api.auth`)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from gramvault import config as config_module
from gramvault.api.deps import get_config_dependency
from gramvault.config import Config, PathsConfig
from gramvault.main import create_app


@pytest.fixture
def token_client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    """A client whose `get_config()` (the global the middleware reads)
    resolves to a config.yaml with `auth.token` set to 'sekret'."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "auth:\n  token: sekret\n"
        f"paths:\n  db_path: {tmp_path / 'gv.db'}\n"
        f"  library_dir: {tmp_path / 'lib'}\n"
        f"  chroma_dir: {tmp_path / 'chroma'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(config_module.CONFIG_PATH_ENV_VAR, str(cfg_path))
    # conftest's autouse _clear_config_cache already busts the lru_cache
    # around this test, so the middleware's get_config() picks up cfg_path.

    tmp_cfg = Config(
        paths=PathsConfig(
            library_dir=str(tmp_path / "lib"),
            db_path=str(tmp_path / "gv.db"),
            chroma_dir=str(tmp_path / "chroma"),
        )
    )
    app = create_app(tmp_cfg)
    app.dependency_overrides[get_config_dependency] = lambda: tmp_cfg
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


def test_health_is_open_without_a_token(token_client: TestClient) -> None:
    assert token_client.get("/api/health").status_code == 200


def test_protected_endpoint_needs_the_token(token_client: TestClient) -> None:
    assert token_client.get("/api/library/categories").status_code == 401
    assert token_client.get("/api/jobs").status_code == 401


def test_media_is_not_bearer_gated(token_client: TestClient) -> None:
    # /media is loaded via plain <img src>/<video src>, which can never
    # carry an Authorization header — bearer auth deliberately does not
    # cover it (see auth.py's docstring); its defense is CrossSiteGuard
    # instead (test_main_prod_hardening.py). A 404 here (no such file, no
    # frontend/dist in this fixture) — not 401 — proves that.
    assert token_client.get("/media/anything.jpg").status_code == 404


def test_docs_and_openapi_need_the_token(token_client: TestClient) -> None:
    assert token_client.get("/docs").status_code == 401
    assert token_client.get("/redoc").status_code == 401
    assert token_client.get("/openapi.json").status_code == 401


def test_spa_shell_stays_open_without_a_token(token_client: TestClient) -> None:
    # `/` must never be 401'd by the auth gate -- the browser can't attach
    # an Authorization header to a plain navigation, see auth.py's
    # docstring. If frontend/dist isn't built the SPA fallback route isn't
    # registered and this 404s instead, which is equally "not 401".
    assert token_client.get("/").status_code in (200, 404)


def test_correct_bearer_token_is_accepted(token_client: TestClient) -> None:
    r = token_client.get("/api/library/categories", headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 200


def test_wrong_token_is_rejected(token_client: TestClient) -> None:
    r = token_client.get("/api/jobs", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    assert "token" in r.json()["detail"].lower()


def test_no_token_configured_is_a_noop(client: TestClient) -> None:
    # The default `client` fixture's config has no auth block.
    assert client.get("/api/jobs").status_code == 200
