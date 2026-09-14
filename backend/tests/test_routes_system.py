"""`/api/system/info` — read-only settings snapshot for the Settings
page's Security/Advanced tabs (UX-9)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gramvault.api.deps import get_config_dependency, get_config_path_dependency
from gramvault.config import AuthConfig, Config, PathsConfig, ServerConfig
from gramvault.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("paths:\n  db_path: " + str(tmp_path / "data" / "gv.db") + "\n")
    base = Config(
        paths=PathsConfig(
            library_dir=str(tmp_path / "library"),
            db_path=str(tmp_path / "data" / "gv.db"),
            chroma_dir=str(tmp_path / "data" / "chroma"),
        ),
        server=ServerConfig(host="127.0.0.1", port=8000, allowed_hosts=["nas.tailnet.ts.net"]),
        auth=AuthConfig(token="s3cr3t"),
    )
    app = create_app(base)
    app.dependency_overrides[get_config_dependency] = lambda: base
    app.dependency_overrides[get_config_path_dependency] = lambda: config_yaml
    with TestClient(
        app, base_url="http://127.0.0.1", headers={"X-GramVault-Client": "1", "Authorization": "Bearer s3cr3t"}
    ) as c:
        yield c


def test_system_info_shape(client: TestClient) -> None:
    r = client.get("/api/system/info")
    assert r.status_code == 200
    body = r.json()
    assert body["server_host"] == "127.0.0.1"
    assert body["allowed_hosts"] == ["nas.tailnet.ts.net"]
    assert body["auth_enabled"] is True
    assert body["chroma_telemetry_disabled"] is True
    assert body["library_dir"].endswith("library")


def test_system_info_never_leaks_token(client: TestClient) -> None:
    r = client.get("/api/system/info")
    assert "s3cr3t" not in r.text


def test_health_reflects_library_state(client: TestClient) -> None:
    from gramvault.api.deps import get_config_dependency
    from gramvault.db.session import session_scope

    # Seed through the same config the app's dependency override resolves
    # to, so it lands in the same tmp_path DB the running client sees.
    app_config = client.app.dependency_overrides[get_config_dependency]()
    with session_scope(app_config) as conn:
        author_id = conn.execute("INSERT INTO authors (username) VALUES ('acc') RETURNING id").fetchone()["id"]
        conn.execute(
            "INSERT INTO items (media_type, caption, author_id, enrichment_status) "
            "VALUES ('reel', 'x', ?, 'done')",
            (author_id,),
        )

    r = client.get("/api/health")
    body = r.json()
    assert body["item_count"] == 1
    assert body["enriched_count"] == 1
