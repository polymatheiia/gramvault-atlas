"""Regression tests for the SPA fallback route (audit finding S1):
`GET /{full_path}` must never serve a file outside `frontend/dist`, no
matter how `full_path` is spelled."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gramvault import main as main_module
from gramvault.api.deps import get_config_dependency
from gramvault.config import Config


@pytest.fixture
def dist_client(tmp_path: Path, tmp_config: Config, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client with a fake `frontend/dist` (so the SPA fallback route
    registers) and a secret file one level above it, standing in for
    `secrets.yaml` at the repo root."""
    dist_dir = tmp_path / "frontend" / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text('<div id="root">shell</div>', encoding="utf-8")
    (tmp_path / "secrets.yaml").write_text("auth:\n  token: leaked\n", encoding="utf-8")

    monkeypatch.setattr(main_module, "_FRONTEND_DIST_DIR", dist_dir)

    app = main_module.create_app(tmp_config)
    app.dependency_overrides[get_config_dependency] = lambda: tmp_config
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


@pytest.mark.parametrize(
    "path",
    [
        "/../secrets.yaml",
        "/..%2fsecrets.yaml",
        "/%2e%2e/secrets.yaml",
        "/..%2f..%2fsecrets.yaml",
    ],
)
def test_spa_fallback_never_leaves_dist(dist_client: TestClient, path: str) -> None:
    r = dist_client.get(path)
    assert r.status_code == 200
    assert b"leaked" not in r.content
    assert b'<div id="root">shell</div>' in r.content


def test_spa_fallback_still_serves_real_route(dist_client: TestClient) -> None:
    r = dist_client.get("/items/42")
    assert r.status_code == 200
    assert b'<div id="root">shell</div>' in r.content
