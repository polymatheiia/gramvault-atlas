"""Load the hand-labelled classifier eval set.

The dataset is the 1814 saved items the reels-workflow run categorised by
hand (`categories_final.csv`), joined to their full enriched text
(`items_raw.json` — caption, hashtags, transcript, vision caption). It is
**real personal Instagram data**, so per the project's privacy rules it is
never committed. Point the harness at your local copies with:

    export GRAMVAULT_EVAL_LABELS=~/reels-workflow/categories_final.csv
    export GRAMVAULT_EVAL_ITEMS=~/reels-workflow/items_raw.json

or drop the two files into `backend/tests/eval/data/` (gitignored). When
neither is present the eval tests skip.
"""

from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path

from gramvault.models.schemas import Author, Item, MediaFile, Tag

_HERE = Path(__file__).parent
_DATA = _HERE / "data"

LABELS_PATH = Path(os.environ.get("GRAMVAULT_EVAL_LABELS", _DATA / "categories_final.csv"))
ITEMS_PATH = Path(os.environ.get("GRAMVAULT_EVAL_ITEMS", _DATA / "items_raw.json"))

EVAL_DATA_AVAILABLE = LABELS_PATH.is_file() and ITEMS_PATH.is_file()


def missing_reason() -> str:
    missing = [str(p) for p in (LABELS_PATH, ITEMS_PATH) if not p.is_file()]
    return (
        "classifier eval data not found: "
        + ", ".join(missing)
        + " — set GRAMVAULT_EVAL_LABELS / GRAMVAULT_EVAL_ITEMS (see loader.py docstring)"
    )


def _split_hashtags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [tag.lstrip("#").strip() for tag in re.split(r"[\s,]+", raw) if tag.strip()]


def _record_to_item(rec: dict) -> Item:
    item_id = int(rec["id"])
    media_files: list[MediaFile] = []
    transcript = rec.get("transcript")
    vision = rec.get("vision") or rec.get("vision_caption")
    ocr = rec.get("ocr_text")
    if transcript or vision or ocr:
        media_files.append(
            MediaFile(
                id=item_id,
                item_id=item_id,
                file_path=f"{rec.get('external_id', item_id)}.bin",
                media_type="video",
                transcript=transcript,
                vision_caption=vision,
                ocr_text=ocr,
            )
        )
    username = rec.get("username")
    return Item(
        id=item_id,
        external_id=rec.get("external_id"),
        media_type=rec.get("media_type") or "reel",
        caption=rec.get("caption"),
        permalink=rec.get("permalink"),
        author=Author(username=username) if username else None,
        tags=[Tag(name=name, kind="hashtag") for name in _split_hashtags(rec.get("hashtags"))],
        media_files=media_files,
    )


def load_eval_set() -> list[tuple[Item, str]]:
    """Return `(item, gold_category)` pairs. Raises if the data is absent —
    guard callers with `EVAL_DATA_AVAILABLE` / `pytest.skip(missing_reason())`."""
    if not EVAL_DATA_AVAILABLE:
        raise FileNotFoundError(missing_reason())

    raw = json.loads(ITEMS_PATH.read_text(encoding="utf-8"))
    records = raw.values() if isinstance(raw, dict) else raw
    items_by_id = {int(rec["id"]): _record_to_item(rec) for rec in records}

    pairs: list[tuple[Item, str]] = []
    with LABELS_PATH.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            gold = (row.get("category") or "").strip()
            item = items_by_id.get(int(row["id"]))
            if item is not None and gold:
                pairs.append((item, gold))
    return pairs
