"""Two-pass item categoriser.

Pass 1 — `keyword_vote`: a free, deterministic multilingual keyword/hashtag
vote (port of the reels-workflow `classify.py`). Cheap, and a decent floor,
but memes/other/research bleed into each other and it can't read sarcasm or
non-English nuance.

Pass 2 — `llm_classify`: re-label each item from its actual text through the
`categorize` provider, in token-budgeted batches, returning strict JSON. The
keyword guess is passed in as a hint; on a parse failure (after one retry)
the batch falls back to its keyword guesses.

`categorize_items` orchestrates the two for a list of item ids and writes the
result to `items.category_*`. An item whose `category_source` is `'manual'`
is never touched.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from typing import Any, Literal

from gramvault.ai.classifier_keywords import KEYWORDS, TAG_OVERRIDE
from gramvault.ai.providers import get_provider
from gramvault.chat.retrieval import fetch_items
from gramvault.config import Config, get_config
from gramvault.db.session import session_scope
from gramvault.models.schemas import Item

CategorizeMethod = Literal["keyword", "llm", "keyword_then_llm"]

# needs_review thresholds from classify.py: a weak winning score or a thin
# margin over the runner-up.
_MIN_SCORE = 3.0
_MIN_MARGIN = 1.5
# An automatic label below this confidence lands in the review queue.
REVIEW_CONFIDENCE = 0.6

# Rough token budget per LLM batch (chars // 4). Small enough for a 3B local
# model's context, large enough to keep the request count sane.
_BATCH_TOKEN_BUDGET = 6000

_SYSTEM_PROMPT = (
    "You are a librarian sorting saved social-media posts into exactly one "
    "category each. Reply with ONLY a JSON object mapping each post id (a "
    'string) to {"category": <one category name from the rubric>, '
    '"confidence": <number 0-1>, "reason": <short phrase>}. '
    "Every id in the batch must appear in your reply."
)


@dataclass
class CategoryResult:
    category: str
    confidence: float
    reason: str
    source: Literal["keyword", "llm"]


_UNCLASSIFIED = CategoryResult("other", 0.1, "no signal", "keyword")


# --- text normalisation ------------------------------------------------------


def _normalise(text: str | None) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text.lower())
    text = text.replace("∕", "/")  # instaloader's sanitised slash
    return re.sub(r"\s+", " ", text)


def _media_text(item: Item, *attrs: str) -> str:
    parts: list[str] = []
    for media_file in item.media_files:
        for attr in attrs:
            value = getattr(media_file, attr, None)
            if value:
                parts.append(value)
    return " ".join(parts)


def _hashtags(item: Item) -> list[str]:
    return [t.name.lstrip("#").lower() for t in item.tags if t.kind == "hashtag"]


# --- pass 1: keyword vote --------------------------------------------------


def keyword_vote(item: Item, category_names: Collection[str]) -> CategoryResult:
    """Score `item` against `KEYWORDS` and return the winning category.

    `category_names` is the live taxonomy — a category with no keyword
    entries simply never scores. Falls back to `other` when nothing hits
    (or, if `other` was deleted, the first available category name)."""
    tags = set(_hashtags(item))
    caption = _normalise(item.caption)
    tags_blob = _normalise(" ".join(tags))
    visual = _normalise(_media_text(item, "vision_caption", "ocr_text"))
    transcript = _normalise(_media_text(item, "transcript"))
    # caption + hashtags weighted double, as in classify.py
    blob = " ".join([caption, caption, tags_blob, tags_blob, visual, transcript])

    scores = dict.fromkeys(category_names, 0.0)
    hits: dict[str, list[str]] = {name: [] for name in category_names}
    for category, keywords in KEYWORDS.items():
        if category not in scores:
            continue
        for keyword, weight in keywords.items():
            count = blob.count(keyword)
            if count:
                scores[category] += weight * (1 + 0.3 * (count - 1))
                label = keyword.strip()
                hits[category].append(label if count == 1 else f"{label}×{count}")

    for tag, category in TAG_OVERRIDE.items():
        if tag in tags and category in scores:
            return CategoryResult(category, 0.9, f"hashtag #{tag}", "keyword")

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (top, top_score), (runner_up, runner_score) = ranked[0], ranked[1]
    if top_score == 0:
        fallback = "other" if "other" in scores else next(iter(category_names), "other")
        return CategoryResult(fallback, 0.1, "no keyword signal", "keyword")

    margin = top_score - runner_score
    confidence = round(1 / (1 + math.exp(-(margin - _MIN_MARGIN))), 2)
    if top_score < _MIN_SCORE:
        confidence = min(confidence, 0.5)
    reason = ", ".join(hits[top][:6]) or "keyword match"
    if runner_score > 0:
        reason += f" (over {runner_up})"
    return CategoryResult(top, confidence, reason, "keyword")


# --- pass 2: LLM re-label --------------------------------------------------


def _rubric(categories: list[tuple[str, str]]) -> str:
    lines = [f"- {name}: {description}".rstrip(": ") for name, description in categories]
    return "Categories:\n" + "\n".join(lines)


def _clip(text: str | None, limit: int) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())[:limit]


def _item_line(item: Item, auto_guess: str) -> str:
    author = f"@{item.author.username}" if item.author and item.author.username else "?"
    date = item.taken_at.date().isoformat() if item.taken_at else "?"
    return (
        f"#{item.id} [{item.media_type}] {author} {date} "
        f"| C: {_clip(item.caption, 400)} "
        f"| TR: {_clip(_media_text(item, 'transcript'), 700)} "
        f"| V: {_clip(_media_text(item, 'ocr_text', 'vision_caption'), 400)} "
        f"| AUTO={auto_guess}"
    )


def _batches(lines: list[str]) -> Iterator[list[str]]:
    batch: list[str] = []
    size = 0
    for line in lines:
        estimate = len(line) // 4 + 1
        if batch and size + estimate > _BATCH_TOKEN_BUDGET:
            yield batch
            batch, size = [], 0
        batch.append(line)
        size += estimate
    if batch:
        yield batch


def _parse_labels(reply: str) -> dict[str, Any]:
    text = reply.strip()
    if "```" in text:
        text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in model reply")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model reply was not a JSON object")
    return parsed


def _line_id(line: str) -> int:
    return int(line[1 : line.index(" ")])


async def _ask(provider: Any, model: str, rubric: str, batch: list[str], *, retry: bool) -> str:
    content = f"{rubric}\n\nPosts:\n" + "\n".join(batch)
    if retry:
        content += "\n\nReturn ONLY the JSON object, no prose, no code fence."
    return await provider.chat(
        model,
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
    )


async def llm_classify(
    items: list[Item],
    keyword_guesses: dict[int, CategoryResult],
    categories: list[tuple[str, str]],
    config: Config,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[int, CategoryResult]:
    """Re-classify `items` through the `categorize` provider. Returns a
    result per item id; a batch that can't be parsed keeps its keyword
    guess. Stops early (leaving remaining items on their keyword guess) if
    `cancel_check` returns True between batches."""
    results: dict[int, CategoryResult] = {}
    if not items:
        return results

    provider, model = get_provider("categorize", config)
    await provider.ensure_ready(model)
    valid = {name for name, _ in categories}
    rubric = _rubric(categories)

    lines = [
        _item_line(item, keyword_guesses.get(item.id, _UNCLASSIFIED).category)
        for item in items
    ]
    total = len(lines)
    done = 0
    for batch in _batches(lines):
        if cancel_check and cancel_check():
            break
        try:
            parsed = _parse_labels(await _ask(provider, model, rubric, batch, retry=False))
        except (ValueError, json.JSONDecodeError):
            try:
                parsed = _parse_labels(await _ask(provider, model, rubric, batch, retry=True))
            except (ValueError, json.JSONDecodeError):
                parsed = {}

        for line in batch:
            item_id = _line_id(line)
            entry = parsed.get(str(item_id))
            results[item_id] = _entry_to_result(entry, valid) or keyword_guesses.get(
                item_id, _UNCLASSIFIED
            )
        done += len(batch)
        if progress_cb:
            progress_cb(done, total)

    for item in items:
        results.setdefault(item.id, keyword_guesses.get(item.id, _UNCLASSIFIED))
    return results


def _entry_to_result(entry: Any, valid: set[str]) -> CategoryResult | None:
    if not isinstance(entry, dict):
        return None
    category = str(entry.get("category", "")).strip()
    if category not in valid:
        return None
    raw_confidence = entry.get("confidence")
    confidence = float(raw_confidence) if isinstance(raw_confidence, (int, float)) else 0.7
    reason = str(entry.get("reason", "")).strip()[:200] or "LLM classification"
    return CategoryResult(category, round(min(max(confidence, 0.0), 1.0), 2), reason, "llm")


# --- orchestration -------------------------------------------------------


async def categorize_items(
    item_ids: list[int],
    method: CategorizeMethod,
    config: Config | None = None,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Categorise `item_ids` with `method` and persist the labels. Items
    already assigned `category_source='manual'` are skipped. Returns a
    summary dict (also stored as the job result)."""
    config = config or get_config()

    with session_scope(config) as conn:
        categories = [
            (row["name"], row["description"] or "")
            for row in conn.execute(
                "SELECT name, description FROM categories ORDER BY sort_order, name"
            )
        ]
        items_by_id = fetch_items(conn, item_ids)

    category_names = [name for name, _ in categories]
    items = [
        item
        for item in (items_by_id.get(item_id) for item_id in item_ids)
        if item is not None and item.category_source != "manual"
    ]
    skipped_manual = len(item_ids) - len(items)

    keyword_guesses = {item.id: keyword_vote(item, category_names) for item in items}

    if method == "keyword":
        final = dict(keyword_guesses)
    elif method == "llm":
        final = await llm_classify(
            items, keyword_guesses, categories, config,
            progress_cb=progress_cb, cancel_check=cancel_check,
        )
    else:  # keyword_then_llm — only re-run the low-confidence keyword guesses
        weak = [i for i in items if keyword_guesses[i.id].confidence < REVIEW_CONFIDENCE]
        settled = len(items) - len(weak)
        relay = None
        if progress_cb:
            relay = lambda d, _t: progress_cb(settled + d, len(items))  # noqa: E731
        llm_results = await llm_classify(
            weak, keyword_guesses, categories, config,
            progress_cb=relay, cancel_check=cancel_check,
        )
        final = {**keyword_guesses, **llm_results}

    if progress_cb:
        progress_cb(len(items), len(items))

    written = {"keyword": 0, "llm": 0}
    by_category: dict[str, int] = {}
    needs_review = 0
    with session_scope(config) as conn:
        name_to_id = {
            row["name"]: row["id"] for row in conn.execute("SELECT name, id FROM categories")
        }
        for item in items:
            result = final.get(item.id)
            category_id = name_to_id.get(result.category) if result else None
            if result is None or category_id is None:
                continue
            conn.execute(
                "UPDATE items SET category_id = ?, category_source = ?, "
                "category_confidence = ?, category_reason = ?, "
                "category_updated_at = datetime('now') "
                "WHERE id = ? AND COALESCE(category_source, '') != 'manual'",
                (category_id, result.source, result.confidence, result.reason, item.id),
            )
            written[result.source] += 1
            by_category[result.category] = by_category.get(result.category, 0) + 1
            if result.confidence < REVIEW_CONFIDENCE:
                needs_review += 1

    return {
        "processed": len(items),
        "keyword": written["keyword"],
        "llm": written["llm"],
        "needs_review": needs_review,
        "skipped_manual": skipped_manual,
        "by_category": by_category,
    }
