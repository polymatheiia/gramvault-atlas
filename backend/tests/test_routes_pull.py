"""Tests for the /api/pull/* HTTP surface: config gating, cookie connect,
session reporting, and job conflict handling. The saved-feed walk itself
is covered in test_ingestion_instagram.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gramvault.api.deps import get_config_dependency
from gramvault.config import Config, PathsConfig, PullConfig
from gramvault.db.session import get_connection, init_db, session_scope
from gramvault.ingestion import instagram
from gramvault.main import create_app
from gramvault.models.schemas import JobKind


class _FakeLoader:
    login_user: str | None = "tester"

    def __init__(self, **kwargs):
        self.context = SimpleNamespace(_session=SimpleNamespace(cookies={}), username=None)

    def test_login(self):
        return _FakeLoader.login_user

    def save_session_to_file(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("fake", encoding="utf-8")


@pytest.fixture(autouse=True)
def _fake_instaloader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        instagram,
        "_load_instaloader",
        lambda: SimpleNamespace(Instaloader=_FakeLoader),
    )
    _FakeLoader.login_user = "tester"


def _make_config(tmp_path: Path, *, enabled: bool) -> Config:
    return Config(
        paths=PathsConfig(
            library_dir=str(tmp_path / "library"),
            db_path=str(tmp_path / "data" / "gramvault.db"),
            chroma_dir=str(tmp_path / "data" / "chroma"),
        ),
        pull=PullConfig(enabled=enabled, session_dir=str(tmp_path / "ig")),
    )


def _client(config: Config) -> Iterator[TestClient]:
    conn = get_connection(config)
    init_db(conn)
    conn.close()
    app = create_app(config)
    app.dependency_overrides[get_config_dependency] = lambda: config
    with TestClient(app) as c:
        yield c


@pytest.fixture
def enabled_client(tmp_path: Path) -> Iterator[TestClient]:
    yield from _client(_make_config(tmp_path, enabled=True))


@pytest.fixture
def disabled_client(tmp_path: Path) -> Iterator[TestClient]:
    yield from _client(_make_config(tmp_path, enabled=False))


# --- session reporting -------------------------------------------------


def test_session_reports_disabled(disabled_client: TestClient):
    body = disabled_client.get("/api/pull/session").json()
    assert body == {
        "enabled": False,
        "configured": False,
        "username": None,
        "last_verified_at": None,
    }


def test_session_reports_enabled_but_unconfigured(enabled_client: TestClient):
    body = enabled_client.get("/api/pull/session").json()
    assert body["enabled"] is True
    assert body["configured"] is False


# --- gating -----------------------------------------------------------


def test_connect_refused_when_disabled(disabled_client: TestClient):
    r = disabled_client.post("/api/pull/connect", json={"cookies": '{"sessionid": "x"}'})
    assert r.status_code == 403
    assert "pull.enabled" in r.json()["detail"]


def test_run_refused_when_disabled(disabled_client: TestClient):
    assert disabled_client.post("/api/pull/run", json={}).status_code == 403


def test_run_refused_when_not_connected(enabled_client: TestClient):
    r = enabled_client.post("/api/pull/run", json={})
    assert r.status_code == 503
    assert "Not connected" in r.json()["detail"]


# --- connect ---------------------------------------------------------


def test_connect_with_valid_cookies(enabled_client: TestClient):
    r = enabled_client.post("/api/pull/connect", json={"cookies": '{"sessionid": "abc"}'})
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["username"] == "tester"

    # ...and it's now reported by GET /session
    assert enabled_client.get("/api/pull/session").json()["configured"] is True


def test_connect_rejects_cookies_without_sessionid(enabled_client: TestClient):
    r = enabled_client.post("/api/pull/connect", json={"cookies": '{"csrftoken": "x"}'})
    assert r.status_code == 400
    assert "sessionid" in r.json()["detail"]


def test_connect_rejects_stale_session(enabled_client: TestClient):
    _FakeLoader.login_user = None
    r = enabled_client.post("/api/pull/connect", json={"cookies": '{"sessionid": "dead"}'})
    assert r.status_code == 400
    assert "stale" in r.json()["detail"]


def test_connect_rejects_empty_body(enabled_client: TestClient):
    assert enabled_client.post("/api/pull/connect", json={"cookies": ""}).status_code == 422


def test_disconnect(enabled_client: TestClient):
    enabled_client.post("/api/pull/connect", json={"cookies": '{"sessionid": "abc"}'})
    r = enabled_client.request("DELETE", "/api/pull/session")
    assert r.status_code == 200
    assert r.json()["configured"] is False


# --- run / conflict / progress -------------------------------------


def test_run_conflicts_with_an_active_pull_job(enabled_client: TestClient, tmp_path: Path):
    config = _make_config(tmp_path, enabled=True)
    enabled_client.post("/api/pull/connect", json={"cookies": '{"sessionid": "abc"}'})

    with session_scope(config) as conn:
        conn.execute("INSERT INTO jobs (kind, status) VALUES (?, 'running')", (JobKind.PULL.value,))

    r = enabled_client.post("/api/pull/run", json={"max_count": 10})
    assert r.status_code == 409


def test_progress_shape_before_any_run(enabled_client: TestClient):
    body = enabled_client.get("/api/pull/progress").json()
    assert body["enabled"] is True
    assert body["job_id"] is None
    assert body["scanned"] == 0
    assert body["new_item_ids"] == []


def test_progress_surfaces_a_finished_job_result(enabled_client: TestClient, tmp_path: Path):
    config = _make_config(tmp_path, enabled=True)
    with session_scope(config) as conn:
        conn.execute(
            "INSERT INTO jobs (kind, status, result_json) VALUES (?, 'done', ?)",
            (JobKind.PULL.value, '{"scanned": 12, "new": 3, "imported": 3, "linked": 2, '
             '"stopped_reason": "completed", "new_item_ids": [5, 6, 7]}'),
        )
    body = enabled_client.get("/api/pull/progress").json()
    assert body["scanned"] == 12
    assert body["new"] == 3
    assert body["linked"] == 2
    assert body["stopped_reason"] == "completed"
    assert body["new_item_ids"] == [5, 6, 7]
    assert body["last_status"] == "done"


def test_progress_surfaces_a_cancelled_job_result(enabled_client: TestClient, tmp_path: Path):
    """A cancelled pull still imported whatever it fetched — show those counts."""
    config = _make_config(tmp_path, enabled=True)
    with session_scope(config) as conn:
        conn.execute(
            "INSERT INTO jobs (kind, status, result_json) VALUES (?, 'cancelled', ?)",
            (JobKind.PULL.value, '{"new": 2, "imported": 2, "stopped_reason": "cancelled"}'),
        )
    body = enabled_client.get("/api/pull/progress").json()
    assert body["last_status"] == "cancelled"
    assert body["imported"] == 2
    assert body["stopped_reason"] == "cancelled"
