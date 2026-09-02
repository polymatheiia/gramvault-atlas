"""Tests for the `/api/categorize` HTTP surface and the
`/api/library/items?needs_review=1` review-queue filter.

`gramvault.ai.classifier.categorize_items` runs for real against the tmp
DB with the keyword method (no model needed); the LLM path is only
exercised for its friendly-503 and 409-conflict behaviour, with the
provider mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from gramvault.ai.errors import ProviderNotReadyError
from gramvault.api import jobs
from gramvault.config import Config
from gramvault.db.session import session_scope
from gramvault.models.schemas import JobKind, JobStatus


def _seed_item(
    config: Config,
    caption: str,
    *,
    category: str | None = None,
    source: str | None = None,
    confidence: float | None = None,
) -> int:
    with session_scope(config) as conn:
        category_id = None
        if category is not None:
            category_id = conn.execute(
                "SELECT id FROM categories WHERE name = ?", (category,)
            ).fetchone()["id"]
        item_id = conn.execute(
            "INSERT INTO items (media_type, caption, category_id, category_source, "
            "category_confidence) VALUES ('reel', ?, ?, ?, ?)",
            (caption, category_id, source, confidence),
        ).lastrowid
    assert item_id is not None
    return item_id


class TestRunCategorize:
    def test_keyword_run_labels_uncategorized_items(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        gym = _seed_item(tmp_config, "gym workout, squats and deadlifts, leg day")
        _seed_item(tmp_config, "recipe: preheat the oven, bake the dough 20 min")

        response = client.post(
            "/api/categorize/run", json={"method": "keyword", "scope": "uncategorized"}
        )

        assert response.status_code == 202
        body = response.json()
        assert body["queued_count"] == 2
        assert body["job_id"] is not None

        with session_scope(tmp_config) as conn:
            name = conn.execute(
                "SELECT categories.name FROM items JOIN categories "
                "ON categories.id = items.category_id WHERE items.id = ?",
                (gym,),
            ).fetchone()["name"]
            job = jobs.get(conn, body["job_id"])
        assert name == "workouts"
        assert job.status == JobStatus.DONE
        assert job.result["keyword"] == 2

    def test_explicit_item_ids_scope(self, client: TestClient, tmp_config: Config) -> None:
        target = _seed_item(tmp_config, "gym workout leg day squats")
        other = _seed_item(tmp_config, "recipe preheat oven bake")

        response = client.post(
            "/api/categorize/run",
            json={"method": "keyword", "scope": {"item_ids": [target]}},
        )

        assert response.json()["queued_count"] == 1
        with session_scope(tmp_config) as conn:
            rows = {
                r["id"]: r["category_id"]
                for r in conn.execute("SELECT id, category_id FROM items")
            }
        assert rows[target] is not None
        assert rows[other] is None

    def test_empty_scope_returns_zero_and_no_job(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        response = client.post("/api/categorize/run", json={"method": "keyword"})
        assert response.status_code == 202
        assert response.json() == {"queued_count": 0, "job_id": None}
        with session_scope(tmp_config) as conn:
            assert jobs.list_jobs(conn, kind=JobKind.CATEGORIZE) == []

    def test_llm_method_returns_503_when_provider_not_ready(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        _seed_item(tmp_config, "some caption")

        fake_provider = AsyncMock()
        fake_provider.ensure_ready.side_effect = ProviderNotReadyError("Ollama is not running")
        with patch(
            "gramvault.api.routes_categorize.get_provider",
            return_value=(fake_provider, "llama3.2:3b"),
        ):
            response = client.post("/api/categorize/run", json={"method": "llm", "scope": "all"})

        assert response.status_code == 503
        assert "Ollama is not running" in response.json()["detail"]

    def test_conflicts_with_an_active_categorize_job(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        _seed_item(tmp_config, "gym workout leg day")
        with session_scope(tmp_config) as conn:
            jobs.create(conn, JobKind.CATEGORIZE, params={"count": 1})

        response = client.post("/api/categorize/run", json={"method": "keyword", "scope": "all"})

        assert response.status_code == 409


class TestCategorizeProgress:
    def test_reports_counts(self, client: TestClient, tmp_config: Config) -> None:
        _seed_item(tmp_config, "uncategorised one")
        _seed_item(tmp_config, "solid", category="workouts", source="keyword", confidence=0.9)
        _seed_item(tmp_config, "weak", category="other", source="keyword", confidence=0.2)

        body = client.get("/api/categorize/progress").json()

        assert body["total"] == 3
        assert body["uncategorized"] == 1
        assert body["categorized"] == 2
        assert body["needs_review"] == 1
        assert body["by_source"]["keyword"] == 2


class TestReviewQueueFilter:
    def test_needs_review_filter_on_library_items(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        weak = _seed_item(
            tmp_config, "weak label", category="other", source="keyword", confidence=0.2
        )
        _seed_item(tmp_config, "confident", category="workouts", source="keyword", confidence=0.95)
        _seed_item(tmp_config, "manual pick", category="beauty", source="manual", confidence=1.0)
        _seed_item(tmp_config, "not categorised at all")

        body = client.get("/api/library/items", params={"needs_review": "true"}).json()

        assert [item["id"] for item in body["items"]] == [weak]
