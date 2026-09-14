"""Tests for gramvault.ingestion.importer: end-to-end import of a fake
ZIP into the DB (authors/items/media_files), progress tracking on the
import_jobs row, dedupe-on-reimport, and media organization/dedupe by
hash.
"""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from gramvault.ai.keyframes import ffmpeg_available
from gramvault.config import Config
from gramvault.db.session import session_scope
from gramvault.ingestion.importer import (
    cancel_import_job,
    create_import_job,
    import_zip,
    run_import,
)
from gramvault.models.schemas import JobStatus

needs_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe not on PATH")


def _write_saved_export(tmp_path: Path, name: str = "export.zip") -> Path:
    entries = [
        {
            "title": "chef_alice",
            "string_list_data": [
                {"href": "https://www.instagram.com/p/ABC123abc/", "timestamp": 1700000000}
            ],
        },
        {
            "title": "traveler_bob",
            "string_list_data": [
                {"href": "https://www.instagram.com/reel/XYZ789xyz/", "timestamp": 1700100000}
            ],
        },
    ]
    zip_path = tmp_path / name
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "your_instagram_activity/saved/saved_posts.json",
            json.dumps({"saved_saved_media": entries}),
        )
    return zip_path


def _write_own_posts_export(tmp_path: Path, name: str = "own_export.zip") -> Path:
    posts_json = json.dumps(
        [
            {
                "title": "a carousel post",
                "creation_timestamp": 1700000000,
                "media": [
                    {"uri": "media/posts/202301/a.jpg", "creation_timestamp": 1700000000},
                    {"uri": "media/posts/202301/b.jpg", "creation_timestamp": 1700000001},
                ],
            }
        ]
    )
    zip_path = tmp_path / name
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("your_instagram_activity/media/posts_1.json", posts_json)
        # Real JPEG magic bytes (organizer.sniff_extension gates on content,
        # not the claimed .jpg extension — see audit finding S3).
        zf.writestr("media/posts/202301/a.jpg", b"\xff\xd8\xff\xe0fake jpeg bytes AAAA")
        zf.writestr("media/posts/202301/b.jpg", b"\xff\xd8\xff\xe0fake jpeg bytes BBBB")
    return zip_path


def test_import_zip_creates_authors_and_items(tmp_path: Path, tmp_config: Config) -> None:
    zip_path = _write_saved_export(tmp_path)

    job = import_zip(zip_path, tmp_config)

    assert job.status == JobStatus.DONE
    assert job.total_items == 2
    assert job.processed_items == 2
    assert job.failed_items == 0

    from gramvault.db.session import session_scope

    with session_scope(tmp_config) as conn:
        authors = {row["username"] for row in conn.execute("SELECT username FROM authors")}
        items = conn.execute("SELECT external_id, author_id, caption FROM items").fetchall()

    assert authors == {"chef_alice", "traveler_bob"}
    assert len(items) == 2
    # Saved (other users') items are link-only: no real caption available.
    assert all(row["caption"] is None for row in items)


def test_reimport_same_zip_dedupes_items(tmp_path: Path, tmp_config: Config) -> None:
    zip_path = _write_saved_export(tmp_path)

    first_job = import_zip(zip_path, tmp_config)
    second_job = import_zip(zip_path, tmp_config)

    assert first_job.status == JobStatus.DONE
    assert second_job.status == JobStatus.DONE
    # Second run should still report the items as "processed" (skipped, not
    # duplicated) rather than erroring.
    assert second_job.processed_items == 2
    assert second_job.failed_items == 0

    from gramvault.db.session import session_scope

    with session_scope(tmp_config) as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
        author_count = conn.execute("SELECT COUNT(*) AS c FROM authors").fetchone()["c"]

    assert count == 2  # not 4 -- no duplicates created
    assert author_count == 2


