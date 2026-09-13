"""Orchestrates a full export run: resolve/validate the vault path, write
one Markdown note per Item (idempotently), copy/link media, write the
index note.

Idempotency strategy (the important bit):
  - Every note's filename is deterministic from `note_filename()`, which
    embeds the item's stable `gramvault_id` (`Item.id`). Re-running an
    export for the same item always resolves to the same path, so writing
    the file again just overwrites it in place -- no `Item (1).md`
    duplicates, no need to diff old vs. new content.
  - As a belt-and-suspenders cleanup, before writing we also scan the
    target folder's existing notes' frontmatter for a `gramvault_id` that
    matches an item we're about to (re-)export. If a stale note is found
    at a *different* path than the freshly computed filename (e.g. the
    author's username or the post's date changed between exports, which
    would otherwise change the deterministic filename), we remove the
    stale file so it doesn't linger as an orphaned duplicate.

Vault path handling:
  - `config.paths.obsidian_vault_dir` unset -> `VaultNotConfiguredError`
    (nothing to export to).
  - Configured but the base directory doesn't exist on disk ->
    `VaultPathNotFoundError` (we refuse to silently create an arbitrary
    folder tree outside a vault the user actually pointed us at).
  - The target subfolder *inside* the vault (default: `GramVault/`) is
    created automatically if missing -- that part is expected to not
    exist yet on a first export.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from gramvault.config import Config
from gramvault.export.index_builder import IndexEntry, build_index_markdown
from gramvault.export.markdown_builder import (
    DASHBOARD_NOTE_FILENAME,
    INDEX_NOTE_FILENAME,
    MANAGED_END,
    MANAGED_START,
    MediaLink,
    build_note_markdown,
    extract_gramvault_id,
    media_filename,
    note_filename,
    note_relpath,
    render_frontmatter,
    sanitize_filename_component,
    user_tail,
)
from gramvault.export.moc_builder import (
    MOC_SUBDIR_NAME,
    build_moc_markdown,
    moc_filename,
    read_moc_meta,
)
from gramvault.export.overview_builder import build_dashboard_markdown
from gramvault.export.poster import poster_frame
from gramvault.models.schemas import Item, MediaFile

MEDIA_SUBDIR_NAME = "media"
DIGESTS_SUBDIR_NAME = "_digests"
_CITATION_RE = re.compile(r"\[\[item:(\d+)\]\]")


class VaultNotConfiguredError(Exception):
    """`config.paths.obsidian_vault_dir` is unset."""


class VaultPathNotFoundError(Exception):
    """The configured vault base directory doesn't exist / isn't a directory."""


class InvalidSubfolderError(Exception):
    """`vault_subfolder` (or `default_vault_subfolder`) isn't a plain relative
    folder path -- e.g. it tries to escape the vault with `..` or an
    absolute/drive-letter path."""


_SUBFOLDER_SEGMENT_RE = re.compile(r'^[^<>:"/\\|?*\x00-\x1f]{1,60}$')


def validate_subfolder(value: str) -> str:
    """Reject anything that isn't a plain relative folder path made of
    ordinary segments, so `resolve_export_target` can never be pointed
    outside the vault (`..`, a leading `/`, a drive letter like `C:\\`) or
    at the vault root itself (`.`/empty)."""
    normalized = value.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if (
        not parts
        or parts[0] in ("/", "")
        or any(p in (".", "..") or not _SUBFOLDER_SEGMENT_RE.match(p) for p in parts)
    ):
        raise InvalidSubfolderError(
            f"vault_subfolder must be a relative folder name like 'GramVault' "
            f"or 'Notes/GramVault': {value!r}"
        )
    return "/".join(parts)


@dataclass
class SkippedItem:
    item_id: int | None
    reason: str


@dataclass
class ExportResult:
    target_dir: Path
    notes_written: int = 0
    notes_updated: int = 0
    media_files_copied: int = 0
    skipped: list[SkippedItem] = field(default_factory=list)
    index_path: Path | None = None
    dashboard_path: Path | None = None
    moc_paths: list[Path] = field(default_factory=list)


def resolve_export_target(config: Config, vault_subfolder: str | None) -> Path:
    """Validate the configured vault path and return the (created) target
    subfolder within it. Raises `VaultNotConfiguredError` /
    `VaultPathNotFoundError` / `InvalidSubfolderError` per the module
    docstring."""
    vault_dir = config.resolved_obsidian_vault_dir
    if vault_dir is None:
        raise VaultNotConfiguredError(
            "No Obsidian vault configured (set paths.obsidian_vault_dir in config.yaml)"
        )
    if not vault_dir.is_dir():
        raise VaultPathNotFoundError(f"Configured Obsidian vault folder does not exist: {vault_dir}")

    subfolder = validate_subfolder(vault_subfolder or config.export.default_vault_subfolder)
    resolved_vault_dir = vault_dir.resolve()
    target_dir = (resolved_vault_dir / subfolder).resolve()
    if not target_dir.is_relative_to(resolved_vault_dir):
        raise InvalidSubfolderError(f"vault_subfolder escapes the configured vault: {vault_subfolder!r}")
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


_NON_ITEM_NOTES = {INDEX_NOTE_FILENAME, "GramVault Dashboard.md"}


def _scan_existing_notes_by_gramvault_id(target_dir: Path) -> dict[int, Path]:
    """Best-effort map of gramvault_id -> existing note path, built by
    reading frontmatter out of every `.md` file under `target_dir`
    (recursively, so a note filed under a `by-category` subfolder is
    found; skips the index/dashboard and the `_digests`/`_moc` folders).
    Used to clean up stale duplicates and to carry a note's user-authored
    tail when its path changes -- see the idempotency note in the module
    docstring."""
    mapping: dict[int, Path] = {}
    if not target_dir.is_dir():
        return mapping
    for path in target_dir.rglob("*.md"):
        if path.name in _NON_ITEM_NOTES or path.name == "media":
            continue
        if any(part in {"_digests", "_moc"} for part in path.relative_to(target_dir).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        gramvault_id = extract_gramvault_id(text)
        if gramvault_id is not None:
            mapping[gramvault_id] = path
    return mapping


def _resolve_media_src(config: Config, file_path: str) -> Path:
    """Absolute source path for a `MediaFile.file_path`, which is stored
    relative to the library dir (older rows may be absolute)."""
    path = Path(file_path).expanduser()
    if path.is_absolute():
        return path
    return config.resolved_library_dir / path


def _is_video(media_file: MediaFile) -> bool:
    media_type = media_file.media_type
    return (media_type.value if hasattr(media_type, "value") else str(media_type)) == "video"


def _poster_is_stale(poster_path: Path, src_path: Path) -> bool:
    if not poster_path.is_file():
        return True
    try:
        return poster_path.stat().st_mtime < src_path.stat().st_mtime
    except OSError:
        return True


def _copy_or_link_media(
    config: Config, item: Item, media_dir: Path, result: ExportResult
) -> list[MediaLink]:
    media_links: list[MediaLink] = []
    media_mode = config.export.media_mode
    for media_file in item.media_files:
        src_path = _resolve_media_src(config, media_file.file_path)
        if media_mode == "copy" and src_path.is_file():
            media_dir.mkdir(parents=True, exist_ok=True)
            dest_name = media_filename(item, media_file)
            try:
                shutil.copy2(src_path, media_dir / dest_name)
                result.media_files_copied += 1
            except OSError:
                media_links.append(MediaLink(media_file, str(src_path), embed=False))
                continue

            poster_target: str | None = None
            if _is_video(media_file):
                stem = dest_name.rsplit(".", 1)[0] if "." in dest_name else dest_name
                poster_name = f"{stem}.poster.jpg"
                poster_path = media_dir / poster_name
                if _poster_is_stale(poster_path, src_path):
                    poster_frame(src_path, poster_path)
                if poster_path.is_file():
                    poster_target = f"{MEDIA_SUBDIR_NAME}/{poster_name}"

            media_links.append(
                MediaLink(
                    media_file,
                    f"{MEDIA_SUBDIR_NAME}/{dest_name}",
                    embed=True,
                    poster=poster_target,
                )
            )
        else:
            media_links.append(MediaLink(media_file, str(src_path), embed=False))
    return media_links


def export_items(
    config: Config,
    items: list[Item],
    vault_subfolder: str | None = None,
    *,
    category_digests: dict[str, str] | None = None,
    prune_stale_mocs: bool = False,
) -> ExportResult:
    """Export `items` into the configured Obsidian vault. Idempotent --
    see module docstring.

    `category_digests` (category name -> raw digest markdown) embeds the
    latest digest for each category into its MOC note (§G3); the export
    job resolves it from the DB, direct callers may omit it.

    `prune_stale_mocs` deletes `_moc/<category>.md` files whose category
    has no items in this export (and which carry no user notes). Only safe
    on a whole-library export — the job passes it when `item_ids is None`.
    """
    target_dir = resolve_export_target(config, vault_subfolder)
    media_dir = target_dir / MEDIA_SUBDIR_NAME

    result = ExportResult(target_dir=target_dir)
    existing_notes_by_id = _scan_existing_notes_by_gramvault_id(target_dir)
    index_entries: list[IndexEntry] = []
    entries_by_category: dict[str, list[IndexEntry]] = {}

    layout = config.export.layout

    for item in items:
        if item.id is None:
            result.skipped.append(
                SkippedItem(item_id=None, reason="item has no id (not yet persisted to the DB)")
            )
            continue

        relpath = note_relpath(item, layout)
        filename = note_filename(item)
        note_path = target_dir / relpath
        note_path.parent.mkdir(parents=True, exist_ok=True)

        # A note for this gramvault_id may already exist under a different
        # path (author/date/category changed, or the layout changed). Carry
        # its user-authored tail to the new path, then remove the stale file.
        stale_path = existing_notes_by_id.get(item.id)
        source_text: str | None = None
        if note_path.exists():
            source_text = note_path.read_text(encoding="utf-8")
        elif stale_path is not None and stale_path.is_file():
            source_text = stale_path.read_text(encoding="utf-8")
        if stale_path is not None and stale_path != note_path:
            stale_path.unlink(missing_ok=True)

        existed_before = source_text is not None
        media_links = _copy_or_link_media(config, item, media_dir, result)
        note_text = build_note_markdown(item, media_links, existing_text=source_text)
        note_path.write_text(note_text, encoding="utf-8")

        if existed_before:
            result.notes_updated += 1
        else:
            result.notes_written += 1

        date_source = item.taken_at or item.imported_at
        entry = IndexEntry(
            item_id=item.id,
            note_filename=filename,
            author=item.author.username if item.author is not None else "unknown",
            media_type=item.media_type.value if hasattr(item.media_type, "value") else str(item.media_type),
            date=date_source.isoformat() if date_source else "",
            tags=[tag.name for tag in item.tags],
        )
        index_entries.append(entry)
        if item.category:
            entries_by_category.setdefault(item.category, []).append(entry)

    index_markdown = build_index_markdown(index_entries, subfolder_name=target_dir.name)
    index_path = target_dir / INDEX_NOTE_FILENAME
    index_path.write_text(index_markdown, encoding="utf-8")
    result.index_path = index_path

    dashboard_path = target_dir / DASHBOARD_NOTE_FILENAME
    dashboard_path.write_text(
        build_dashboard_markdown(items, subfolder_name=target_dir.name), encoding="utf-8"
    )
    result.dashboard_path = dashboard_path

    _write_category_mocs(
        target_dir,
        entries_by_category,
        items,
        category_digests or {},
        result,
        prune_stale=prune_stale_mocs,
    )

    return result


def _write_category_mocs(
    target_dir: Path,
    entries_by_category: dict[str, list[IndexEntry]],
    items: list[Item],
    category_digests: dict[str, str],
    result: ExportResult,
    *,
    prune_stale: bool = False,
) -> None:
    """One managed MOC note per category present in this export, under
    `<target>/_moc/`. Preserves each MOC's user tail (§G3). When
    `prune_stale`, also removes MOCs for categories that have no items in
    this (whole-library) export and no user notes."""
    moc_dir = target_dir / MOC_SUBDIR_NAME
    if not entries_by_category and not (prune_stale and moc_dir.is_dir()):
        return
    moc_dir.mkdir(parents=True, exist_ok=True)
    for category, entries in sorted(entries_by_category.items()):
        digest_md = category_digests.get(category)
        if digest_md:
            digest_md = _rewrite_citations(digest_md, items)
        path = moc_dir / moc_filename(category)
        existing = path.read_text(encoding="utf-8") if path.is_file() else None
        path.write_text(
            build_moc_markdown(
                category,
                entries,
                subfolder_name=target_dir.name,
                digest_markdown=digest_md,
                existing_text=existing,
            ),
            encoding="utf-8",
        )
        result.moc_paths.append(path)

    if not prune_stale:
        return
    live = set(entries_by_category)
    for path in moc_dir.glob("*.md"):
        try:
            category, has_tail = read_moc_meta(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if category is not None and category not in live and not has_tail:
            path.unlink(missing_ok=True)
            result.moc_paths = [p for p in result.moc_paths if p != path]


def _rewrite_citations(markdown: str, items: list[Item]) -> str:
    """`[[item:<id>]]` -> `[[<note basename>]]` so a digest's citations
    resolve to the exported item notes. An id with no matching item is
    left as-is (post-processing already dropped truly dangling ones)."""
    link_by_id = {
        item.id: note_filename(item).removesuffix(".md")
        for item in items
        if item.id is not None
    }
    return _CITATION_RE.sub(
        lambda m: f"[[{link_by_id[int(m.group(1))]}]]"
        if int(m.group(1)) in link_by_id
        else m.group(0),
        markdown,
    )


def export_digest(
    config: Config,
    *,
    digest_id: int,
    name: str,
    markdown: str,
    items: list[Item],
    template: str | None = None,
    model: str | None = None,
    vault_subfolder: str | None = None,
) -> Path:
    """Write a digest's Markdown into `<subfolder>/_digests/<name>.md` as a
    managed note (citations rewritten to `[[<item note>]]`). Idempotent on
    the slugified name; preserves any user content after the end marker."""
    target_dir = resolve_export_target(config, vault_subfolder)
    digests_dir = target_dir / DIGESTS_SUBDIR_NAME
    digests_dir.mkdir(parents=True, exist_ok=True)

    path = digests_dir / f"{sanitize_filename_component(name)}.md"
    tail = user_tail(path.read_text(encoding="utf-8")) if path.is_file() else ""

    frontmatter = render_frontmatter(
        {
            "gramvault_digest": digest_id,
            "template": template,
            "model": model,
            "generated": datetime.now().isoformat(timespec="seconds"),
        }
    )
    body = _rewrite_citations(markdown, items).strip()
    doc = f"{frontmatter}\n{MANAGED_START}\n# {name}\n\n{body}\n{MANAGED_END}\n"
    if tail:
        doc += f"\n{tail}\n"
    path.write_text(doc, encoding="utf-8")
    return path


__all__ = [
    "DIGESTS_SUBDIR_NAME",
    "MEDIA_SUBDIR_NAME",
    "ExportResult",
    "InvalidSubfolderError",
    "SkippedItem",
    "VaultNotConfiguredError",
    "VaultPathNotFoundError",
    "export_digest",
    "export_items",
    "resolve_export_target",
    "validate_subfolder",
]
