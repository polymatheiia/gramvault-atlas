"""Read-only system/settings info for the Settings page's Security and
Advanced tabs (audit finding UX-9 — Settings restructure).

Everything here is derived from `Config`; none of it is secret (the auth
token itself is never returned — `ModelsOverview.auth_token_set` already
covers "is one configured").
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from gramvault.ai import ollama_client
from gramvault.ai.keyframes import ffmpeg_available
from gramvault.api.deps import get_config_dependency, get_config_path_dependency
from gramvault.config import Config
from gramvault.db.session import get_connection, schema_version as get_schema_version
from gramvault.models.schemas import JobKind, JobStatus

router = APIRouter(prefix="/api/system", tags=["system"])


class SystemInfo(BaseModel):
    server_host: str
    server_port: int
    allowed_hosts: list[str]
    auth_enabled: bool
    auth_disabled_explicitly: bool
    pull_enabled: bool
    transcription_model_size: str
    transcription_device: str
    transcription_compute_type: str
    # Chroma's anonymised telemetry is disabled unconditionally at
    # construction time (Phase 0.4 / audit finding S7) — reported here so
    # the Advanced tab can state it rather than leave it undocumented.
    chroma_telemetry_disabled: bool
    config_path: str
    library_dir: str
    db_path: str
    chroma_dir: str
    schema_version: int | None = None


@router.get("/info", response_model=SystemInfo)
def system_info(
    config: Config = Depends(get_config_dependency),
    config_path=Depends(get_config_path_dependency),
) -> SystemInfo:
    return SystemInfo(
        server_host=config.server.host,
        server_port=config.server.port,
        allowed_hosts=list(config.server.allowed_hosts),
        auth_enabled=bool(config.auth.token) and not config.auth.disabled,
        auth_disabled_explicitly=config.auth.disabled,
        pull_enabled=config.pull.enabled,
        transcription_model_size=config.transcription.model_size,
        transcription_device=config.transcription.device,
        transcription_compute_type=config.transcription.compute_type,
        chroma_telemetry_disabled=True,
        config_path=str(config_path),
        library_dir=str(config.resolved_library_dir),
        db_path=str(config.resolved_db_path),
        chroma_dir=str(config.resolved_chroma_dir),
    )


# --- health (UX-2 — the Home page's onboarding/health panel) --------------


class HealthResponse(BaseModel):
    status: str = "ok"
    ollama_reachable: bool
    item_count: int
    enriched_count: int
    categorized_count: int
    needs_review_count: int
    last_pull_at: str | None = None
    schema_version: int
    ffmpeg_found: bool
    disk_free_bytes: int | None = None


def _disk_free_bytes(path: Path) -> int | None:
    for candidate in (path, path.parent, Path.cwd()):
        try:
            return shutil.disk_usage(candidate).free
        except OSError:
            continue
    return None


async def build_health_response(config: Config) -> HealthResponse:
    ollama_reachable = await ollama_client.check_health(config)
    conn = get_connection(config)
    try:
        item_count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        enriched_count = conn.execute(
            "SELECT COUNT(*) FROM items WHERE enrichment_status = 'done'"
        ).fetchone()[0]
        categorized_count = conn.execute(
            "SELECT COUNT(*) FROM items WHERE category_id IS NOT NULL"
        ).fetchone()[0]
        needs_review_count = conn.execute(
            "SELECT COUNT(*) FROM items WHERE category_id IS NOT NULL "
            "AND COALESCE(category_source, '') != 'manual' "
            "AND COALESCE(category_confidence, 0) < 0.6"
        ).fetchone()[0]
        last_pull_row = conn.execute(
            "SELECT finished_at FROM jobs WHERE kind = ? AND status = ? "
            "ORDER BY id DESC LIMIT 1",
            (JobKind.PULL.value, JobStatus.DONE.value),
        ).fetchone()
        sv = get_schema_version(conn)
    finally:
        conn.close()

    return HealthResponse(
        ollama_reachable=ollama_reachable,
        item_count=item_count,
        enriched_count=enriched_count,
        categorized_count=categorized_count,
        needs_review_count=needs_review_count,
        last_pull_at=last_pull_row["finished_at"] if last_pull_row else None,
        schema_version=sv,
        ffmpeg_found=ffmpeg_available(),
        disk_free_bytes=_disk_free_bytes(config.resolved_library_dir),
    )