def test_import_own_posts_organizes_media_by_hash(tmp_path: Path, tmp_config: Config) -> None:
    zip_path = _write_own_posts_export(tmp_path)

    job = import_zip(zip_path, tmp_config)

    assert job.status == JobStatus.DONE
    assert job.total_items == 1
    assert job.processed_items == 1

    from gramvault.db.session import session_scope

    with session_scope(tmp_config) as conn:
        items = conn.execute("SELECT id, media_type, caption FROM items").fetchall()
        assert len(items) == 1
        assert items[0]["media_type"] == "carousel"
        assert items[0]["caption"] == "a carousel post"
        media_files = conn.execute(
            "SELECT file_path, checksum FROM media_files WHERE item_id = ? ORDER BY sequence_index",
            (items[0]["id"],),
        ).fetchall()

    assert len(media_files) == 2
    for row in media_files:
        media_path = tmp_config.resolved_library_dir / row["file_path"]
        assert media_path.is_file()
        assert row["checksum"] in row["file_path"]  # organized under a hash-based path


@needs_ffmpeg
def test_import_own_posts_populates_media_dimensions_via_ffprobe(
    tmp_path: Path, tmp_config: Config
) -> None:
    """R11: width/height/duration_seconds were in the schema but never
    populated — ffprobe is now called at import time (importer.py)."""
    jpeg_path = tmp_path / "real.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=32x24", "-frames:v", "1", str(jpeg_path)],
        capture_output=True,
        check=True,
    )
    real_jpeg_bytes = jpeg_path.read_bytes()

    posts_json = json.dumps(
        [
            {
                "title": "a real photo",
                "creation_timestamp": 1700000000,
                "media": [{"uri": "media/posts/202301/real.jpg", "creation_timestamp": 1700000000}],
            }
        ]
    )
    zip_path = tmp_path / "own_export_real.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("your_instagram_activity/media/posts_1.json", posts_json)
        zf.writestr("media/posts/202301/real.jpg", real_jpeg_bytes)

    job = import_zip(zip_path, tmp_config)
    assert job.status == JobStatus.DONE

    with session_scope(tmp_config) as conn:
        row = conn.execute(
            "SELECT width, height FROM media_files WHERE item_id = "
            "(SELECT id FROM items ORDER BY id DESC LIMIT 1)"
        ).fetchone()

    assert (row["width"], row["height"]) == (32, 24)


def test_reimport_own_posts_does_not_duplicate_media_bytes_on_disk(
    tmp_path: Path, tmp_config: Config
) -> None:
    zip_path = _write_own_posts_export(tmp_path)

    import_zip(zip_path, tmp_config)
    import_zip(zip_path, tmp_config)

    media_dir = tmp_config.resolved_library_dir / "media"
    all_files = [p for p in media_dir.rglob("*") if p.is_file()]
    # Exactly 2 unique files (a.jpg, b.jpg content), never duplicated.
    assert len(all_files) == 2


def test_import_job_status_transitions_and_progress(tmp_path: Path, tmp_config: Config) -> None:
    from gramvault.ingestion.importer import create_import_job, get_import_job, run_import

    zip_path = _write_saved_export(tmp_path)
    job = create_import_job(zip_path, tmp_config)
    assert job.status == JobStatus.PENDING
    assert job.id is not None

    finished = run_import(job.id, zip_path, tmp_config)
    assert finished.status == JobStatus.DONE
    assert finished.started_at is not None
    assert finished.finished_at is not None

    fetched = get_import_job(job.id, tmp_config)
    assert fetched is not None
    assert fetched.status == JobStatus.DONE
    assert fetched.progress_pct == 100.0


def test_import_bad_zip_marks_job_failed_with_friendly_message(
    tmp_path: Path, tmp_config: Config
) -> None:
    bad_zip = tmp_path / "bad_export.zip"
    bad_zip.write_bytes(b"not a real zip file at all")

    job = import_zip(bad_zip, tmp_config)

    assert job.status == JobStatus.FAILED
    assert job.error_message
    assert "zip" in job.error_message.lower()


def test_list_import_jobs_orders_most_recent_first(tmp_path: Path, tmp_config: Config) -> None:
    from gramvault.ingestion.importer import list_import_jobs

    zip_a = _write_saved_export(tmp_path, "a.zip")
    zip_b = _write_saved_export(tmp_path, "b.zip")
    job_a = import_zip(zip_a, tmp_config)
    job_b = import_zip(zip_b, tmp_config)

    jobs = list_import_jobs(tmp_config)

    assert jobs[0].id == job_b.id
    assert jobs[1].id == job_a.id


