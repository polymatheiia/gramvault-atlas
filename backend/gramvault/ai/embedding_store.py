"""ChromaDB wrapper for item/media-file embeddings.

`upsert_item()` is called by the enrichment pipeline when it chunks and
embeds captions/transcripts; `query()` is called directly by
`gramvault.chat.retrieval`. Keep both signatures stable, or update those
call sites alongside any change.

Design:
    - A single persistent Chroma collection ("gramvault_items") at
      `config.resolved_chroma_dir`, one vector per (item, chunk). Each
      point's metadata carries `item_id` (int) and optionally
      `media_file_id` (int) plus a small text `snippet` for display, so
      results can be resolved back to SQLite rows without a second lookup
      for the common case.
    - IDs are opaque strings (`f"{item_id}:{chunk_index}"` by default) —
      only the metadata's `item_id`/`media_file_id` fields are semantically
      meaningful to callers.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

import chromadb

from gramvault.config import Config, get_config

COLLECTION_NAME = "gramvault_items"

# R9: `chromadb.PersistentClient(...)` opens its own on-disk index/SQLite
# state, so constructing a fresh one on every call (as this used to do) is
# real per-request overhead, not just an allocation. One client is safe to
# reuse for the life of the process per `chroma_dir` — a `reset_collection()`
# (e.g. before a re-embed) still operates on the same cached client, so it
# sees the drop-and-recreate immediately.
_clients: dict[str, chromadb.ClientAPI] = {}


@dataclass
class QueryResult:
    item_id: int
    media_file_id: int | None
    score: float
    snippet: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


def _client(config: Config) -> chromadb.ClientAPI:
    path = str(config.resolved_chroma_dir)
    client = _clients.get(path)
    if client is None:
        config.resolved_chroma_dir.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(
            path=path,
            settings=chromadb.Settings(anonymized_telemetry=False),
        )
        _clients[path] = client
    return client


def get_collection(config: Config | None = None):
    """Return the (created-if-missing) shared Chroma collection."""
    config = config or get_config()
    client = _client(config)
    return client.get_or_create_collection(COLLECTION_NAME)


def reset_collection(config: Config | None = None) -> None:
    """Drop and recreate the collection — every embedding is discarded.
    Used before a re-embed when the embedding model (and so the vector
    dimension) changes, since Chroma can't hold a mixed collection."""
    config = config or get_config()
    client = _client(config)
    with contextlib.suppress(Exception):  # "collection doesn't exist" is fine
        client.delete_collection(COLLECTION_NAME)
    client.get_or_create_collection(COLLECTION_NAME)


def upsert_item(
    item_id: int,
    embedding: list[float],
    document: str,
    media_file_id: int | None = None,
    chunk_index: int = 0,
    extra_metadata: dict[str, Any] | None = None,
    config: Config | None = None,
) -> None:
    """Upsert a single embedded chunk for `item_id`."""
    collection = get_collection(config)
    point_id = f"{item_id}:{media_file_id or 0}:{chunk_index}"
    metadata: dict[str, Any] = {"item_id": item_id}
    if media_file_id is not None:
        metadata["media_file_id"] = media_file_id
    if extra_metadata:
        metadata.update(extra_metadata)
    collection.upsert(
        ids=[point_id],
        embeddings=[embedding],
        documents=[document],
        metadatas=[metadata],
    )


def prune_item_chunks(item_id: int, chunk_count: int, config: Config | None = None) -> None:
    """Delete `item_id`'s chunks beyond the first `chunk_count` (the
    `upsert_item` ids `"{item_id}:0:{index}"`). Re-embedding an item whose
    document got shorter — or empty — otherwise left its old tail chunks in
    the index, still matching searches with text the item no longer has."""
    collection = get_collection(config)
    keep = {f"{item_id}:0:{index}" for index in range(chunk_count)}
    existing = collection.get(where={"item_id": item_id}, include=[])["ids"]
    stale = [point_id for point_id in existing if point_id not in keep]
    if stale:
        collection.delete(ids=stale)


def get_item_embedding(item_id: int, config: Config | None = None) -> list[float] | None:
    """The stored embedding for one of `item_id`'s chunks (arbitrarily the
    first one Chroma returns), for "find similar" — reuses whatever's
    already indexed rather than re-embedding the item's text through an AI
    provider. `None` if the item has no embedded chunk yet (not enriched,
    or `embed` was left off its enrichment run)."""
    collection = get_collection(config)
    raw = collection.get(where={"item_id": item_id}, include=["embeddings"], limit=1)
    embeddings = raw.get("embeddings")
    if embeddings is None or len(embeddings) == 0:
        return None
    return list(embeddings[0])


def delete_by_item(item_id: int, config: Config | None = None) -> None:
    """Drop every embedded chunk for `item_id` — called when the item
    itself is deleted from the library, so a stale vector never surfaces
    in chat retrieval or "find similar" for an item that no longer exists."""
    collection = get_collection(config)
    with contextlib.suppress(Exception):  # nothing embedded yet is fine
        collection.delete(where={"item_id": item_id})


def query(embedding: list[float], top_k: int = 10, config: Config | None = None) -> list[QueryResult]:
    """Nearest-neighbor search. Returns results ordered nearest-first, with
    `score` as a similarity in roughly [0, 1] (1 - normalized distance;
    clamped, since Chroma's raw distance metric can exceed 1)."""
    collection = get_collection(config)
    count = collection.count()
    if count == 0:
        return []
    n_results = min(top_k, count)
    raw = collection.query(query_embeddings=[embedding], n_results=n_results)

    results: list[QueryResult] = []
    ids = raw.get("ids", [[]])[0]
    distances = raw.get("distances", [[]])[0]
    documents = raw.get("documents", [[]])[0]
    metadatas = raw.get("metadatas", [[]])[0]
    for i in range(len(ids)):
        metadata = metadatas[i] or {}
        distance = distances[i] if i < len(distances) else 1.0
        score = max(0.0, 1.0 - distance)
        results.append(
            QueryResult(
                item_id=int(metadata["item_id"]),
                media_file_id=metadata.get("media_file_id"),
                score=score,
                snippet=documents[i] if i < len(documents) else None,
                metadata=metadata,
            )
        )
    return results
