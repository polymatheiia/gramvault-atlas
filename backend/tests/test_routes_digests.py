"""Tests for the `/api/digests` HTTP surface — templates list, preflight,
create (+ job run with the provider mocked), history, single fetch, and
download."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from gramvault.ai.errors import ProviderNotReadyError
from gramvault.ai.providers.base import ChatResult
from gramvault.api import jobs
from gramvault.config import Config
from gramvault.db.session import session_scope
from gramvault.models.schemas import JobKind, JobStatus


def _seed(config: Config, caption: str, *, category: str = "books/manga") -> int:
    with session_scope(config) as conn:
        author_id = conn.execute(
            "INSERT INTO authors (username) VALUES ('acc') "
            "ON CONFLICT(username) DO UPDATE SET username = 'acc' RETURNING id"
        ).fetchone()["id"]
        category_id = conn.execute(
            "SELECT id FROM categories WHERE name = ?", (category,)
        ).fetchone()["id"]
        item_id = conn.execute(
            "INSERT INTO items (media_type, caption, author_id, category_id, permalink) "
            "VALUES ('reel', ?, ?, ?, 'https://instagram.com/p/abc/')",
            (caption, author_id, category_id),
        ).lastrowid
    assert item_id is not None
    return item_id


class TestTemplates:
    def test_lists_bundled_templates(self, client: TestClient) -> None:
        body = client.get("/api/digests/templates").json()
        names = {t["name"] for t in body}
        assert {"book-titles", "advice-digest", "link-list"} <= names
        assert all(t["extract_prompt"] and t["reduce_prompt"] for t in body)


class TestPreflight:
    def test_estimates_a_category_selection(self, client: TestClient, tmp_config: Config) -> None:
        _seed(tmp_config, "read Dune")
        _seed(tmp_config, "read Hyperion")

        res = client.post(
            "/api/digests/preflight",
            json={"template": "book-titles", "category": "books/manga"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["item_count"] == 2
        assert body["batches"] >= 1
        assert body["estimated_tokens_in"] > 0

    def test_unknown_template_422(self, client: TestClient) -> None:
        res = client.post("/api/digests/preflight", json={"template": "nope", "category": "beauty"})
        assert res.status_code == 422


class TestCreate:
    def test_empty_selection_422(self, client: TestClient, tmp_config: Config) -> None:
        res = client.post(
            "/api/digests", json={"template": "book-titles", "category": "books/manga"}
        )
        assert res.status_code == 422

    def test_provider_not_ready_503(self, client: TestClient, tmp_config: Config) -> None:
        _seed(tmp_config, "read Dune")
        provider = AsyncMock()
        provider.ensure_ready.side_effect = ProviderNotReadyError("Ollama is not running")
        with patch(
            "gramvault.api.routes_digests.get_provider", return_value=(provider, "m")
        ):
            res = client.post(
                "/api/digests", json={"template": "book-titles", "category": "books/manga"}
            )
        assert res.status_code == 503
        assert "Ollama is not running" in res.json()["detail"]

    def test_conflict_with_active_digest_job(self, client: TestClient, tmp_config: Config) -> None:
        _seed(tmp_config, "read Dune")
        with session_scope(tmp_config) as conn:
            jobs.create(conn, JobKind.DIGEST, params={})
        with patch(
            "gramvault.api.routes_digests.get_provider",
            return_value=(AsyncMock(), "m"),
        ):
            res = client.post(
                "/api/digests", json={"template": "book-titles", "category": "books/manga"}
            )
        assert res.status_code == 409

    def test_conflict_with_an_active_heavy_job_of_another_kind(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        _seed(tmp_config, "read Dune")
        with session_scope(tmp_config) as conn:
            jobs.create(conn, JobKind.ENRICH, params={})
        with patch(
            "gramvault.api.routes_digests.get_provider",
            return_value=(AsyncMock(), "m"),
        ):
            res = client.post(
                "/api/digests", json={"template": "book-titles", "category": "books/manga"}
            )
        assert res.status_code == 409
        assert "enrich" in res.json()["detail"]

    def test_create_runs_job_and_writes_markdown(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        i1 = _seed(tmp_config, "read Dune by Herbert")

        provider = AsyncMock()
        provider.name = "ollama"
        provider.complete.side_effect = [
            ChatResult(text=json.dumps([{"title": "Dune", "author": "Herbert", "item_id": i1}])),
            ChatResult(text=f"## Sci-fi\n- **Dune** — Herbert ([reel](https://x)) [[item:{i1}]]\n"),
        ]

        with patch(
            "gramvault.api.routes_digests.get_provider", return_value=(provider, "fake-model")
        ), patch("gramvault.ai.digest.get_provider", return_value=(provider, "fake-model")):
            res = client.post(
                "/api/digests",
                json={"template": "book-titles", "category": "books/manga", "name": "My books"},
            )
            assert res.status_code == 202
            body = res.json()
            assert body["item_count"] == 1

            got = client.get(f"/api/digests/{body['digest_id']}").json()

        assert got["status"] == "done"
        assert "Dune" in got["markdown"]
        assert "instagram.com/p/abc" in got["markdown"]  # link rewritten from the DB
        assert got["name"] == "My books"

        with session_scope(tmp_config) as conn:
            job = jobs.get(conn, body["job_id"])
        assert job.status == JobStatus.DONE

    def test_export_to_vault(self, client: TestClient, tmp_config: Config, tmp_path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        tmp_config.paths.obsidian_vault_dir = str(vault)

        i1 = _seed(tmp_config, "read Dune")
        with session_scope(tmp_config) as conn:
            digest_id = conn.execute(
                "INSERT INTO digests (name, template, status, selection_json, item_ids_json, "
                "markdown) VALUES ('Books', 'book-titles', 'done', '{}', ?, ?)",
                (json.dumps([i1]), f"- **Dune** [[item:{i1}]]\n"),
            ).lastrowid

        res = client.post(f"/api/digests/{digest_id}/export")
        assert res.status_code == 200
        path = res.json()["path"]
        assert path.endswith("GramVault/_digests/Books.md")
        text = (vault / "GramVault" / "_digests" / "Books.md").read_text()
        assert "[[item:" not in text  # rewritten to the item note link

    def test_export_without_vault_400(self, client: TestClient, tmp_config: Config) -> None:
        with session_scope(tmp_config) as conn:
            digest_id = conn.execute(
                "INSERT INTO digests (name, template, status, selection_json, item_ids_json, "
                "markdown) VALUES ('d', 'book-titles', 'done', '{}', '[]', '# x\n')"
            ).lastrowid
        res = client.post(f"/api/digests/{digest_id}/export")
        assert res.status_code == 400

    def test_history_omits_markdown_and_download_serves_it(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        with session_scope(tmp_config) as conn:
            conn.execute(
                "INSERT INTO digests (name, template, status, selection_json, item_ids_json, "
                "markdown) VALUES ('done one', 'book-titles', 'done', '{}', '[]', '# hi\n')"
            )

        listing = client.get("/api/digests").json()
        assert listing[0]["name"] == "done one"
        assert listing[0]["markdown"] is None

        digest_id = listing[0]["id"]
        dl = client.get(f"/api/digests/{digest_id}/download")
        assert dl.status_code == 200
        assert dl.text == "# hi\n"
        assert "attachment" in dl.headers["content-disposition"]
        assert "done-one.md" in dl.headers["content-disposition"]
