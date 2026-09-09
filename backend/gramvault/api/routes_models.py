"""Models & providers settings API (the Settings -> Models tab).

Read: what Ollama has pulled, which providers are configured, which model
each AI task routes to. Write: pull/delete Ollama models (as a background
job), point a task at a provider/model, store an API key or the auth
token (write-only), test a task end to end, and re-embed the library
after an embedding-model change.

Provider routing + credentials are persisted to `secrets.yaml` (deep-
merged over config.yaml at load) via `config.update_secrets`, so
config.yaml stays hand-authored.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, model_validator

from gramvault.ai import embedding_store, ollama_client
from gramvault.ai.errors import ProviderError
from gramvault.ai.providers import get_provider, list_ollama_models
from gramvault.api import jobs
from gramvault.api.deps import get_config_dependency, get_config_path_dependency
from gramvault.chat.retrieval import fetch_items
from gramvault.config import AI_TASKS, Config, ProviderKind, update_secrets
from gramvault.db.session import session_scope
from gramvault.models.schemas import JobKind

router = APIRouter(prefix="/api/models", tags=["models"])


# --- response / request shapes -------------------------------------------


class OllamaModelInfo(BaseModel):
    name: str
    size: int | None = None
    modified_at: str | None = None


class ProviderInfo(BaseModel):
    name: str
    kind: str
    base_url: str | None = None
    api_key_set: bool = False


class TaskRouting(BaseModel):
    provider: str
    model: str
    source: Literal["configured", "default"]


class ModelsOverview(BaseModel):
    ollama_reachable: bool
    ollama_models: list[OllamaModelInfo]
    providers: list[ProviderInfo]
    tasks: dict[str, TaskRouting]
    auth_token_set: bool


class SuggestedModel(BaseModel):
    tasks: list[str]
    provider_kind: ProviderKind
    model: str
    size: str
    note: str


class ModelPullRequest(BaseModel):
    model: str


class JobStartResponse(BaseModel):
    job_id: int


class TaskRoutingUpdate(BaseModel):
    task: str
    provider: str
    model: str
    # If given, (re)register the provider in secrets.yaml as well.
    provider_kind: ProviderKind | None = None
    base_url: str | None = None

    @model_validator(mode="after")
    def _valid_task(self) -> TaskRoutingUpdate:
        if self.task not in AI_TASKS:
            raise ValueError(f"task must be one of {AI_TASKS}")
        return self


class TaskRoutingUpdateResponse(ModelsOverview):
    # True when the embedding task's provider/model changed — the library
    # needs a re-embed (POST /api/models/reembed) before search is correct.
    needs_reembed: bool = False


class SecretUpdate(BaseModel):
    # Either set a provider's API key...
    provider: str | None = None
    api_key: str | None = None  # None clears it
    # ...or set the API auth token (exactly one of the two).
    auth_token: str | None = None
    set_auth_token: bool = False  # discriminates "clear the token" from "not touching it"

    @model_validator(mode="after")
    def _one_target(self) -> SecretUpdate:
        touches_provider = self.provider is not None
        touches_auth = self.set_auth_token
        if touches_provider == touches_auth:
            raise ValueError("set exactly one of {provider} or {set_auth_token}")
        return self


class TestTaskRequest(BaseModel):
    task: str


class TestTaskResult(BaseModel):
    ok: bool
    detail: str
    latency_ms: int | None = None


SUGGESTED: list[SuggestedModel] = [
    SuggestedModel(
        tasks=["embedding"], provider_kind="ollama", model="bge-m3", size="1.2 GB",
        note="Multilingual. The current default — changing it re-embeds the library.",
    ),
    SuggestedModel(
        tasks=["embedding"], provider_kind="ollama", model="nomic-embed-text", size="274 MB",
        note="English-centric but tiny.",
    ),
    SuggestedModel(
        tasks=["vision"], provider_kind="ollama", model="minicpm-v", size="5.5 GB",
        note="On-screen text only (Latin script), ~minutes per image on CPU.",
    ),
    SuggestedModel(
        tasks=["vision"], provider_kind="ollama", model="moondream", size="1.7 GB",
        note="Fast, weak on small text.",
    ),
    SuggestedModel(
        tasks=["chat", "categorize", "digest"], provider_kind="ollama", model="qwen2.5:3b",
        size="1.9 GB", note="Good JSON compliance; a solid local default on a 7 GB box.",
    ),
    SuggestedModel(
        tasks=["chat"], provider_kind="ollama", model="llama3.1:8b", size="4.9 GB",
        note="Tight on 7 GB — swap-heavy.",
    ),
    SuggestedModel(
        tasks=["chat", "categorize", "digest"], provider_kind="anthropic",
        model="claude-haiku-4-5-20251001", size="API", note="Cheap, fast; great for categorising.",
    ),
    SuggestedModel(
        tasks=["chat", "vision", "digest"], provider_kind="anthropic", model="claude-sonnet-5",
        size="API", note="Strong at digests and at OCR/vision the local models can't do.",
    ),
]


# --- helpers -------------------------------------------------------------


async def _build_overview(config: Config) -> ModelsOverview:
    ollama_models = await list_ollama_models(config)
    tasks: dict[str, TaskRouting] = {}
    for task in AI_TASKS:
        pc, model = config.resolve_task(task)
        configured = getattr(config.ai, task) is not None
        provider_name = getattr(config.ai, task).provider if configured else "ollama"
        tasks[task] = TaskRouting(
            provider=provider_name,
            model=model,
            source="configured" if configured else "default",
        )

    providers = [
        ProviderInfo(
            name="ollama", kind="ollama", base_url=config.ollama.host, api_key_set=False
        )
    ]
    for name, pc in config.providers.items():
        if name == "ollama":
            providers[0] = ProviderInfo(
                name="ollama", kind="ollama",
                base_url=pc.base_url or config.ollama.host, api_key_set=False,
            )
            continue
        providers.append(
            ProviderInfo(
                name=name, kind=pc.kind, base_url=pc.base_url, api_key_set=bool(pc.api_key)
            )
        )

    return ModelsOverview(
        ollama_reachable=bool(ollama_models) or await ollama_client.check_health(config),
        ollama_models=[
            OllamaModelInfo(
                name=m.get("name", ""), size=m.get("size"), modified_at=m.get("modified_at")
            )
            for m in ollama_models
        ],
        providers=providers,
        tasks=tasks,
        auth_token_set=bool(config.auth.token),
    )


# --- endpoints ---------------------------------------------------------


@router.get("", response_model=ModelsOverview)
async def get_models(config: Config = Depends(get_config_dependency)) -> ModelsOverview:
    return await _build_overview(config)


@router.get("/suggested", response_model=list[SuggestedModel])
async def get_suggested() -> list[SuggestedModel]:
    return SUGGESTED


@router.post("/pull", response_model=JobStartResponse, status_code=202)
async def pull_model(
    body: ModelPullRequest,
    background_tasks: BackgroundTasks,
    config: Config = Depends(get_config_dependency),
) -> JobStartResponse:
    """Pull an Ollama model, tracked as a `model_pull` job — poll
    `GET /api/jobs/{job_id}` for `progress` ({status, completed, total})."""
    model = body.model.strip()
    if not model:
        raise HTTPException(status_code=422, detail="model must not be empty")

    with session_scope(config) as conn:
        try:
            job = jobs.create(conn, JobKind.MODEL_PULL, params={"model": model})
        except jobs.JobConflict as exc:
            raise HTTPException(status_code=409, detail="A model pull is already running") from exc
    assert job.id is not None

    async def _work(ctx: jobs.JobContext) -> dict:
        last: dict = {}
        async for event in ollama_client.pull_model(model, config):
            last = event
            ctx.progress(
                status=event.get("status"),
                completed=event.get("completed"),
                total=event.get("total"),
            )
            if ctx.cancelled:
                break
        return {"model": model, "status": last.get("status", "done")}

    background_tasks.add_task(jobs.run, job.id, _work, config)
    return JobStartResponse(job_id=job.id)


@router.delete("/{name:path}", status_code=204)
async def delete_model(
    name: str, config: Config = Depends(get_config_dependency)
) -> None:
    try:
        await ollama_client.delete_model(name, config)
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.put("/tasks", response_model=TaskRoutingUpdateResponse)
async def set_task_routing(
    body: TaskRoutingUpdate,
    config: Config = Depends(get_config_dependency),
    config_path: Path = Depends(get_config_path_dependency),
) -> TaskRoutingUpdateResponse:
    """Point an AI task at a provider/model. If `provider_kind` is given,
    also (re)registers that provider. Persisted to secrets.yaml."""
    before_pc, before_model = config.resolve_task(body.task)

    patch: dict = {"ai": {body.task: {"provider": body.provider, "model": body.model}}}
    if body.provider_kind is not None:
        patch.setdefault("providers", {})[body.provider] = {
            "kind": body.provider_kind,
            "base_url": body.base_url,
        }
    new_config = update_secrets(patch, config_path)

    after_pc, after_model = new_config.resolve_task(body.task)
    needs_reembed = body.task == "embedding" and (
        after_model != before_model or after_pc.kind != before_pc.kind
    )

    overview = await _build_overview(new_config)
    return TaskRoutingUpdateResponse(**overview.model_dump(), needs_reembed=needs_reembed)


@router.put("/secrets", status_code=204)
async def set_secret(
    body: SecretUpdate,
    config_path: Path = Depends(get_config_path_dependency),
) -> None:
    """Write-only: store a provider API key, or the API auth token. Never
    returns the stored value."""
    if body.set_auth_token:
        update_secrets({"auth": {"token": body.auth_token}}, config_path)
    else:
        assert body.provider is not None
        update_secrets(
            {"providers": {body.provider: {"api_key": body.api_key}}}, config_path
        )


@router.post("/test", response_model=TestTaskResult)
async def test_task(
    body: TestTaskRequest, config: Config = Depends(get_config_dependency)
) -> TestTaskResult:
    """Run one tiny real request for a task's provider/model."""
    if body.task not in AI_TASKS:
        raise HTTPException(status_code=422, detail=f"task must be one of {AI_TASKS}")
    provider, model = get_provider(body.task, config)
    start = time.monotonic()
    try:
        await provider.ensure_ready(model)
        if body.task == "embedding":
            vector = await provider.embed(model, "connection test")
            detail = f"OK — embedding returned {len(vector)} dimensions"
        elif body.task == "vision":
            detail = "OK — provider reachable (vision not exercised, needs an image)"
        else:
            reply = await provider.chat(
                model, [{"role": "user", "content": "Reply with just: OK"}]
            )
            detail = f"OK — model replied {reply.strip()[:60]!r}"
        ok = True
    except ProviderError as exc:
        ok, detail = False, str(exc)
    return TestTaskResult(
        ok=ok, detail=detail, latency_ms=int((time.monotonic() - start) * 1000)
    )


@router.post("/reembed", response_model=JobStartResponse, status_code=202)
async def reembed_library(
    background_tasks: BackgroundTasks,
    config: Config = Depends(get_config_dependency),
) -> JobStartResponse:
    """Drop the vector store and re-embed every enriched item with the
    current embedding provider/model. Run this after changing the
    embedding model."""
    from gramvault.ai import pipeline

    with session_scope(config) as conn:
        try:
            job = jobs.create(conn, JobKind.REEMBED, params={})
        except jobs.JobConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        item_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM items WHERE enrichment_status = 'done' ORDER BY id"
            )
        ]
    assert job.id is not None

    async def _work(ctx: jobs.JobContext) -> dict:
        embedding_store.reset_collection(config)
        done = 0
        for index, item_id in enumerate(item_ids):
            if ctx.cancelled:
                break
            with session_scope(config) as conn:
                item = fetch_items(conn, [item_id]).get(item_id)
            if item is not None:
                await pipeline._embed_and_upsert(config, item)
                done = index + 1
            ctx.progress(done=done, total=len(item_ids))
        return {"reembedded": done, "total": len(item_ids)}

    background_tasks.add_task(jobs.run, job.id, _work, config)
    return JobStartResponse(job_id=job.id)
