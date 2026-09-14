"""Library/gallery API.

Owns: the browsable gallery grid, filtering by author/media type/tag/
category/date/free-text, single-item detail, manual tag editing, manual
category assignment, and the category taxonomy CRUD.

Uses plain SQL (via `gramvault.db.session.session_scope`) joining
items/authors/media_files/tags/categories, per the project's "sqlite3,
not an ORM" convention (see `gramvault/db/session.py`).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from gramvault.api.deps import get_config_dependency
from gramvault.chat import fts
from gramvault.chat.retrieval import fetch_items
from gramvault.config import Config
from gramvault.db.session import session_scope
from gramvault.models.schemas import (
    Author,
    Category,
    CategoryWithCount,
    Item,
    MediaFile,
    MediaType,
    Tag,
)

router = APIRouter(prefix="/api/library", tags=["library"])

# Sentinel `category` filter value meaning "items with no category yet".
UNCATEGORIZED = "__uncategorized__"

_ITEM_SELECT = """
    SELECT items.*, authors.username AS author_username, authors.full_name AS author_full_name,
           authors.profile_url AS author_profile_url, authors.avatar_path AS author_avatar_path,
           categories.name AS category_name
    FROM items
    LEFT JOIN authors ON authors.id = items.author_id
    LEFT JOIN categories ON categories.id = items.category_id
