"""Tests for gramvault.ingestion.linker: matching separately downloaded
media back to link-only saved items by shortcode, hardlinking it into
the content-addressed store, correcting the imported media_type guess,
and staying idempotent across re-runs.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from gramvault.config import Config
from gramvault.db.session import session_scope
from gramvault.ingestion.importer import import_zip
from gramvault.ingestion.linker import link_local_media


def _write_export(tmp_path: Path, entries: list[dict], name: str = "export.zip") -> Path:
    zip_path = tmp_path / name
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "your_instagram_activity/saved/saved_posts.json", json.dumps(entries)
        )
    return zip_path


def _saved_entry(url: str, username: str = "chef_alice") -> dict:
    return {
        "timestamp": 1700000000,
        "media": [],
        "label_values": [
            {"label": "URL", "value": url, "href": url},
            {"label": "Caption", "value": "a caption"},
            {
                "title": "Owner",
                "dict": [{"title": "", "dict": [{"label": "Username", "value": username}]}],
            },
        ],
    }


def _downloads(tmp_path: Path, files: dict[str, bytes]) -> Path:
    source = tmp_path / "downloads"
    source.mkdir(exist_ok=True)
    for name, content in files.items():
        (source / name).write_bytes(content)
    return source


def _media_rows(config: Config, external_id: str) -> list[dict]:
    with session_scope(config) as conn:
        rows = conn.execute(
            """
            SELECT media_files.* FROM media_files
            JOIN items ON items.id = media_files.item_id
            WHERE items.external_id = ?
            ORDER BY media_files.sequence_index
            """,
            (external_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def _item_media_type(config: Config, external_id: str) -> str:
    with session_scope(config) as conn:
        row = conn.execute(
            "SELECT media_type FROM items WHERE external_id = ?", (external_id,)
        ).fetchone()
    assert row is not None
    return row["media_type"]


def test_link_media_attaches_video_to_saved_reel(tmp_path: Path, tmp_config: Config) -> None:
    zip_path = _write_export(
        tmp_path, [_saved_entry("https://www.instagram.com/reel/REEL123xyz/")]
    )
    import_zip(zip_path, tmp_config)
    source = _downloads(
        tmp_path, {"2024-01-02_03-04-05_UTC_REEL123xyz.mp4": b"fake mp4 bytes"}
    )

    report = link_local_media(source, tmp_config)

    assert report.files_linked == 1
    assert report.items_linked == 1
    assert report.unmatched_files == 0

    rows = _media_rows(tmp_config, "REEL123xyz")
    assert len(rows) == 1
    assert rows[0]["media_type"] == "video"
    assert rows[0]["file_path"].startswith("media/")
    assert (tmp_config.resolved_library_dir / rows[0]["file_path"]).exists()


def test_link_media_prefers_video_over_its_thumbnail(
    tmp_path: Path, tmp_config: Config
) -> None:
    """instaloader saves a .jpg next to each video; storing it as a
    separate media file would show a still where the reel belongs."""
    zip_path = _write_export(
        tmp_path, [_saved_entry("https://www.instagram.com/reel/REEL123xyz/")]
    )
    import_zip(zip_path, tmp_config)
    source = _downloads(
        tmp_path,
        {
            "2024-01-02_03-04-05_UTC_REEL123xyz.mp4": b"fake mp4 bytes",
            "2024-01-02_03-04-05_UTC_REEL123xyz.jpg": b"fake thumbnail bytes",
        },
    )

    link_local_media(source, tmp_config)

    rows = _media_rows(tmp_config, "REEL123xyz")
    assert len(rows) == 1
    assert rows[0]["media_type"] == "video"


def test_link_media_orders_carousel_files_by_index(
    tmp_path: Path, tmp_config: Config
) -> None:
    zip_path = _write_export(
        tmp_path, [_saved_entry("https://www.instagram.com/p/CAROUSEL01/")]
    )
    import_zip(zip_path, tmp_config)
    source = _downloads(
        tmp_path,
        {
            "2024-01-02_03-04-05_UTC_CAROUSEL01_1.jpg": b"slide one",
            "2024-01-02_03-04-05_UTC_CAROUSEL01_2.jpg": b"slide two",
            "2024-01-02_03-04-05_UTC_CAROUSEL01_3.jpg": b"slide three",
        },
    )

    link_local_media(source, tmp_config)

    rows = _media_rows(tmp_config, "CAROUSEL01")
    assert [row["sequence_index"] for row in rows] == [0, 1, 2]
    assert len({row["checksum"] for row in rows}) == 3
    assert _item_media_type(tmp_config, "CAROUSEL01") == "carousel"


def test_link_media_upgrades_photo_guess_to_video(
    tmp_path: Path, tmp_config: Config
) -> None:
    """A non-/reel/ permalink imports as 'photo'; finding a video file is
    the first evidence of what it actually is."""
    zip_path = _write_export(tmp_path, [_saved_entry("https://www.instagram.com/p/VID123abcd/")])
    import_zip(zip_path, tmp_config)
    assert _item_media_type(tmp_config, "VID123abcd") == "photo"

    source = _downloads(tmp_path, {"2024-01-02_03-04-05_UTC_VID123abcd.mp4": b"fake mp4"})
    link_local_media(source, tmp_config)

    assert _item_media_type(tmp_config, "VID123abcd") == "video"


def test_link_media_keeps_reel_type_when_only_thumbnail_present(
    tmp_path: Path, tmp_config: Config
) -> None:
    """A lone .jpg for a /reel/ post means the video download failed —
    don't downgrade the item to a photo on that evidence."""
    zip_path = _write_export(
        tmp_path, [_saved_entry("https://www.instagram.com/reel/REEL123xyz/")]
    )
    import_zip(zip_path, tmp_config)
    source = _downloads(tmp_path, {"2024-01-02_03-04-05_UTC_REEL123xyz.jpg": b"thumb"})

    link_local_media(source, tmp_config)

    assert _item_media_type(tmp_config, "REEL123xyz") == "reel"


