"""Read-only system/settings info for the Settings page's Security and
Advanced tabs (audit finding UX-9 — Settings restructure).

Everything here is derived from `Config`; none of it is secret (the auth
token itself is never returned — `ModelsOverview.auth_token_set` already
covers "is one configured").
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from gramvault.api.deps import get_config_dependency, get_config_path_dependency
from gramvault.config import Config

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