"""


class ItemListResponse(BaseModel):
    items: list[Item]
    total: int
    page: int
    page_size: int


class ItemIdListResponse(BaseModel):
    ids: list[int]
    total: int


class TagUpdateRequest(BaseModel):
    tags: list[str]


class ItemCategoryUpdateRequest(BaseModel):
    # None clears the category. Any other value must be an existing category id.
    category_id: int | None = None


class CategoryCreateRequest(BaseModel):
    name: str
    description: str | None = None
    color: str | None = None
    sort_order: int | None = None


class CategoryUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    color: str | None = None
    sort_order: int | None = None


class CategoryListResponse(BaseModel):
    categories: list[CategoryWithCount]
    uncategorized_count: int
    total: int


def _fetch_media_files(conn: sqlite3.Connection, item_id: int) -> list[MediaFile]:
    rows = conn.execute(
        "SELECT * FROM media_files WHERE item_id = ? ORDER BY sequence_index", (item_id,)
    ).fetchall()
    return [MediaFile.model_validate(dict(row)) for row in rows]


def _fetch_tags(conn: sqlite3.Connection, item_id: int) -> list[Tag]:
    rows = conn.execute(
        """
        SELECT tags.* FROM tags
        JOIN item_tags ON item_tags.tag_id = tags.id
        WHERE item_tags.item_id = ?
        ORDER BY tags.name
        """,
        (item_id,),
    ).fetchall()
    return [Tag.model_validate(dict(row)) for row in rows]


def _row_to_item(conn: sqlite3.Connection, row: sqlite3.Row) -> Item:
    data = dict(row)
    author = None
    if data.get("author_id") is not None:
        author = Author(
            id=data["author_id"],
            username=data["author_username"],
            full_name=data["author_full_name"],
            profile_url=data["author_profile_url"],
            avatar_path=data["author_avatar_path"],
        )
    return Item(
        id=data["id"],
        external_id=data["external_id"],
        author=author,
        media_type=data["media_type"],
        caption=data["caption"],
        permalink=data["permalink"],
        taken_at=data["taken_at"],
        imported_at=data["imported_at"],
        import_job_id=data["import_job_id"],
        enrichment_status=data["enrichment_status"],
        category_id=data.get("category_id"),
        category=data.get("category_name"),
        category_source=data.get("category_source"),
        category_confidence=data.get("category_confidence"),
        category_reason=data.get("category_reason"),
        tags=_fetch_tags(conn, data["id"]),
        media_files=_fetch_media_files(conn, data["id"]),
    )


def _category_row(conn: sqlite3.Connection, category_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()


# --- gallery listing -------------------------------------------------------


def _item_filter_sql(
    conn: sqlite3.Connection,
    *,
    author: str | None,
    media_type: MediaType | None,
    tag: str | None,
    category: str | None,
    q: str | None,
    needs_review: bool,
    date_from: datetime | None,
    date_to: datetime | None,
) -> tuple[str, str, list[object]]:
    """Shared gallery filter → `(joins, where_sql, params)`. Used by the
    paginated listing and the id-only listing so both stay in lockstep."""
    clauses: list[str] = []
    params: list[object] = []
    joins: list[str] = []

    if author:
        clauses.append("authors.username = ?")
        params.append(author)
    if media_type:
        clauses.append("items.media_type = ?")
        params.append(media_type.value)
    if category == UNCATEGORIZED:
        clauses.append("items.category_id IS NULL")
    elif category:
        clauses.append("items.category_id = (SELECT id FROM categories WHERE name = ?)")
        params.append(category)
    if q:
        # R12: this used to be caption-only LIKE even though the FTS5
        # index (migration 006) already covers tags/transcript/vision/OCR
        # text — that broader search was only reachable through the
        # semantic endpoint. Same tokenizer as chat.retrieval.keyword_search,
        # so the two behave consistently; LIKE is still the fallback for a
        # DB predating migration 006 or a query with no word characters.
        match_expr = fts.match_expr(q) if fts.ensure_populated(conn) else ""
        if match_expr:
            try:
                # FTS5 treats a bare "AND"/"OR"/"NOT"/"NEAR" token as a
                # query operator, not a search term — `match_expr("cats
                # AND dogs")` produces `cats* AND* dogs*`, a syntax error
                # (the operator followed by a stray `*`). keyword_search()
                # already guards this with a try/except around the real
                # query for the same reason; probe once here so a bad
                # expression falls through to the LIKE branch below
                # instead of 500ing the whole listing.
                conn.execute(
                    "SELECT 1 FROM items_fts WHERE items_fts MATCH ? LIMIT 1", (match_expr,)
                )
            except sqlite3.OperationalError:
                match_expr = ""
        if match_expr:
            joins.append("JOIN items_fts ON items_fts.rowid = items.id")
            clauses.append("items_fts MATCH ?")
            params.append(match_expr)
        else:
            clauses.append("items.caption LIKE ?")
            params.append(f"%{q}%")
    if needs_review:
        clauses.append(
            "items.category_id IS NOT NULL "
            "AND COALESCE(items.category_source, '') != 'manual' "
            "AND COALESCE(items.category_confidence, 0) < 0.6"
        )
    if date_from:
        clauses.append("items.taken_at >= ?")
        params.append(date_from.isoformat())
    if date_to:
        clauses.append("items.taken_at <= ?")
        params.append(date_to.isoformat())
    if tag:
        joins.append(
            "JOIN item_tags ON item_tags.item_id = items.id JOIN tags ON tags.id = item_tags.tag_id"
        )
        clauses.append("tags.name = ?")
        params.append(tag)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return " ".join(joins), where_sql, params


_ITEM_ORDER = "ORDER BY items.imported_at DESC, items.id DESC"


@router.get("/items", response_model=ItemListResponse)
async def list_items(
    author: str | None = Query(default=None, description="Filter by author username"),
    media_type: MediaType | None = Query(default=None, description="Filter by media type"),
    tag: str | None = Query(default=None, description="Filter by tag name"),
    category: str | None = Query(
        default=None,
        description=f"Filter by category name, or '{UNCATEGORIZED}' for items with no category",
    ),
    q: str | None = Query(default=None, description="Free-text search over captions"),
    ids: str | None = Query(
        default=None,
        description="Comma-separated item ids: return exactly these, in this order "
        "(all other filters and pagination are ignored). Powers the feed / prev-next.",
    ),
    needs_review: bool = Query(
        default=False,
        description="Only items with a low-confidence automatic category (the review queue)",
    ),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    config: Config = Depends(get_config_dependency),
) -> ItemListResponse:
    """Paginated, filterable gallery listing, joined with each item's
    author/category/tags/media_files. With `ids=`, returns just those
    items in the given order (for hydrating a feed window)."""
    if ids is not None:
        id_list = _parse_id_csv(ids)[:200]
        with session_scope(config) as conn:
            items = _items_by_ids(conn, id_list)
        return ItemListResponse(
            items=items, total=len(items), page=1, page_size=max(1, len(items))
        )

    with session_scope(config) as conn:
        joins, where_sql, params = _item_filter_sql(
            conn,
            author=author,
            media_type=media_type,
            tag=tag,
            category=category,
            q=q,
            needs_review=needs_review,
            date_from=date_from,
            date_to=date_to,
        )
        total_row = conn.execute(
            f"SELECT COUNT(DISTINCT items.id) AS c FROM items "
            f"LEFT JOIN authors ON authors.id = items.author_id {joins} {where_sql}",
            params,
        ).fetchone()
        total = total_row["c"] if total_row else 0

        offset = (page - 1) * page_size
        page_ids = [
            row["id"]
            for row in conn.execute(
                f"SELECT DISTINCT items.id AS id FROM items "
                f"LEFT JOIN authors ON authors.id = items.author_id {joins} {where_sql} "
                f"{_ITEM_ORDER} LIMIT ? OFFSET ?",
                [*params, page_size, offset],
            ).fetchall()
        ]
        items = _items_by_ids(conn, page_ids)

    return ItemListResponse(items=items, total=total, page=page, page_size=page_size)


def _parse_id_csv(raw: str) -> list[int]:
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.append(int(part))
    return out


def _items_by_ids(conn: sqlite3.Connection, ids: list[int]) -> list[Item]:
    """Full `Item`s for `ids`, returned in the given order. Batched through
    `chat.retrieval.fetch_items` (3 queries total for however many ids)
    rather than one `_row_to_item` (2 more queries each) per item — a
    48-item gallery page used to issue ~100 queries for this (R8)."""
    if not ids:
        return []
    by_id = fetch_items(conn, ids)
    return [by_id[i] for i in ids if i in by_id]


@router.get("/item-ids", response_model=ItemIdListResponse)
async def list_item_ids(
    author: str | None = Query(default=None),
    media_type: MediaType | None = Query(default=None),
    tag: str | None = Query(default=None),
    category: str | None = Query(default=None),
    q: str | None = Query(default=None),
    needs_review: bool = Query(default=False),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    limit: int = Query(default=5000, ge=1, le=20000),
    config: Config = Depends(get_config_dependency),
) -> ItemIdListResponse:
    """Just the ordered item ids for a gallery filter (same order as
    `/items`). Powers the reels-style feed and prev/next navigation
    without shipping every item's full payload."""
    with session_scope(config) as conn:
        joins, where_sql, params = _item_filter_sql(
            conn,
            author=author,
            media_type=media_type,
            tag=tag,
            category=category,
            q=q,
            needs_review=needs_review,
            date_from=date_from,
            date_to=date_to,
        )
        rows = conn.execute(
            f"SELECT DISTINCT items.id AS id FROM items "
            f"LEFT JOIN authors ON authors.id = items.author_id {joins} {where_sql} "
            f"{_ITEM_ORDER} LIMIT ?",
            [*params, limit],
        ).fetchall()
    ids = [row["id"] for row in rows]
    return ItemIdListResponse(ids=ids, total=len(ids))


