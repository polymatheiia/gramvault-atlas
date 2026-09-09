"""`/api/jobs` HTTP surface: list, get, cancel."""

from __future__ import annotations

from fastapi.testclient import TestClient

from gramvault.api import jobs
from gramvault.config import Config
from gramvault.db.session import session_scope
from gramvault.models.schemas import JobKind


def _make(config: Config, kind: JobKind = JobKind.ENRICH, status: str = "pending") -> int:
    with session_scope(config) as conn:
        job = jobs.create(conn, kind, params={"x": 1})
        if status != "pending":
            conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job.id))
    assert job.id is not None
    return job.id


class TestList:
    def test_lists_newest_first_with_filters(self, client: TestClient, tmp_config: Config) -> None:
        a = _make(tmp_config, JobKind.ENRICH, "done")
        b = _make(tmp_config, JobKind.ENRICH, "pending")
        _make(tmp_config, JobKind.PULL, "pending")

        res = client.get("/api/jobs", params={"kind": "enrich"})
        assert res.status_code == 200
        assert [j["id"] for j in res.json()] == [b, a]

        res = client.get("/api/jobs", params={"status": "done"})
        assert [j["id"] for j in res.json()] == [a]

    def test_empty(self, client: TestClient) -> None:
        assert client.get("/api/jobs").json() == []


class TestGet:
    def test_returns_parsed_json_fields(self, client: TestClient, tmp_config: Config) -> None:
        job_id = _make(tmp_config)
        body = client.get(f"/api/jobs/{job_id}").json()
        assert body["id"] == job_id
        assert body["kind"] == "enrich"
        assert body["params"] == {"x": 1}
        assert body["progress"] is None

    def test_missing_is_404(self, client: TestClient) -> None:
        assert client.get("/api/jobs/999999").status_code == 404


class TestCancel:
    def test_flags_an_active_job(self, client: TestClient, tmp_config: Config) -> None:
        job_id = _make(tmp_config, status="running")
        res = client.post(f"/api/jobs/{job_id}/cancel")
        assert res.status_code == 200
        assert res.json()["cancel_requested"] is True
        with session_scope(tmp_config) as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        assert row["cancel_requested"] == 1

    def test_noop_on_finished_job(self, client: TestClient, tmp_config: Config) -> None:
        job_id = _make(tmp_config, status="done")
        res = client.post(f"/api/jobs/{job_id}/cancel")
        assert res.status_code == 200
        assert res.json()["cancel_requested"] is False

    def test_missing_is_404(self, client: TestClient) -> None:
        assert client.post("/api/jobs/999999/cancel").status_code == 404