def test_link_media_hardlinks_without_a_second_copy(
    tmp_path: Path, tmp_config: Config
) -> None:
    zip_path = _write_export(
        tmp_path, [_saved_entry("https://www.instagram.com/reel/REEL123xyz/")]
    )
    import_zip(zip_path, tmp_config)
    source = _downloads(tmp_path, {"2024-01-02_03-04-05_UTC_REEL123xyz.mp4": b"fake mp4"})
    original = source / "2024-01-02_03-04-05_UTC_REEL123xyz.mp4"

    link_local_media(source, tmp_config)

    stored = tmp_config.resolved_library_dir / _media_rows(tmp_config, "REEL123xyz")[0]["file_path"]
    assert stored.stat().st_ino == original.stat().st_ino


def test_link_media_copy_mode_makes_an_independent_file(
    tmp_path: Path, tmp_config: Config
) -> None:
    zip_path = _write_export(
        tmp_path, [_saved_entry("https://www.instagram.com/reel/REEL123xyz/")]
    )
    import_zip(zip_path, tmp_config)
    source = _downloads(tmp_path, {"2024-01-02_03-04-05_UTC_REEL123xyz.mp4": b"fake mp4"})
    original = source / "2024-01-02_03-04-05_UTC_REEL123xyz.mp4"

    link_local_media(source, tmp_config, copy=True)

    stored = tmp_config.resolved_library_dir / _media_rows(tmp_config, "REEL123xyz")[0]["file_path"]
    assert stored.read_bytes() == original.read_bytes()
    assert stored.stat().st_ino != original.stat().st_ino


def test_link_media_is_idempotent(tmp_path: Path, tmp_config: Config) -> None:
    zip_path = _write_export(
        tmp_path, [_saved_entry("https://www.instagram.com/reel/REEL123xyz/")]
    )
    import_zip(zip_path, tmp_config)
    source = _downloads(tmp_path, {"2024-01-02_03-04-05_UTC_REEL123xyz.mp4": b"fake mp4"})

    link_local_media(source, tmp_config)
    second = link_local_media(source, tmp_config)

    assert second.files_linked == 0
    assert second.items_already_linked == 1
    assert len(_media_rows(tmp_config, "REEL123xyz")) == 1


def test_link_media_reports_files_with_no_matching_item(
    tmp_path: Path, tmp_config: Config
) -> None:
    zip_path = _write_export(
        tmp_path, [_saved_entry("https://www.instagram.com/reel/REEL123xyz/")]
    )
    import_zip(zip_path, tmp_config)
    source = _downloads(
        tmp_path,
        {
            "2024-01-02_03-04-05_UTC_REEL123xyz.mp4": b"fake mp4",
            "2024-01-02_03-04-05_UTC_NOTMINE999.mp4": b"someone else's",
            "2024-01-02_03-04-05_UTC_REEL123xyz.txt": b"caption sidecar",
        },
    )

    report = link_local_media(source, tmp_config)

    assert report.files_scanned == 2  # the .txt sidecar isn't media
    assert report.files_linked == 1
    assert report.unmatched_files == 1
    assert report.unmatched_examples == ["2024-01-02_03-04-05_UTC_NOTMINE999.mp4"]


def test_link_media_matches_shortcode_that_looks_like_an_index(
    tmp_path: Path, tmp_config: Config
) -> None:
    """Shortcodes may end in `_<digit>`, which is also the carousel index
    suffix. The real shortcode wins because it names an actual item."""
    zip_path = _write_export(tmp_path, [_saved_entry("https://www.instagram.com/p/ABCdef_1/")])
    import_zip(zip_path, tmp_config)
    source = _downloads(tmp_path, {"2024-01-02_03-04-05_UTC_ABCdef_1.jpg": b"a photo"})

    report = link_local_media(source, tmp_config)

    assert report.files_linked == 1
    assert _media_rows(tmp_config, "ABCdef_1")[0]["sequence_index"] == 0


def test_link_media_accepts_bare_shortcode_filenames(
    tmp_path: Path, tmp_config: Config
) -> None:
    zip_path = _write_export(
        tmp_path, [_saved_entry("https://www.instagram.com/reel/REEL123xyz/")]
    )
    import_zip(zip_path, tmp_config)
    source = _downloads(tmp_path, {"REEL123xyz.mp4": b"fake mp4"})

    assert link_local_media(source, tmp_config).files_linked == 1


def test_link_media_rejects_a_missing_directory(tmp_path: Path, tmp_config: Config) -> None:
    with pytest.raises(NotADirectoryError):
        link_local_media(tmp_path / "nope", tmp_config)