@router.get("/items/{item_id}", response_model=Item)
async def get_item(
    item_id: int,
    config: Config = Depends(get_config_dependency),
) -> Item:
    """Fetch a single item with its media files and tags."""
    with session_scope(config) as conn:
        row = conn.execute(f"{_ITEM_SELECT} WHERE items.id = ?", (item_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
        item = _row_to_item(conn, row)
    return item


@router.get("/authors", response_model=list[Author])
async def list_authors(
    config: Config = Depends(get_config_dependency),
) -> list[Author]:
    """List all known authors (for filter dropdowns)."""
    with session_scope(config) as conn:
        rows = conn.execute("SELECT * FROM authors ORDER BY username").fetchall()
    return [Author.model_validate(dict(row)) for row in rows]


@router.get("/tags", response_model=list[Tag])
async def list_tags(
    config: Config = Depends(get_config_dependency),
) -> list[Tag]:
    """List all known tags (for filter dropdowns / tag-cloud UI)."""
    with session_scope(config) as conn:
        rows = conn.execute("SELECT * FROM tags ORDER BY name").fetchall()
    return [Tag.model_validate(dict(row)) for row in rows]


# --- categories ----------------------------------------------------------


@router.get("/categories", response_model=CategoryListResponse)
async def list_categories(
    config: Config = Depends(get_config_dependency),
) -> CategoryListResponse:
    """The category taxonomy with per-category item counts, plus how many
    items are still uncategorised (for the gallery filter)."""
    with session_scope(config) as conn:
        rows = conn.execute(
            """
            SELECT c.*, (SELECT COUNT(*) FROM items WHERE items.category_id = c.id) AS count
            FROM categories c
            ORDER BY c.sort_order, c.name
            """
        ).fetchall()
        uncategorized = conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE category_id IS NULL"
        ).fetchone()["n"]
        total = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
    return CategoryListResponse(
        categories=[CategoryWithCount.model_validate(dict(r)) for r in rows],
        uncategorized_count=uncategorized,
        total=total,
    )


@router.post("/categories", response_model=Category, status_code=201)
async def create_category(
    body: CategoryCreateRequest,
    config: Config = Depends(get_config_dependency),
) -> Category:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Category name must not be empty")
    with session_scope(config) as conn:
        if conn.execute("SELECT 1 FROM categories WHERE name = ?", (name,)).fetchone():
            raise HTTPException(status_code=409, detail=f"Category '{name}' already exists")
        sort_order = body.sort_order
        if sort_order is None:
            sort_order = (
                conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM categories")
                .fetchone()["n"]
            )
        cursor = conn.execute(
            "INSERT INTO categories (name, description, color, sort_order) VALUES (?, ?, ?, ?)",
            (name, body.description, body.color, sort_order),
        )
        row = _category_row(conn, cursor.lastrowid)
    return Category.model_validate(dict(row))


@router.patch("/categories/{category_id}", response_model=Category)
async def update_category(
    category_id: int,
    body: CategoryUpdateRequest,
    config: Config = Depends(get_config_dependency),
) -> Category:
    with session_scope(config) as conn:
        if _category_row(conn, category_id) is None:
            raise HTTPException(status_code=404, detail=f"Category {category_id} not found")
        if body.name is not None:
            new_name = body.name.strip()
            if not new_name:
                raise HTTPException(status_code=422, detail="Category name must not be empty")
            clash = conn.execute(
                "SELECT 1 FROM categories WHERE name = ? AND id != ?", (new_name, category_id)
            ).fetchone()
            if clash:
                raise HTTPException(status_code=409, detail=f"Category '{new_name}' already exists")
            conn.execute("UPDATE categories SET name = ? WHERE id = ?", (new_name, category_id))
        for field in ("description", "color", "sort_order"):
            value = getattr(body, field)
            if value is not None:
                conn.execute(
                    f"UPDATE categories SET {field} = ? WHERE id = ?", (value, category_id)
                )
        row = _category_row(conn, category_id)
    return Category.model_validate(dict(row))


@router.delete("/categories/{category_id}", status_code=204)
async def delete_category(
    category_id: int,
    move_to: int | None = Query(
        default=None, description="Reassign items in this category to this category id first"
    ),
    config: Config = Depends(get_config_dependency),
) -> None:
    """Delete a category. If items still use it, either pass `move_to` to
    reassign them, or accept that they become uncategorised (the FK is
    `ON DELETE SET NULL`) — the latter requires no items, so an accidental
    delete of a populated category needs an explicit choice."""
    with session_scope(config) as conn:
        if _category_row(conn, category_id) is None:
            raise HTTPException(status_code=404, detail=f"Category {category_id} not found")
        in_use = conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE category_id = ?", (category_id,)
        ).fetchone()["n"]
        if move_to is not None:
            if move_to == category_id or _category_row(conn, move_to) is None:
                raise HTTPException(status_code=422, detail=f"Invalid move_to category {move_to}")
            conn.execute(
                "UPDATE items SET category_id = ? WHERE category_id = ?", (move_to, category_id)
            )
        elif in_use:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{in_use} item(s) still use this category — pass ?move_to=<id> to reassign "
                    "them, or move them off it first"
                ),
            )
        conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))