def _write_label_values_export(tmp_path: Path, name: str = "new_export.zip") -> Path:
    """An export in the newer `label_values` shape, which — unlike the
    older one — carries the real caption, the owner's display name, and
    hashtags."""
    entries = [
        {
            "timestamp": 1700000000,
            "media": [],
            "label_values": [
                {
                    "label": "URL",
                    "value": "https://www.instagram.com/reel/Da2_CrNoI9n/",
                    "href": "https://www.instagram.com/reel/Da2_CrNoI9n/",
                },
                {"label": "Caption", "value": "roladki z cukinii"},
                {
                    "title": "Hashtags",
                    "dict": [
                        {"title": "", "dict": [{"label": "Name", "value": "przepis"}]},
                        {"title": "", "dict": [{"label": "Name", "value": "obiad"}]},
                    ],
                },
                {
                    "title": "Owner",
                    "dict": [
                        {
                            "title": "",
                            "dict": [
                                {"label": "Name", "value": "Alice Cooks"},
                                {"label": "Username", "value": "chef_alice"},
                            ],
                        }
                    ],
                },
            ],
        }
    ]
    zip_path = tmp_path / name
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("your_instagram_activity/saved/saved_posts.json", json.dumps(entries))
    return zip_path


def test_import_label_values_export_stores_caption_author_and_tags(
    tmp_path: Path, tmp_config: Config
) -> None:
    zip_path = _write_label_values_export(tmp_path)

    import_zip(zip_path, tmp_config)

    with session_scope(tmp_config) as conn:
        item = conn.execute(
            "SELECT * FROM items WHERE external_id = ?", ("Da2_CrNoI9n",)
        ).fetchone()
        assert item is not None
        assert item["caption"] == "roladki z cukinii"
        assert item["media_type"] == "reel"
        assert item["permalink"] == "https://www.instagram.com/reel/Da2_CrNoI9n/"

        author = conn.execute(
            "SELECT * FROM authors WHERE id = ?", (item["author_id"],)
        ).fetchone()
        assert author["username"] == "chef_alice"
        assert author["full_name"] == "Alice Cooks"

        tags = conn.execute(
            """
            SELECT tags.name, tags.kind FROM tags
            JOIN item_tags ON item_tags.tag_id = tags.id
            WHERE item_tags.item_id = ?
            ORDER BY tags.name
            """,
            (item["id"],),
        ).fetchall()
        assert [(t["name"], t["kind"]) for t in tags] == [
            ("obiad", "hashtag"),
            ("przepis", "hashtag"),
        ]


# --- audit finding R2: cooperative cancellation -----------------------------


def _write_many_saved_export(tmp_path: Path, count: int) -> Path:
    entries = [
        {
            "title": "someone",
            "string_list_data": [
                {
                    "href": f"https://www.instagram.com/p/CANCEL{i:04d}/",
                    "timestamp": 1700000000 + i,
                }
            ],
        }
        for i in range(count)
    ]
    zip_path = tmp_path / "big_export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "your_instagram_activity/saved/saved_posts.json",
            json.dumps({"saved_saved_media": entries}),
        )
    return zip_path


def test_run_import_honours_cancel_requested_mid_run(tmp_path: Path, tmp_config: Config) -> None:
    """A cancel requested before/while run_import is looping stops it well
    short of the full item count, rather than the cancel flag only being
    checked (uselessly) after the whole loop finishes — the pre-fix
    behaviour, since import used to run synchronously to completion
    before a cancel request could ever be seen."""
    zip_path = _write_many_saved_export(tmp_path, 200)
    job = create_import_job(zip_path, tmp_config)
    assert job.id is not None

    # Simulate a cancel arriving before the run loop's first checkpoint.
    cancelled = cancel_import_job(job.id, tmp_config)
    assert cancelled is not None and cancelled.status == JobStatus.PENDING

    result = run_import(job.id, zip_path, tmp_config)

    assert result.status == JobStatus.FAILED
    assert result.error_message == "cancelled by user"
    assert result.processed_items < result.total_items
    assert result.total_items == 200


def test_reimport_label_values_export_dedupes(tmp_path: Path, tmp_config: Config) -> None:
    zip_path = _write_label_values_export(tmp_path)

    import_zip(zip_path, tmp_config)
    import_zip(zip_path, tmp_config)

    with session_scope(tmp_config) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM item_tags").fetchone()["n"] == 2
