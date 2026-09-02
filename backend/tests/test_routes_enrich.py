"""Tests for the `/api/enrich` HTTP surface: triggering enrichment,
aggregate progress, and per-item status — including the friendly-503
behavior when Ollama isn't reachable or the required models aren't pulled.

`gramvault.ai.pipeline.process_items` is mocked at the route-module
boundary so these tests never run the real AI pipeline; `ollama_client`'s
readiness checks are mocked directly to exercise the 503 translation path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from gramvault.ai.ollama_client import ModelNotPulledError, OllamaNotRunningError
from gramvault.api import jobs
from gramvault.config import Config
from gramvault.db.session import session_scope
from gramvault.models.schemas import JobKind


def _seed_item(
    config: Config, *, media_type: str = "photo", enrichment_status: str = "pending"
) -> int:
    with session_scope(config) as conn:
        item_id = conn.execute(
            "INSERT INTO items (media_type, caption, enrichment_status) VALUES (?, ?, ?)",
            (media_type, "a caption", enrichment_status),
        ).lastrowid
    assert item_id is not None
    return item_id


class TestRunEnrichment:
    def test_triggers_pipeline_with_explicit_item_ids(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        item_id = _seed_item(tmp_config)

        with (
            patch(
                "gramvault.ai.ollama_client.ensure_running",
                new_callable=AsyncMock,
            ),
            patch(
                "gramvault.ai.ollama_client.ensure_model_pulled",
                new_callable=AsyncMock,
            ),
            patch(
                "gramvault.api.routes_enrich.pipeline.process_items", new_callable=AsyncMock
            ) as mock_process,
        ):
            response = client.post("/api/enrich/run", json={"item_ids": [item_id]})

        assert response.status_code == 202
        assert response.json()["queued_count"] == 1
        assert response.json()["job_id"] is not None
        mock_process.assert_awaited_once()
        args, _kwargs = mock_process.call_args
        assert args[0] == [item_id]

    def test_none_item_ids_enqueues_all_currently_pending(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        pending_id = _seed_item(tmp_config, enrichment_status="pending")
        _seed_item(tmp_config, enrichment_status="done")

        with (
            patch(
                "gramvault.ai.ollama_client.ensure_running",
                new_callable=AsyncMock,
            ),
            patch(
                "gramvault.ai.ollama_client.ensure_model_pulled",
                new_callable=AsyncMock,
            ),
            patch(
                "gramvault.api.routes_enrich.pipeline.process_items", new_callable=AsyncMock
            ) as mock_process,
        ):
            response = client.post("/api/enrich/run", json={})

        assert response.status_code == 202
        assert response.json()["queued_count"] == 1
        args, _kwargs = mock_process.call_args
        assert args[0] == [pending_id]

    def test_explicit_item_ids_flip_status_to_pending_immediately(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        item_id = _seed_item(tmp_config, enrichment_status="failed")

        with (
            patch(
                "gramvault.ai.ollama_client.ensure_running",
                new_callable=AsyncMock,
            ),
            patch(
                "gramvault.ai.ollama_client.ensure_model_pulled",
                new_callable=AsyncMock,
            ),
            patch("gramvault.api.routes_enrich.pipeline.process_items", new_callable=AsyncMock),
        ):
            client.post("/api/enrich/run", json={"item_ids": [item_id]})

        with session_scope(tmp_config) as conn:
            row = conn.execute(
                "SELECT enrichment_status FROM items WHERE id = ?", (item_id,)
            ).fetchone()
        assert row["enrichment_status"] == "pending"

    def test_no_items_to_process_returns_zero_and_skips_background_task(
        self, client: TestClient
    ) -> None:
        with (
            patch(
                "gramvault.ai.ollama_client.ensure_running",
                new_callable=AsyncMock,
            ),
            patch(
                "gramvault.ai.ollama_client.ensure_model_pulled",
                new_callable=AsyncMock,
            ),
            patch(
                "gramvault.api.routes_enrich.pipeline.process_items", new_callable=AsyncMock
            ) as mock_process,
        ):
            response = client.post("/api/enrich/run", json={})

        assert response.status_code == 202
        assert response.json()["queued_count"] == 0
        mock_process.assert_not_awaited()

    def test_ollama_not_running_is_503_with_friendly_message_not_a_stack_trace(
        self, client: TestClient
    ) -> None:
        with patch(
            "gramvault.ai.ollama_client.ensure_running",
            new_callable=AsyncMock,
            side_effect=OllamaNotRunningError("http://localhost:11434"),
        ):
            response = client.post("/api/enrich/run", json={})

        assert response.status_code == 503
        body = response.json()
        assert "detail" in body
        assert "ollama serve" in body["detail"].lower()

    def test_model_not_pulled_is_503_with_friendly_message(self, client: TestClient) -> None:
        with (
            patch(
                "gramvault.ai.ollama_client.ensure_running",
                new_callable=AsyncMock,
            ),
            patch(
                "gramvault.ai.ollama_client.ensure_model_pulled",
                new_callable=AsyncMock,
                side_effect=ModelNotPulledError("llava:7b"),
            ),
        ):
            response = client.post("/api/enrich/run", json={})

        assert response.status_code == 503
        body = response.json()
        assert "llava:7b" in body["detail"]
        assert "pull" in body["detail"].lower()

    def test_readiness_checks_happen_before_scheduling_background_task(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        _seed_item(tmp_config)

        with (
            patch(
                "gramvault.ai.ollama_client.ensure_running",
                new_callable=AsyncMock,
                side_effect=OllamaNotRunningError("http://localhost:11434"),
            ),
            patch(
                "gramvault.api.routes_enrich.pipeline.process_items", new_callable=AsyncMock
            ) as mock_process,
        ):
            response = client.post("/api/enrich/run", json={})

        assert response.status_code == 503
        mock_process.assert_not_awaited()

    def test_link_only_items_skip_the_vision_model_check(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        # Items with no media files (link-only saved posts) enrich with just
        # the embedding model — mirrors the pipeline's per-item needs_vision
        # narrowing, so a metadata-only library works without llava pulled.
        _seed_item(tmp_config)

        with (
            patch(
                "gramvault.ai.ollama_client.ensure_running",
                new_callable=AsyncMock,
            ),
            patch(
                "gramvault.ai.ollama_client.ensure_model_pulled",
                new_callable=AsyncMock,
            ) as mock_pulled,
            patch("gramvault.api.routes_enrich.pipeline.process_items", new_callable=AsyncMock),
        ):
            response = client.post("/api/enrich/run", json={})

        assert response.status_code == 202
        checked = {call.args[0] for call in mock_pulled.await_args_list}
        assert checked == {"nomic-embed-text"}

    def test_items_with_media_still_require_the_vision_model(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        item_id = _seed_item(tmp_config)
        with session_scope(tmp_config) as conn:
            conn.execute(
                "INSERT INTO media_files (item_id, file_path, media_type) VALUES (?, ?, 'photo')",
                (item_id, "media/ab/abc123.jpg"),
            )

        with (
            patch(
                "gramvault.ai.ollama_client.ensure_running",
                new_callable=AsyncMock,
            ),
            patch(
                "gramvault.ai.ollama_client.ensure_model_pulled",
                new_callable=AsyncMock,
            ) as mock_pulled,
            patch("gramvault.api.routes_enrich.pipeline.process_items", new_callable=AsyncMock),
        ):
            response = client.post("/api/enrich/run", json={})

        assert response.status_code == 202
        checked = {call.args[0] for call in mock_pulled.await_args_list}
        assert checked == {"llava:7b", "nomic-embed-text"}


class TestEnrichmentJob:
    def test_second_run_while_one_is_active_is_409(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        _seed_item(tmp_config)
        # Pretend a previous enrich job is still running.
        with session_scope(tmp_config) as conn:
            jobs.create(conn, JobKind.ENRICH)

        with (
            patch(
                "gramvault.ai.ollama_client.ensure_running",
                new_callable=AsyncMock,
            ),
            patch(
                "gramvault.ai.ollama_client.ensure_model_pulled",
                new_callable=AsyncMock,
            ),
            patch(
                "gramvault.api.routes_enrich.pipeline.process_items", new_callable=AsyncMock
            ) as mock_process,
        ):
            response = client.post("/api/enrich/run", json={})

        assert response.status_code == 409
        mock_process.assert_not_awaited()

    def test_run_creates_a_job_row_and_reports_progress(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        item_id = _seed_item(tmp_config)

        with (
            patch(
                "gramvault.ai.ollama_client.ensure_running",
                new_callable=AsyncMock,
            ),
            patch(
                "gramvault.ai.ollama_client.ensure_model_pulled",
                new_callable=AsyncMock,
            ),
            patch(
                "gramvault.api.routes_enrich.pipeline.process_items", new_callable=AsyncMock
            ) as mock_process,
        ):
            # Drive the real progress callback so the job row records it.
            async def fake_process(ids, cfg, *, progress_cb=None, **_kwargs):
                if progress_cb:
                    progress_cb(len(ids), len(ids))

            mock_process.side_effect = fake_process
            response = client.post("/api/enrich/run", json={"item_ids": [item_id]})

        assert response.status_code == 202
        job_id = response.json()["job_id"]

        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["kind"] == "enrich"
        assert job["status"] == "done"
        assert job["result"] == {"processed": 1, "total": 1}


class TestEnrichmentProgress:
    def test_aggregates_counts_by_status(self, client: TestClient, tmp_config: Config) -> None:
        _seed_item(tmp_config, enrichment_status="pending")
        _seed_item(tmp_config, enrichment_status="pending")
        _seed_item(tmp_config, enrichment_status="running")
        _seed_item(tmp_config, enrichment_status="done")
        _seed_item(tmp_config, enrichment_status="failed")

        response = client.get("/api/enrich/progress")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 5
        assert body["pending"] == 2
        assert body["running"] == 1
        assert body["done"] == 1
        assert body["failed"] == 1

    def test_empty_library_is_all_zero(self, client: TestClient) -> None:
        response = client.get("/api/enrich/progress")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 0
        assert body["pending"] == body["running"] == body["done"] == body["failed"] == 0
        assert body["job_id"] is None
        assert set(body["steps"]) == {"transcribe", "ocr", "vision_caption", "embed"}
        assert all(s == {"done": 0, "pending": 0} for s in body["steps"].values())


class TestScopedEnrichmentRun:
    def test_steps_and_ocr_scope_are_forwarded_to_the_pipeline(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        item_id = _seed_item(tmp_config)
        with session_scope(tmp_config) as conn:
            conn.execute(
                "INSERT INTO media_files (item_id, file_path, media_type) VALUES (?, 's.jpg', 'photo')",
                (item_id,),
            )

        with (
            patch("gramvault.ai.ollama_client.ensure_running", new_callable=AsyncMock),
            patch(
                "gramvault.ai.ollama_client.ensure_model_pulled", new_callable=AsyncMock
            ) as mock_pulled,
            patch(
                "gramvault.api.routes_enrich.pipeline.process_items", new_callable=AsyncMock
            ) as mock_process,
        ):
            response = client.post(
                "/api/enrich/run",
                json={
                    "scope": {"item_ids": [item_id], "only_missing": False},
                    "steps": {
                        "transcribe": False,
                        "ocr": True,
                        "vision_caption": False,
                        "embed": False,
                    },
                    "ocr_scope": "all_media",
                },
            )

        assert response.status_code == 202
        _args, kwargs = mock_process.call_args
        assert kwargs["steps"].ocr is True
        assert kwargs["steps"].embed is False
        assert kwargs["ocr_scope"] == "all_media"
        # embed step off -> embedding model is never checked; vision is (ocr + media).
        checked = {c.args[0] for c in mock_pulled.await_args_list}
        assert "nomic-embed-text" not in checked
        assert "llava:7b" in checked

    def test_only_missing_skips_already_done_items(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        done_id = _seed_item(tmp_config, enrichment_status="done")
        pending_id = _seed_item(tmp_config, enrichment_status="pending")

        with (
            patch("gramvault.ai.ollama_client.ensure_running", new_callable=AsyncMock),
            patch("gramvault.ai.ollama_client.ensure_model_pulled", new_callable=AsyncMock),
            patch(
                "gramvault.api.routes_enrich.pipeline.process_items", new_callable=AsyncMock
            ) as mock_process,
        ):
            response = client.post(
                "/api/enrich/run",
                json={"scope": {"only_missing": True}, "steps": {"ocr": False}},
            )

        assert response.status_code == 202
        assert response.json()["queued_count"] == 1
        assert mock_process.call_args.args[0] == [pending_id]
        assert done_id not in mock_process.call_args.args[0]

    def test_scope_by_category(self, client: TestClient, tmp_config: Config) -> None:
        in_cat = _seed_item(tmp_config, enrichment_status="pending")
        _seed_item(tmp_config, enrichment_status="pending")  # different / no category
        with session_scope(tmp_config) as conn:
            cat_id = conn.execute(
                "SELECT id FROM categories WHERE name = 'recipes'"
            ).fetchone()[0]
            conn.execute("UPDATE items SET category_id = ? WHERE id = ?", (cat_id, in_cat))

        with (
            patch("gramvault.ai.ollama_client.ensure_running", new_callable=AsyncMock),
            patch("gramvault.ai.ollama_client.ensure_model_pulled", new_callable=AsyncMock),
            patch(
                "gramvault.api.routes_enrich.pipeline.process_items", new_callable=AsyncMock
            ) as mock_process,
        ):
            response = client.post(
                "/api/enrich/run",
                json={"scope": {"category": "recipes", "only_missing": False}},
            )

        assert response.status_code == 202
        assert mock_process.call_args.args[0] == [in_cat]


class TestPerStepProgress:
    def test_progress_counts_media_by_pass(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        item_id = _seed_item(tmp_config, enrichment_status="done")
        with session_scope(tmp_config) as conn:
            conn.executescript(
                f"""
                INSERT INTO media_files (item_id, file_path, media_type, transcript, ocr_attempted_at)
                  VALUES ({item_id}, 'a.mp4', 'video', 'hello', '2026-01-01');
                INSERT INTO media_files (item_id, file_path, media_type, transcript, vision_caption)
                  VALUES ({item_id}, 'b.mp4', 'video', NULL, 'a scene');
                """
            )

        body = client.get("/api/enrich/progress").json()
        assert body["steps"]["transcribe"] == {"done": 1, "pending": 1}
        assert body["steps"]["ocr"] == {"done": 1, "pending": 1}
        assert body["steps"]["vision_caption"]["done"] == 1
        assert body["steps"]["embed"] == {"done": 1, "pending": 0}

    def test_failures_endpoint_lists_failed_items(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        failed_id = _seed_item(tmp_config, enrichment_status="failed")
        _seed_item(tmp_config, enrichment_status="done")
        with session_scope(tmp_config) as conn:
            conn.execute(
                "UPDATE items SET raw_metadata_json = ? WHERE id = ?",
                ('{"enrichment_error": "boom"}', failed_id),
            )

        body = client.get("/api/enrich/failures").json()
        assert [it["item_id"] for it in body] == [failed_id]
        assert body[0]["error"] == "boom"


class TestItemEnrichmentStatus:
    def test_returns_item_with_status_and_media_files(
        self, client: TestClient, tmp_config: Config
    ) -> None:
        item_id = _seed_item(tmp_config, enrichment_status="done")
        with session_scope(tmp_config) as conn:
            conn.execute(
                "INSERT INTO media_files (item_id, file_path, media_type, vision_caption) "
                "VALUES (?, 'a.jpg', 'photo', ?)",
                (item_id, "a nice photo"),
            )

        response = client.get(f"/api/enrich/progress/{item_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == item_id
        assert body["enrichment_status"] == "done"
        assert body["media_files"][0]["vision_caption"] == "a nice photo"

    def test_missing_item_is_404(self, client: TestClient) -> None:
        response = client.get("/api/enrich/progress/999999")
        assert response.status_code == 404