# --- per-item edits ----------------------------------------------------------


@router.patch("/items/{item_id}", response_model=Item)
async def update_item_category(
    item_id: int,
    body: ItemCategoryUpdateRequest,
    config: Config = Depends(get_config_dependency),
) -> Item:
    """Set (or clear, with `category_id: null`) an item's category. A manual
    assignment is marked `source='manual'` / `confidence=1.0` and is never
    overwritten by the automatic classifier."""
    with session_scope(config) as conn:
        if conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")

        if body.category_id is not None and _category_row(conn, body.category_id) is None:
            raise HTTPException(
                status_code=422, detail=f"Category {body.category_id} does not exist"
            )

        if body.category_id is None:
            conn.execute(
                "UPDATE items SET category_id = NULL, category_source = NULL, "
                "category_confidence = NULL, category_reason = NULL, "
                "category_updated_at = datetime('now') WHERE id = ?",
                (item_id,),
            )
        else:
            conn.execute(
                "UPDATE items SET category_id = ?, category_source = 'manual', "
                "category_confidence = 1.0, category_reason = NULL, "
                "category_updated_at = datetime('now') WHERE id = ?",
                (body.category_id, item_id),
            )

        row = conn.execute(f"{_ITEM_SELECT} WHERE items.id = ?", (item_id,)).fetchone()
        item = _row_to_item(conn, row)
    return item


