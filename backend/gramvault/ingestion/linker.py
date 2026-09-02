"""Attach locally-downloaded media to link-only saved items.

Instagram's export gives link-only metadata for other people's posts —
author, URL, caption, timestamp, but no media bytes (see
`parser.SavedItem`). So an imported library of saved reels renders as
cards with no thumbnail until the media is fetched separately, with
`instaloader` or a similar downloader.

This module closes that gap. It walks a directory of downloaded media,
matches each file back to an already-imported item by the Instagram
shortcode in its filename, brings the bytes into the content-addressed
library store (hardlinked by default — see
`organizer.organize_local_file`), and writes the `media_files` rows the
gallery reads.

Matching is deliberately driven by the shortcodes already in the
database rather than by a filename grammar: shortcodes themselves can
contain `_` and `-`, so a pattern like `_UTC_<shortcode>_<n>.jpg` can't
be split unambiguously on its own. Checking candidate splits against
known ids removes the guesswork, and means any downloader whose
filenames merely *contain* the shortcode works.

Two filename conventions are understood:
  - `<anything>_UTC_<shortcode>[_<n>].<ext>` — instaloader's output when
    `filename_pattern` ends with `{shortcode}`.
  - `<shortcode>[_<n>].<ext>` — a bare shortcode filename.
The optional `_<n>` is the position within a carousel.

Re-running is cheap and safe: items that already have at least as many
media files as this directory offers are skipped without re-hashing, and
the store dedupes by content hash regardless.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from gramvault.config import Config, get_config
from gramvault.db.session import session_scope
from gramvault.ingestion.organizer import organize_local_file
from gramvault.models.schemas import FileMediaType, MediaType

_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_MEDIA_EXTENSIONS = _VIDEO_EXTENSIONS | _PHOTO_EXTENSIONS

# Trailing "_12" on a filename stem: instaloader's index within a carousel.
_INDEX_SUFFIX_RE = re.compile(r"_(\d+)$")

# instaloader separates its date prefix from the rest with "_UTC_".
_UTC_SEPARATOR = "_UTC_"


@dataclass
class LinkReport:
    """Outcome of one `link_local_media` run."""

    files_scanned: int = 0
    files_matched: int = 0
    files_linked: int = 0
    items_linked: int = 0
    items_already_linked: int = 0
    unmatched_files: int = 0
    failed_files: int = 0
    unmatched_examples: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.files_linked} media file(s) linked to {self.items_linked} item(s); "
            f"{self.items_already_linked} item(s) already had media, "
            f"{self.unmatched_files} file(s) matched no imported item, "
            f"{self.failed_files} file(s) could not be read."
        )


@dataclass
class _MediaSlot:
    """One position within an item: a single photo, or a video plus the
    thumbnail instaloader saves next to it."""

    index: int
    photo: Path | None = None
    video: Path | None = None

    def chosen(self) -> tuple[Path, FileMediaType] | None:
        # A video's sibling .jpg is instaloader's thumbnail, not a separate
        # piece of content — storing both would show a still frame where
        # the gallery expects the reel.
        if self.video is not None:
            return self.video, FileMediaType.VIDEO
        if self.photo is not None:
            return self.photo, FileMediaType.PHOTO
        return None


def _split_index(stem: str) -> tuple[str, int]:
    """Split a trailing carousel index off a filename stem."""
    match = _INDEX_SUFFIX_RE.search(stem)
    if match is None:
        return stem, 0
    return stem[: match.start()], int(match.group(1))


def _candidate_shortcodes(stem: str) -> list[tuple[str, int]]:
    """Every (shortcode, index) reading of `stem`, most specific first.

    Shortcodes may end in `_` or a digit, so `..._UTC_ABC_1` is genuinely
    ambiguous — it could be shortcode `ABC_1` at index 0 or shortcode
    `ABC` at index 1. Both readings are returned; the caller keeps
    whichever one names an item that actually exists.
    """
    bases = [stem]
    if _UTC_SEPARATOR in stem:
        bases.append(stem.split(_UTC_SEPARATOR, 1)[1])

    candidates: list[tuple[str, int]] = []
    for base in bases:
        if base and (base, 0) not in candidates:
            candidates.append((base, 0))
        trimmed, index = _split_index(base)
        if index and trimmed and (trimmed, index) not in candidates:
            candidates.append((trimmed, index))
    return candidates


def _load_external_ids(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT id, external_id FROM items WHERE external_id IS NOT NULL"
    ).fetchall()
    return {row["external_id"]: row["id"] for row in rows}


def _existing_media_counts(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute(
        "SELECT item_id, COUNT(*) AS n FROM media_files GROUP BY item_id"
    ).fetchall()
    return {row["item_id"]: row["n"] for row in rows}


def _collect_slots(
    source_dir: Path, known_ids: dict[str, int]
) -> tuple[dict[int, dict[int, _MediaSlot]], LinkReport]:
    """Group every recognizable media file under `source_dir` by item id
    and carousel position."""
    report = LinkReport()
    slots: dict[int, dict[int, _MediaSlot]] = defaultdict(dict)

    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in _MEDIA_EXTENSIONS:
            continue  # .txt captions, .json.xz metadata, session files...

        report.files_scanned += 1

        match: tuple[int, int] | None = None
        for shortcode, index in _candidate_shortcodes(path.stem):
            item_id = known_ids.get(shortcode)
            if item_id is not None:
                match = (item_id, index)
                break

        if match is None:
            report.unmatched_files += 1
            if len(report.unmatched_examples) < 5:
                report.unmatched_examples.append(path.name)
            continue

        report.files_matched += 1
        item_id, index = match
        slot = slots[item_id].setdefault(index, _MediaSlot(index=index))
        if ext in _VIDEO_EXTENSIONS:
            slot.video = path
        else:
            slot.photo = path

    return slots, report


def _resolve_media_type(
    current: str, permalink: str | None, slots: list[_MediaSlot]
) -> str | None:
    """The item's media type given what was actually found on disk, or
    None to leave the imported guess alone.

    The importer can only guess from the URL (a `/reel/` link is a reel,
    everything else defaults to photo), so this is the first point where
    a carousel or a plain video can be told apart from a photo. Only
    upgrades are applied — finding one photo is never enough to overrule
    a `/reel/` permalink, since that photo may just be a thumbnail whose
    video failed to download.
    """
    if len(slots) > 1:
        resolved = MediaType.CAROUSEL.value
    elif slots and slots[0].video is not None:
        is_reel = current == MediaType.REEL.value or (permalink or "").find("/reel/") != -1
        resolved = MediaType.REEL.value if is_reel else MediaType.VIDEO.value
    else:
        return None
    return resolved if resolved != current else None


def link_local_media(
    source_dir: Path, config: Config | None = None, *, copy: bool = False
) -> LinkReport:
    """Link downloaded media under `source_dir` to already-imported items.

    Pass `copy=True` to force real copies instead of hardlinks (see
    `organizer.organize_local_file`).
    """
    config = config or get_config()
    if not source_dir.is_dir():
        raise NotADirectoryError(f"No such directory: {source_dir}")

    library_dir = config.resolved_library_dir
    library_dir.mkdir(parents=True, exist_ok=True)

    with session_scope(config) as conn:
        known_ids = _load_external_ids(conn)
        slots_by_item, report = _collect_slots(source_dir, known_ids)
        existing_counts = _existing_media_counts(conn)

        for item_id, slots in slots_by_item.items():
            ordered = [slots[index] for index in sorted(slots)]

            # Already linked in an earlier run — skip before hashing, which
            # is what makes re-running over a large directory cheap.
            if existing_counts.get(item_id, 0) >= len(ordered):
                report.items_already_linked += 1
                continue

            row = conn.execute(
                "SELECT media_type, permalink FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                continue

            linked_here = 0
            for sequence_index, slot in enumerate(ordered):
                chosen = slot.chosen()
                if chosen is None:
                    continue
                path, file_type = chosen

                organized = organize_local_file(path, library_dir, copy=copy)
                if organized is None:
                    report.failed_files += 1
                    continue

                already = conn.execute(
                    "SELECT 1 FROM media_files WHERE item_id = ? AND file_path = ?",
                    (item_id, organized.file_path),
                ).fetchone()
                if already is not None:
                    continue

                conn.execute(
                    """
                    INSERT INTO media_files
                        (item_id, file_path, media_type, sequence_index, checksum)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        item_id,
                        organized.file_path,
                        file_type.value,
                        sequence_index,
                        organized.checksum,
                    ),
                )
                linked_here += 1

            if linked_here:
                report.files_linked += linked_here
                report.items_linked += 1

            resolved = _resolve_media_type(row["media_type"], row["permalink"], ordered)
            if resolved is not None:
                conn.execute(
                    "UPDATE items SET media_type = ? WHERE id = ?", (resolved, item_id)
                )

    return report
