"""Shared Pydantic models for GramVault.

This is the contract other agents build on top of:
  - Agent A2 (ingestion) produces Author / Item / MediaFile / ImportJob rows.
  - Agent A3 (AI pipeline) fills in MediaFile.transcript / vision_caption and
    flips Item.enrichment_status, and writes embeddings to ChromaDB keyed by
    item/media_file id (collection layout is A3's call).
  - Agent A4 (chat/search) produces ChatSession / ChatMessage / ChatCitation.
  - Agent A5 (frontend) consumes all of these as JSON over the API in
    backend/gramvault/api/.
  - Agent A6 (Obsidian exporter) reads Item/MediaFile/Tag to render notes.

Keep field additions backwards compatible (add optional fields with
defaults) so agents working in parallel don't break each other.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MediaType(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"
    REEL = "reel"
    CAROUSEL = "carousel"


class FileMediaType(StrEnum):
    """Media type for an individual file on disk (a carousel Item is made
    of several PHOTO/VIDEO MediaFile rows)."""

    PHOTO = "photo"
    VIDEO = "video"


class JobStatus(StrEnum):
    """Shared status enum for long-running jobs (import, export, and the
    `jobs` table). `CANCELLED` only applies to `jobs`-table jobs."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobKind(StrEnum):
    """Kinds of background job tracked in the `jobs` table (the Pipeline
    UI). `import` / `export` predate this and keep their own tables."""

    ENRICH = "enrich"
    CATEGORIZE = "categorize"
    DIGEST = "digest"
    PULL = "pull"
    MODEL_PULL = "model_pull"
    REEMBED = "reembed"


class EnrichmentStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class TagKind(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"
    HASHTAG = "hashtag"


class ChatRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ORMBase(BaseModel):
    """Base class enabling `model_validate` from sqlite3.Row / ORM objects
    via attribute access."""

    model_config = ConfigDict(from_attributes=True)


class Author(ORMBase):
    id: int | None = None
    username: str
    full_name: str | None = None
    profile_url: str | None = None
    avatar_path: str | None = None
    created_at: datetime | None = None


class Tag(ORMBase):
    id: int | None = None
    name: str
    kind: TagKind = TagKind.AUTO


class CategorySource(StrEnum):
    KEYWORD = "keyword"
    LLM = "llm"
    MANUAL = "manual"


class Category(ORMBase):
    id: int | None = None
    name: str
    sort_order: int = 0
    color: str | None = None
    description: str | None = None


class CategoryWithCount(Category):
    count: int = 0


class MediaFile(ORMBase):
    id: int | None = None
    item_id: int
    file_path: str
    media_type: FileMediaType
    sequence_index: int = 0
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    # TODO(A3): populated by faster-whisper for videos/reels.
    transcript: str | None = None
    # TODO(A3): populated by the llava vision model.
    vision_caption: str | None = None
    checksum: str | None = None
    # On-screen text read by `gramvault.ai.ocr` (migration 003). `ocr_text`
    # is None both before an attempt and after a discarded one — the two are
    # told apart by `ocr_attempted_at`, which is set either way so the OCR
    # queue (`ocr_attempted_at IS NULL`) doesn't re-run unreadable frames.
    ocr_text: str | None = None
    ocr_attempted_at: str | None = None
    ocr_model: str | None = None
    vision_model: str | None = None
    transcript_model: str | None = None


class Item(ORMBase):
    id: int | None = None
    external_id: str | None = None
    author: Author | None = None
    media_type: MediaType
    caption: str | None = None
    permalink: str | None = None
    taken_at: datetime | None = None
    imported_at: datetime | None = None
    import_job_id: int | None = None
    enrichment_status: EnrichmentStatus = EnrichmentStatus.PENDING
    category_id: int | None = None
    category: str | None = None  # resolved category name, for display
    category_source: CategorySource | None = None
    category_confidence: float | None = None
    category_reason: str | None = None  # why the classifier chose it (review queue)
    tags: list[Tag] = Field(default_factory=list)
    media_files: list[MediaFile] = Field(default_factory=list)


class ImportJob(ORMBase):
    id: int | None = None
    source_path: str
    status: JobStatus = JobStatus.PENDING
    total_items: int = 0
    processed_items: int = 0
    failed_items: int = 0
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None

    @property
    def progress_pct(self) -> float:
        """0-100 progress based on processed_items/total_items. Safe when
        total_items is 0 (job not yet sized) -> returns 0.0."""
        if self.total_items <= 0:
            return 0.0
        return round(100 * self.processed_items / self.total_items, 1)


class Job(ORMBase):
    """A row in the `jobs` table. The `*_json` TEXT columns are surfaced
    as parsed objects (`params` / `progress` / `result`); build one from a
    `sqlite3.Row` with `Job.from_row(row)`, not `model_validate`."""

    id: int | None = None
    kind: JobKind
    status: JobStatus = JobStatus.PENDING
    params: dict[str, Any] | None = None
    progress: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error_message: str | None = None
    cancel_requested: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Any) -> Job:
        data = dict(row)

        def _loads(key: str) -> dict[str, Any] | None:
            raw = data.get(key)
            if not raw:
                return None
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return None
            return parsed if isinstance(parsed, dict) else None

        return cls(
            id=data["id"],
            kind=data["kind"],
            status=data["status"],
            params=_loads("params_json"),
            progress=_loads("progress_json"),
            result=_loads("result_json"),
            error_message=data.get("error_message"),
            cancel_requested=bool(data.get("cancel_requested")),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            created_at=data.get("created_at"),
        )


class Digest(ORMBase):
    """A row in the `digests` table. `selection` / `item_ids` are surfaced
    as parsed objects; build one from a `sqlite3.Row` with
    `Digest.from_row(row)`, not `model_validate`."""

    id: int | None = None
    name: str
    template: str
    template_version: str | None = None
    status: JobStatus = JobStatus.PENDING
    selection: dict[str, Any] = Field(default_factory=dict)
    item_ids: list[int] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_estimate: float | None = None
    markdown: str | None = None
    error_message: str | None = None
    job_id: int | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Any) -> Digest:
        data = dict(row)

        def _loads(key: str, fallback: Any) -> Any:
            raw = data.get(key)
            if not raw:
                return fallback
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return fallback

        return cls(
            id=data["id"],
            name=data["name"],
            template=data["template"],
            template_version=data.get("template_version"),
            status=data["status"],
            selection=_loads("selection_json", {}),
            item_ids=_loads("item_ids_json", []),
            provider=data.get("provider"),
            model=data.get("model"),
            tokens_in=data.get("tokens_in") or 0,
            tokens_out=data.get("tokens_out") or 0,
            cost_estimate=data.get("cost_estimate"),
            markdown=data.get("markdown"),
            error_message=data.get("error_message"),
            job_id=data.get("job_id"),
            created_at=data.get("created_at"),
            finished_at=data.get("finished_at"),
        )


class ChatCitation(ORMBase):
    id: int | None = None
    message_id: int | None = None
    item_id: int
    media_file_id: int | None = None
    snippet: str | None = None


class ChatMessage(ORMBase):
    id: int | None = None
    session_id: int
    role: ChatRole
    content: str
    created_at: datetime | None = None
    citations: list[ChatCitation] = Field(default_factory=list)


class ChatSession(ORMBase):
    id: int | None = None
    title: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
