"""Regression tests for audit findings S2, S11, S12: Host allowlist,
disabled docs/CORS in production, and the cross-site request guard."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from gramvault.api.deps import get_config_dependency
from gramvault.config import Config
from gramvault.main import create_app


@pytest.fixture
def prod_client(tmp_config: Config) -> Iterator[TestClient]:
    app = create_app(tmp_config)
    app.dependency_overrides[get_config_dependency] = lambda: tmp_config
    with TestClient(
        app, base_url="http://127.0.0.1", headers={"X-GramVault-Client": "1"}
    ) as client:
        yield client


def test_docs_are_disabled_without_dev_mode(prod_client: TestClient) -> None:
    # `_FRONTEND_DIST_DIR` is a module-level path to the real repo build,
    # not something this fixture's tmp_config isolates — so if a real
    # `frontend/dist` exists, /docs falls through to the SPA shell (200);
    # if not, the unregistered route 404s. Either way, FastAPI's own
    # Swagger UI / raw OpenAPI schema must never be reachable in prod
    # (audit finding S11): no swagger-ui markup, and no JSON schema body.
    docs = prod_client.get("/docs")
    assert docs.status_code in (200, 404)
    assert "swagger-ui" not in docs.text.lower()
    openapi = prod_client.get("/openapi.json")
    assert openapi.status_code in (200, 404)
    assert openapi.headers.get("content-type", "").split(";")[0] != "application/json"


def test_bad_host_header_is_rejected(prod_client: TestClient) -> None:
    response = prod_client.get("/api/health", headers={"Host": "evil.example"})
    assert response.status_code == 400


def test_configured_host_is_accepted(prod_client: TestClient) -> None:
    response = prod_client.get("/api/health", headers={"Host": "127.0.0.1:8000"})
    assert response.status_code == 200


def test_security_headers_present(prod_client: TestClient) -> None:
    response = prod_client.get("/api/health")
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert "content-security-policy" in {k.lower() for k in response.headers}


class TestCrossSiteGuard:
    def test_cross_site_post_without_client_header_is_rejected(
        self, prod_client: TestClient
    ) -> None:
        response = prod_client.post(
            "/api/models/reembed", headers={"X-GramVault-Client": ""}
        )
        assert response.status_code == 403

    def test_sec_fetch_site_cross_site_is_rejected_even_on_get(
        self, prod_client: TestClient
    ) -> None:
        response = prod_client.get(
            "/api/health", headers={"Sec-Fetch-Site": "cross-site"}
        )
        assert response.status_code == 403

    def test_foreign_origin_is_rejected(self, prod_client: TestClient) -> None:
        response = prod_client.get(
            "/api/health", headers={"Origin": "https://evil.example"}
        )
        assert response.status_code == 403

    def test_same_origin_get_with_client_header_passes(
        self, prod_client: TestClient
    ) -> None:
        response = prod_client.get("/api/health")
        assert response.status_code == 200

    def test_get_without_origin_or_sec_fetch_site_passes(
        self, prod_client: TestClient
    ) -> None:
        # A plain curl call (no browser headers at all) must still work.
        response = prod_client.get(
            "/api/health", headers={"X-GramVault-Client": ""}
        )
        assert response.status_code == 200

    def test_cross_site_media_get_is_rejected(self, prod_client: TestClient) -> None:
        # /media can't be bearer-gated (no Authorization header on a plain
        # <img>/<video> load), so this guard is its only defense — a
        # cross-site image/video load still sends Sec-Fetch-Site.
        response = prod_client.get(
            "/media/anything.jpg", headers={"Sec-Fetch-Site": "cross-site"}
        )
        assert response.status_code == 403

    def test_same_site_media_get_passes_through_to_404(
        self, prod_client: TestClient
    ) -> None:
        # Not cross-site -> reaches the actual /media route (which then
        # 404s, since no such file exists in this fixture).
        response = prod_client.get(
            "/media/anything.jpg", headers={"X-GramVault-Client": ""}
        )
        assert response.status_code == 404

    def test_https_origin_behind_a_tls_terminating_proxy_is_same_origin(
        self, prod_client: TestClient
    ) -> None:
        # Regression: same-origin must be judged by host, not scheme.
        # Behind Caddy/nginx terminating TLS, the browser's Origin is
        # https://... while uvicorn (not told to trust forwarded headers)
        # still sees a plain http:// request — comparing scheme too would
        # reject every legitimate request in that (very common) setup.
        response = prod_client.get(
            "/api/health",
            headers={"Origin": "https://127.0.0.1", "Host": "127.0.0.1"},
        )
        assert response.status_code == 200

    def test_same_host_different_port_is_cross_site(
        self, prod_client: TestClient
    ) -> None:
        response = prod_client.get(
            "/api/health", headers={"Origin": "http://127.0.0.1:9999"}
        )
        assert response.status_code == 403