@router.patch("/items/{item_id}/tags", response_model=Item)
async def update_item_tags(
    item_id: int,
    body: TagUpdateRequest,
    config: Config = Depends(get_config_dependency),
) -> Item:
    """Replace an item's manually-assigned tags.

    Decision: only tags of kind='manual' are replaced by this endpoint —
    auto-generated tags (from A3's AI pipeline) and hashtag tags (parsed
    from captions) are left untouched, since a user editing "their" tags
    shouldn't accidentally wipe out AI-generated ones. Any name in
    `body.tags` that doesn't already exist as a tag is created as
    kind='manual'; if it already exists under any kind, the existing tag
    row is simply (re-)linked to this item.
    """
    with session_scope(config) as conn:
        exists = conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")

        conn.execute(
            """
            DELETE FROM item_tags
            WHERE item_id = ?
              AND tag_id IN (SELECT id FROM tags WHERE kind = 'manual')
            """,
            (item_id,),
        )
        for raw_name in body.tags:
            name = raw_name.strip()
            if not name:
                continue
            conn.execute(
                "INSERT INTO tags (name, kind) VALUES (?, 'manual') "
                "ON CONFLICT(name) DO NOTHING",
                (name,),
            )
            tag_row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
            conn.execute(
                "INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?)",
                (item_id, tag_row["id"]),
            )

        row = conn.execute(f"{_ITEM_SELECT} WHERE items.id = ?", (item_id,)).fetchone()
        item = _row_to_item(conn, row)
        fts.reindex(conn, [item_id])  # tags are a keyword-search field
    return item
