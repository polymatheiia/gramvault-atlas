"""Render a single `Item` as an Obsidian-flavored Markdown note.

Design:
  - Frontmatter carries `gramvault_id` (== `Item.id`) — this is the stable
    idempotency key. `exporter.py` also embeds it in the filename so a
    re-export overwrites the same file rather than creating
    `Item (1).md`-style duplicates, and scans existing notes' frontmatter
    for stragglers (e.g. if the author/date changed since the last export)
    so those get cleaned up too instead of leaking a duplicate note.
  - **Managed region.** GramVault owns the frontmatter and the body
    between `%% gramvault:start %%` / `%% gramvault:end %%`; anything the
    user writes *after* the end marker, and any frontmatter key GramVault
    doesn't own, is preserved across re-exports. A legacy note with no
    markers is treated as fully managed once, then gains markers.
  - Body includes the caption, AI vision captions / on-screen text (OCR) /
    transcripts (best-effort: fields may be `None` if enrichment hasn't
    run), and one media reference per `MediaFile`.
  - Filenames are sanitized to be safe on Windows *and* macOS/Linux at
    once (Windows is the strictest: reserved chars `<>:"/\\|?*`, no
    trailing dot/space, reserved device names like `CON`/`COM1`).
"""

from __future__ import annotations

import re
from typing import Literal, NamedTuple

import yaml

from gramvault.models.schemas import Item, MediaFile

# Characters invalid in filenames on Windows (the strictest of the three
# platforms we care about); also covers control characters.
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RUN = re.compile(r"\s+")

# Windows reserved device names (case-insensitive), with or without an
# extension — writing "CON.md" fails on Windows.
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

INDEX_NOTE_FILENAME = "GramVault Index.md"
DASHBOARD_NOTE_FILENAME = "GramVault Dashboard.md"

MANAGED_START = "%% gramvault:start %%"
MANAGED_END = "%% gramvault:end %%"

# Frontmatter keys GramVault regenerates on every export. Any other key an
# existing note carries (e.g. a user's `rating`, `aliases`, `cssclasses`)
# is preserved.
_OWNED_FRONTMATTER_KEYS = {
    "gramvault_id",
    "type",
    "author",
    "account",
    "date",
    "saved_at",
    "category",
    "category_source",
    "tags",
    "hashtags",
    "enrichment",
    "source_url",
}

Layout = Literal["flat", "by-category", "by-date"]


def sanitize_filename_component(text: str, *, max_length: int = 60) -> str:
    """Sanitize `text` into a safe filename component for Windows, macOS,
    and Linux simultaneously. Never raises; always returns a non-empty
    string ("untitled" as the last-resort fallback)."""
    if not text:
        return "untitled"

    cleaned = _INVALID_FILENAME_CHARS.sub("-", text)
    cleaned = _WHITESPACE_RUN.sub("-", cleaned.strip())
    # Windows disallows trailing dots/spaces on path components.
    cleaned = cleaned.strip(". ")
    cleaned = cleaned.strip("-")

    if not cleaned:
        return "untitled"

    cleaned = cleaned[:max_length].strip("-")
    if not cleaned:
        return "untitled"

    if cleaned.upper() in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    return cleaned


def note_filename(item: Item) -> str:
    """Stable, cross-platform-safe filename for `item`'s note.

    Embeds `item.id` (the `gramvault_id`) so re-exporting the same item
    always resolves to the same path — the idempotency key for exports.
    """
    if item.id is None:
        raise ValueError("Item.id is required to compute a stable export filename")

    author_part = sanitize_filename_component(
        item.author.username if item.author is not None else "unknown", max_length=40
    )
    date_source = item.taken_at or item.imported_at
    date_part = date_source.strftime("%Y-%m-%d") if date_source else "nodate"

    return f"{date_part}_{author_part}_{item.id}.md"


def note_relpath(item: Item, layout: Layout = "flat") -> str:
    """The note's path relative to the export subfolder, honouring `layout`:
    `flat` → `<name>`, `by-category` → `<category>/<name>`,
    `by-date` → `<YYYY-MM>/<name>`."""
    name = note_filename(item)
    if layout == "by-category":
        folder = sanitize_filename_component(item.category or "uncategorized", max_length=40)
        return f"{folder}/{name}"
    if layout == "by-date":
        date_source = item.taken_at or item.imported_at
        folder = date_source.strftime("%Y-%m") if date_source else "nodate"
        return f"{folder}/{name}"
    return name


def media_filename(item: Item, media_file: MediaFile) -> str:
    """Stable filename for a copied-into-vault media file, namespaced by
    item id + sequence index so carousels don't collide and re-exports
    overwrite the same copy."""
    original_suffix = ""
    if "." in media_file.file_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]:
        original_suffix = "." + media_file.file_path.rsplit(".", 1)[-1]
    original_suffix = sanitize_filename_component(original_suffix, max_length=10) if original_suffix else ""
    if original_suffix and not original_suffix.startswith("."):
        original_suffix = f".{original_suffix}"
    return f"{item.id}_{media_file.sequence_index}{original_suffix}"


class MediaLink(NamedTuple):
    """How a single MediaFile should be referenced from the note body."""

    media_file: MediaFile
    # Obsidian-relative path (embed) or a plain link target (link mode /
    # copy failed) — interpretation depends on `embed`.
    target: str
    embed: bool
    # For a copied video: an Obsidian-relative path to a still poster frame
    # to embed *above* a plain link to the clip (inline video is unreliable
    # on mobile Obsidian — plan §G6). None for photos and link-mode media.
    poster: str | None = None


