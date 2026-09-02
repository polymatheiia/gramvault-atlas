"""Digest engine — map/reduce a selection of items into one Markdown doc.

The by-hand `~/reels-workflow` recommendation docs were built map -> reduce:
per-batch structured extraction, then one merge/dedupe/render pass. This
module does the same through the `digest` provider.

    select_items()   selection dict           -> list[int]
    run_digest()     digest row id            -> writes markdown + manifest

`run_digest` is the job body (`jobs.kind='digest'`); the HTTP surface is
`api/routes_digests.py`, the offline entry is `gramvault digest`.

Templates live in `ai/digest_templates/*.yaml` (bundled) with user
overrides in `<data>/digest_templates/*.yaml`; see `load_templates`.
Post-processing (`_postprocess`) is pure code: it drops `[[item:<id>]]`
markers that aren't in the selection and rewrites each cited line's link
target / @handle from the DB, because the model must not be trusted for
URLs.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from gramvault.ai.document_builder import build_content_document
from gramvault.ai.providers import get_provider
from gramvault.chat.prompt import CITATION_MARKER_REGEX
from gramvault.chat.retrieval import fetch_items, hybrid_search
from gramvault.config import Config, get_config
from gramvault.db.session import session_scope
from gramvault.models.schemas import Item

_BUILTIN_DIR = Path(__file__).parent / "digest_templates"

# ~chars per token, matching ai/classifier.py's cheap estimate.
_CHARS_PER_TOKEN = 4
# Leave headroom below a typical context window for the prompt + the reply.
_EXTRACT_BUDGET_TOKENS = 8000

# Rough USD per 1M tokens (in, out), 2026 public list prices. Best-effort —
# used only for the pre-flight estimate and the stored manifest.
_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "anthropic": (3.0, 15.0),
    "openai": (2.5, 10.0),
    "openrouter": (2.5, 10.0),
    "ollama": (0.0, 0.0),
}


class DigestError(RuntimeError):
    """A digest run failed for a reason worth showing the user."""


@dataclass(frozen=True)
class DigestTemplate:
    name: str
    description: str
    extract_prompt: str
    reduce_prompt: str
    extract_schema: Any = None
    default_task: str = "digest"
    version: str = "1"
    source: str = "builtin"  # builtin | user

    @property
    def schema_hint(self) -> str:
        if not self.extract_schema:
            return '[{"...": "...", "item_id": <id>}]'
        return json.dumps(self.extract_schema, ensure_ascii=False, indent=2)


# --- templates -----------------------------------------------------------


def _user_templates_dir(config: Config) -> Path:
    return config.resolved_db_path.parent / "digest_templates"


def _parse_template(path: Path, source: str) -> DigestTemplate | None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    try:
        return DigestTemplate(
            name=str(raw["name"]).strip(),
            description=str(raw.get("description", "")).strip(),
            extract_prompt=str(raw["extract_prompt"]).strip(),
            reduce_prompt=str(raw["reduce_prompt"]).strip(),
            extract_schema=raw.get("extract_schema"),
            default_task=str(raw.get("default_task", "digest")),
            version=str(raw.get("version", "1")),
            source=source,
        )
    except (KeyError, TypeError):
        return None


def load_templates(config: Config | None = None) -> dict[str, DigestTemplate]:
    """Bundled templates, with any same-named file in
    `<data>/digest_templates/` taking precedence."""
    config = config or get_config()
    templates: dict[str, DigestTemplate] = {}
    for directory, source in ((_BUILTIN_DIR, "builtin"), (_user_templates_dir(config), "user")):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            template = _parse_template(path, source)
            if template and template.name:
                templates[template.name] = template
    return templates


def get_template(name: str, config: Config | None = None) -> DigestTemplate:
    templates = load_templates(config)
    if name not in templates:
        raise DigestError(f"unknown digest template {name!r} (have: {', '.join(sorted(templates))})")
    return templates[name]


# --- selection ---------------------------------------------------------


@dataclass
class Selection:
    category: str | None = None
    query: str | None = None
    item_ids: list[int] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"category": self.category, "query": self.query, "item_ids": self.item_ids}

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> Selection:
        data = data or {}
        return cls(
            category=data.get("category") or None,
            query=data.get("query") or None,
            item_ids=[int(i) for i in (data.get("item_ids") or [])],
        )


async def select_items(
    conn: sqlite3.Connection, selection: Selection, config: Config | None = None
) -> list[int]:
    """Resolve a selection into item ids. `item_ids` wins; otherwise a
    `query` runs semantic search (optionally narrowed to `category`), and
    a bare `category` returns every item in it. Raises `DigestError` if
    the selection is empty."""
    config = config or get_config()

    if selection.item_ids:
        placeholders = ",".join("?" * len(selection.item_ids))
        rows = conn.execute(
            f"SELECT id FROM items WHERE id IN ({placeholders})", selection.item_ids
        ).fetchall()
        return [row["id"] for row in rows]

    category_id: int | None = None
    if selection.category:
        row = conn.execute(
            "SELECT id FROM categories WHERE name = ?", (selection.category,)
        ).fetchone()
        if row is None:
            raise DigestError(f"unknown category {selection.category!r}")
        category_id = row["id"]

    if selection.query:
        results = await hybrid_search(conn, selection.query, top_k=200, config=config)
        ids = [r.item_id for r in results]
        if category_id is not None and ids:
            placeholders = ",".join("?" * len(ids))
            keep = {
                row["id"]
                for row in conn.execute(
                    f"SELECT id FROM items WHERE id IN ({placeholders}) AND category_id = ?",
                    [*ids, category_id],
                )
            }
            ids = [i for i in ids if i in keep]
        return ids

    if category_id is not None:
        rows = conn.execute(
            "SELECT id FROM items WHERE category_id = ? ORDER BY imported_at", (category_id,)
        ).fetchall()
        return [row["id"] for row in rows]

    raise DigestError("a digest needs a category, a search query, or an explicit item list")


# --- item text + batching --------------------------------------------


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def item_block(item: Item) -> str:
    """The full text of one item for the extract prompt, headed by its id
    so the model can cite it."""
    author = f"@{item.author.username}" if item.author and item.author.username else "@?"
    date = item.taken_at.date().isoformat() if item.taken_at else "?"
    header = f"### item {item.id} — {item.media_type} — {author} — {date}"
    body = build_content_document(item) or (item.caption or "").strip() or "(no text)"
    return f"{header}\n{body}"


def _batches(blocks: list[str], budget_tokens: int) -> Iterator[list[str]]:
    batch: list[str] = []
    size = 0
    for block in blocks:
        estimate = estimate_tokens(block)
        if batch and size + estimate > budget_tokens:
            yield batch
            batch, size = [], 0
        batch.append(block)
        size += estimate
    if batch:
        yield batch


def plan_batches(items: list[Item]) -> list[list[str]]:
    return list(_batches([item_block(i) for i in items], _EXTRACT_BUDGET_TOKENS))


# --- map / reduce ----------------------------------------------------


def _parse_json_array(reply: str) -> list[dict]:
    text = reply.strip()
    if "```" in text:
        text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON array in model reply")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, list):
        raise ValueError("model reply was not a JSON array")
    return [row for row in parsed if isinstance(row, dict)]


_EXTRACT_SYSTEM = (
    "You extract structured entries from saved social-media posts. Reply with "
    "ONLY a JSON array of objects. Every object must include an accurate "
    '"item_id" (an integer) copied from the post it came from. Do not invent '
    "facts, titles, authors, or URLs."
)


async def _extract_batch(
    provider: Any, model: str, template: DigestTemplate, batch: list[str], valid_ids: set[int]
) -> tuple[list[dict], int, int]:
    user = (
        f"{template.extract_prompt}\n\n"
        f"Return a JSON array shaped like:\n{template.schema_hint}\n\n"
        f"Posts:\n\n" + "\n\n".join(batch)
    )
    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user", "content": user},
    ]
    reply = await provider.chat(model, messages)
    tokens_in = estimate_tokens(_EXTRACT_SYSTEM + user)
    tokens_out = estimate_tokens(reply)
    try:
        rows = _parse_json_array(reply)
    except (ValueError, json.JSONDecodeError):
        reply = await provider.chat(
            model, [*messages, {"role": "user", "content": "Return ONLY the JSON array."}]
        )
        tokens_out += estimate_tokens(reply)
        try:
            rows = _parse_json_array(reply)
        except (ValueError, json.JSONDecodeError):
            return [], tokens_in, tokens_out

    cleaned: list[dict] = []
    for row in rows:
        try:
            item_id = int(row.get("item_id"))
        except (TypeError, ValueError):
            continue
        if item_id in valid_ids:
            row["item_id"] = item_id
            cleaned.append(row)
    return cleaned, tokens_in, tokens_out


_REDUCE_SYSTEM = (
    "You merge extracted entries into one clean Markdown document. Deduplicate, "
    "group by theme, and cite sources as [[item:<id>]] using the item_id on each "
    "entry. Never invent a URL or a citation. Output only the Markdown, no "
    "preamble or code fence."
)


async def _reduce(
    provider: Any, model: str, template: DigestTemplate, name: str, rows: list[dict]
) -> tuple[str, int, int]:
    payload = json.dumps(rows, ensure_ascii=False)
    user = (
        f"# {name}\n\n{template.reduce_prompt}\n\n"
        f"Extracted entries (JSON):\n{payload}"
    )
    messages = [
        {"role": "system", "content": _REDUCE_SYSTEM},
        {"role": "user", "content": user},
    ]
    reply = await provider.chat(model, messages)
    return reply.strip(), estimate_tokens(_REDUCE_SYSTEM + user), estimate_tokens(reply)


# --- post-processing -----------------------------------------------


_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HANDLE_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_.]+)")


def _postprocess(markdown: str, items_by_id: dict[int, Item]) -> str:
    """Drop dangling `[[item:<id>]]` markers and, on every line that cites
    exactly one item, rewrite the markdown-link target and any `@handle`
    to the values from the DB."""
    out_lines: list[str] = []
    for line in markdown.splitlines():
        cited = [int(m) for m in re.findall(CITATION_MARKER_REGEX, line)]
        known = [i for i in cited if i in items_by_id]

        # Remove markers for ids not in the selection.
        for missing in set(cited) - set(known):
            line = line.replace(f"[[item:{missing}]]", "").rstrip()

        if len(known) == 1:
            item = items_by_id[known[0]]
            if item.permalink:
                line = _MD_LINK_RE.sub(
                    lambda m, url=item.permalink: f"[{m.group(1) or 'reel'}]({url})", line
                )
            if item.author and item.author.username:
                line = _HANDLE_RE.sub(f"@{item.author.username}", line)
        out_lines.append(line)
    return "\n".join(out_lines).strip() + "\n"


def cost_estimate(provider_name: str, tokens_in: int, tokens_out: int) -> float | None:
    price = _PRICE_PER_MTOK.get(provider_name.lower())
    if price is None:
        return None
    return round(tokens_in / 1e6 * price[0] + tokens_out / 1e6 * price[1], 4)


# --- orchestration -------------------------------------------------


@dataclass
class PreflightEstimate:
    item_count: int
    batches: int
    tokens_in: int
    tokens_out: int
    cost_estimate: float | None
    provider: str
    model: str


async def preflight(
    item_ids: list[int], template: DigestTemplate, config: Config | None = None
) -> PreflightEstimate:
    config = config or get_config()
    with session_scope(config) as conn:
        items = list(fetch_items(conn, item_ids).values())
    batches = plan_batches(items)
    tokens_in = sum(estimate_tokens("\n\n".join(b)) for b in batches) + len(items) * 20
    # +reduce: entries are far smaller than the source text; assume ~15%.
    tokens_in = int(tokens_in * 1.15)
    tokens_out = int(tokens_in * 0.25)
    provider, model = get_provider(template.default_task, config)
    return PreflightEstimate(
        item_count=len(items),
        batches=len(batches),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_estimate=cost_estimate(provider.name, tokens_in, tokens_out),
        provider=provider.name,
        model=model,
    )


async def run_digest(
    digest_id: int,
    config: Config | None = None,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Map/reduce the digest row's item snapshot into Markdown and write it
    back onto the row. Returns a summary (also stored as the job result)."""
    config = config or get_config()

    with session_scope(config) as conn:
        row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
        if row is None:
            raise DigestError(f"digest {digest_id} not found")
        name = row["name"]
        template = get_template(row["template"], config)
        item_ids = json.loads(row["item_ids_json"] or "[]")
        items_by_id = fetch_items(conn, item_ids)

    items = [items_by_id[i] for i in item_ids if i in items_by_id]
    valid_ids = set(items_by_id)
    provider, model = get_provider(template.default_task, config)
    await provider.ensure_ready(model)

    with session_scope(config) as conn:
        conn.execute(
            "UPDATE digests SET status = 'running', provider = ?, model = ? WHERE id = ?",
            (provider.name, model, digest_id),
        )

    batches = plan_batches(items)
    total_steps = len(batches) + 1
    entries: list[dict] = []
    tokens_in = tokens_out = 0
    for done, batch in enumerate(batches):
        if cancel_check and cancel_check():
            return {"status": "cancelled", "batches_done": done, "entries": len(entries)}
        rows, t_in, t_out = await _extract_batch(provider, model, template, batch, valid_ids)
        entries.extend(rows)
        tokens_in += t_in
        tokens_out += t_out
        if progress_cb:
            progress_cb(done + 1, total_steps)

    if not entries:
        raise DigestError("the extract pass found nothing to summarise in this selection")

    markdown, r_in, r_out = await _reduce(provider, model, template, name, entries)
    tokens_in += r_in
    tokens_out += r_out
    markdown = _postprocess(markdown, items_by_id)
    if progress_cb:
        progress_cb(total_steps, total_steps)

    cost = cost_estimate(provider.name, tokens_in, tokens_out)
    with session_scope(config) as conn:
        conn.execute(
            "UPDATE digests SET status = 'done', markdown = ?, template_version = ?, "
            "tokens_in = ?, tokens_out = ?, cost_estimate = ?, "
            "finished_at = datetime('now') WHERE id = ?",
            (markdown, template.version, tokens_in, tokens_out, cost, digest_id),
        )

    return {
        "status": "done",
        "items": len(items),
        "entries": len(entries),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_estimate": cost,
        "markdown_chars": len(markdown),
    }