def _media_type_value(item: Item) -> str:
    media_type = item.media_type
    return media_type.value if hasattr(media_type, "value") else str(media_type)


def _media_file_type_value(media_file: MediaFile) -> str:
    media_type = media_file.media_type
    return media_type.value if hasattr(media_type, "value") else str(media_type)


def _tag_kind(tag: object) -> str:
    kind = getattr(tag, "kind", "")
    return kind.value if hasattr(kind, "value") else str(kind)


def build_frontmatter(item: Item) -> dict[str, object]:
    """Build the YAML-frontmatter dict for `item`. `gramvault_id` is the
    stable re-export lookup key described in the module docstring;
    `hashtags` (parsed `#tags`) are kept separate from `tags` (manual/auto
    labels) so a Dataview query can target either."""
    date_value = item.taken_at or item.imported_at
    hashtags = [t.name.lstrip("#") for t in item.tags if _tag_kind(t) == "hashtag"]
    other_tags = [t.name for t in item.tags if _tag_kind(t) != "hashtag"]
    username = item.author.username if item.author is not None else None
    source = item.category_source
    return {
        "gramvault_id": item.id,
        "type": _media_type_value(item),
        "author": username,
        "account": f"@{username}" if username else None,
        "date": date_value.isoformat() if date_value else None,
        "saved_at": item.imported_at.isoformat() if item.imported_at else None,
        "category": item.category,
        "category_source": source.value if hasattr(source, "value") else source,
        "tags": other_tags,
        "hashtags": hashtags,
        "enrichment": {
            "transcript": any(mf.transcript for mf in item.media_files),
            "ocr": any(mf.ocr_text for mf in item.media_files),
            "vision": any(mf.vision_caption for mf in item.media_files),
        },
        "source_url": item.permalink,
    }


def render_frontmatter(frontmatter: dict[str, object]) -> str:
    yaml_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{yaml_text}\n---\n"


def _split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """`(frontmatter_dict, body)` for a note. `({}, text)` when there's no
    parseable frontmatter."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    try:
        data = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    rest = text[end + 4 :]
    return data, rest.removeprefix("\n")


def extract_gramvault_id(note_text: str) -> int | None:
    """Best-effort parse of the `gramvault_id` frontmatter field out of an
    existing note's text. Returns None if there's no parseable frontmatter
    or no `gramvault_id` key — never raises."""
    data, _ = _split_frontmatter(note_text)
    gramvault_id = data.get("gramvault_id")
    return gramvault_id if isinstance(gramvault_id, int) else None


def user_tail(existing_text: str) -> str:
    """The user-authored content after `%% gramvault:end %%`. Empty for a
    legacy (marker-less) note — its whole body was GramVault's."""
    _, body = _split_frontmatter(existing_text)
    if MANAGED_END in body:
        return body.split(MANAGED_END, 1)[1].strip("\n")
    return ""


def _build_body(item: Item, media_links: list[MediaLink]) -> str:
    sections: list[str] = []

    sections.append(item.caption.strip() if item.caption else "*No caption.*")

    vision_captions = [mf.vision_caption for mf in item.media_files if mf.vision_caption]
    if vision_captions:
        sections.append("## AI Description\n\n" + "\n".join(f"- {c}" for c in vision_captions))

    ocr_texts = [mf.ocr_text for mf in item.media_files if mf.ocr_text]
    if ocr_texts:
        sections.append("## On-screen text\n\n" + "\n\n".join(ocr_texts))

    transcripts = [mf.transcript for mf in item.media_files if mf.transcript]
    if transcripts:
        sections.append("## Transcript\n\n" + "\n\n".join(transcripts))

    if media_links:
        media_lines = []
        for link in media_links:
            label = _media_file_type_value(link.media_file)
            if link.poster:
                # Poster still + a plain link to the clip (§G6).
                media_lines.append(f"![[{link.poster}]]\n\n[▶ {label}]({link.target})")
            elif link.embed:
                media_lines.append(f"![[{link.target}]]")
            else:
                media_lines.append(f"[{label}]({link.target})")
        sections.append("## Media\n\n" + "\n\n".join(media_lines))

    if item.permalink:
        sections.append(f"[Original post]({item.permalink})")

    return "\n\n".join(sections) + "\n"


def build_note_markdown(
    item: Item,
    media_links: list[MediaLink] | None = None,
    *,
    existing_text: str | None = None,
) -> str:
    """Full Markdown note text for `item`.

    When `existing_text` is given, GramVault's frontmatter keys and the
    managed body region are regenerated while the user's extra frontmatter
    keys and everything after `%% gramvault:end %%` are carried over.
    """
    media_links = media_links or []
    frontmatter = build_frontmatter(item)
    tail = ""
    if existing_text:
        existing_fm, _ = _split_frontmatter(existing_text)
        for key, value in existing_fm.items():
            if key not in _OWNED_FRONTMATTER_KEYS:
                frontmatter[key] = value
        tail = user_tail(existing_text)

    body = _build_body(item, media_links)
    managed = f"{MANAGED_START}\n{body}{MANAGED_END}\n"
    doc = f"{render_frontmatter(frontmatter)}\n{managed}"
    if tail:
        doc += f"\n{tail}\n"
    return doc


__all__ = [
    "DASHBOARD_NOTE_FILENAME",
    "INDEX_NOTE_FILENAME",
    "MANAGED_END",
    "MANAGED_START",
    "Layout",
    "MediaLink",
    "build_frontmatter",
    "build_note_markdown",
    "extract_gramvault_id",
    "media_filename",
    "note_filename",
    "note_relpath",
    "render_frontmatter",
    "sanitize_filename_component",
    "user_tail",
]
