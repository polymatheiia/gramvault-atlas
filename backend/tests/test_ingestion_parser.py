"""Tests for gramvault.ingestion.parser: locating + parsing the
saved-posts/own-posts JSON inside a (fake, in-memory) Instagram export
ZIP, and the friendly-failure paths (wrong format, HTML export, garbage
ZIP).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from gramvault.ingestion.parser import ExportFormatError, MediaType, parse_export


def _write_zip(tmp_path: Path, name: str, members: dict[str, bytes | str]) -> Path:
    zip_path = tmp_path / name
    with zipfile.ZipFile(zip_path, "w") as zf:
        for member_name, content in members.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            zf.writestr(member_name, data)
    return zip_path


def _saved_posts_json(entries: list[dict]) -> str:
    return json.dumps({"saved_saved_media": entries})


def test_parse_export_normal_case(tmp_path: Path) -> None:
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
    zip_path = _write_zip(
        tmp_path,
        "export.zip",
        {"your_instagram_activity/saved/saved_posts.json": _saved_posts_json(entries)},
    )

    parsed = parse_export(zip_path)

    assert len(parsed.saved_items) == 2
    first = parsed.saved_items[0]
    assert first.author_username == "chef_alice"
    assert first.external_id == "ABC123abc"
    assert first.instagram_url == "https://www.instagram.com/p/ABC123abc/"
    assert first.saved_at is not None
    assert first.media_type_guess == MediaType.PHOTO

    second = parsed.saved_items[1]
    assert second.media_type_guess == MediaType.REEL
    assert second.external_id == "XYZ789xyz"


def test_parse_export_reorganized_folder_layout(tmp_path: Path) -> None:
    """Instagram has moved saved_posts.json around across export versions
    -- the parser should find it via a broad glob rather than one exact
    hardcoded path."""
    entries = [
        {
            "title": "someone",
            "string_list_data": [
                {"href": "https://www.instagram.com/p/DEF456def/", "timestamp": 1700000000}
            ],
        }
    ]
    zip_path = _write_zip(
        tmp_path,
        "export.zip",
        {"connections/saved/saved_posts.json": _saved_posts_json(entries)},
    )

    parsed = parse_export(zip_path)

    assert len(parsed.saved_items) == 1
    assert parsed.saved_items[0].external_id == "DEF456def"


def test_parse_export_missing_shortcode_falls_back_to_synthetic_id(tmp_path: Path) -> None:
    entries = [
        {
            "title": "someone",
            "string_list_data": [{"href": "https://www.instagram.com/weird/url/", "timestamp": None}],
        }
    ]
    zip_path = _write_zip(
        tmp_path,
        "export.zip",
        {"your_instagram_activity/saved/saved_posts.json": _saved_posts_json(entries)},
    )

    parsed = parse_export(zip_path)

    assert len(parsed.saved_items) == 1
    assert parsed.saved_items[0].external_id is not None
    assert parsed.saved_items[0].external_id.startswith("url:")
    assert parsed.saved_items[0].saved_at is None


def test_parse_export_no_recognizable_content_raises_friendly_error(tmp_path: Path) -> None:
    zip_path = _write_zip(
        tmp_path,
        "export.zip",
        {"some_other_folder/unrelated.json": json.dumps({"foo": "bar"})},
    )

    with pytest.raises(ExportFormatError) as exc_info:
        parse_export(zip_path)

    message = str(exc_info.value)
    assert "saved-posts" in message.lower() or "posts" in message.lower()
    assert "unrelated.json" in message  # lists what was actually found


def test_parse_export_html_format_raises_reexport_error(tmp_path: Path) -> None:
    zip_path = _write_zip(
        tmp_path,
        "export.zip",
        {"your_instagram_activity/saved/saved_posts.html": "<html><body>fake</body></html>"},
    )

    with pytest.raises(ExportFormatError) as exc_info:
        parse_export(zip_path)

    message = str(exc_info.value).lower()
    assert "html" in message
    assert "json" in message  # tells the user to re-export as JSON


def test_parse_export_not_a_zip_raises_friendly_error(tmp_path: Path) -> None:
    not_a_zip = tmp_path / "export.zip"
    not_a_zip.write_bytes(b"this is definitely not a zip file")

    with pytest.raises(ExportFormatError) as exc_info:
        parse_export(not_a_zip)

    assert "zip" in str(exc_info.value).lower()


def test_parse_export_missing_file_raises_friendly_error(tmp_path: Path) -> None:
    with pytest.raises(ExportFormatError):
        parse_export(tmp_path / "does_not_exist.zip")


def test_parse_export_corrupt_json_raises_friendly_error(tmp_path: Path) -> None:
    zip_path = _write_zip(
        tmp_path,
        "export.zip",
        {"your_instagram_activity/saved/saved_posts.json": "{not valid json"},
    )

    with pytest.raises(ExportFormatError) as exc_info:
        parse_export(zip_path)

    assert "json" in str(exc_info.value).lower()


def test_parse_export_own_posts_with_carousel_media(tmp_path: Path) -> None:
    posts_json = json.dumps(
        [
            {
                "title": "my caption here",
                "creation_timestamp": 1700000000,
                "media": [
                    {"uri": "media/posts/202301/photo1.jpg", "creation_timestamp": 1700000000},
                    {"uri": "media/posts/202301/photo2.jpg", "creation_timestamp": 1700000001},
                ],
            }
        ]
    )
    zip_path = _write_zip(
        tmp_path,
        "export.zip",
        {
            "your_instagram_activity/media/posts_1.json": posts_json,
            "media/posts/202301/photo1.jpg": b"\xff\xd8\xff fake jpeg bytes 1",
            "media/posts/202301/photo2.jpg": b"\xff\xd8\xff fake jpeg bytes 2",
        },
    )

    parsed = parse_export(zip_path)

    assert len(parsed.own_posts) == 1
    post = parsed.own_posts[0]
    assert post.caption == "my caption here"
    assert post.media_type == MediaType.CAROUSEL
    assert len(post.media_files) == 2
    assert all(m.zip_member_name is not None for m in post.media_files)


def test_parse_export_own_post_with_missing_media_keeps_metadata(tmp_path: Path) -> None:
    posts_json = json.dumps(
        [
            {
                "title": "orphaned caption",
                "creation_timestamp": 1700000000,
                "media": [{"uri": "media/posts/202301/missing.jpg", "creation_timestamp": 1700000000}],
            }
        ]
    )
    zip_path = _write_zip(
        tmp_path, "export.zip", {"your_instagram_activity/media/posts_1.json": posts_json}
    )

    parsed = parse_export(zip_path)

    assert len(parsed.own_posts) == 1
    assert parsed.own_posts[0].media_files[0].zip_member_name is None
    assert any("missing" in w.lower() or "media" in w.lower() for w in parsed.warnings)


# --- the newer `label_values` saved-posts shape ---------------------------


def _label_values_entry(
    url: str,
    *,
    caption: str = "",
    username: str = "chef_alice",
    full_name: str = "",
    hashtags: tuple[str, ...] = (),
    timestamp: int = 1700000000,
) -> dict:
    """One saved-posts entry in the shape newer exports use: a flat list
    of label/value records plus nested `dict` groups."""
    return {
        "timestamp": timestamp,
        "media": [],
        "fbid": "18071163821445383",
        "label_values": [
            {"label": "URL", "value": url, "href": url},
            {"label": "Caption", "value": caption},
            {"label": "Title", "value": ""},
            {
                "title": "Hashtags",
                "dict": [
                    {"title": "", "dict": [{"label": "Name", "value": tag}]}
                    for tag in hashtags
                ],
            },
            {
                "title": "Owner",
                "dict": [
                    {
                        "title": "",
                        "dict": [
                            {"label": "URL", "value": ""},
                            {"label": "Name", "value": full_name},
                            {"label": "Username", "value": username},
                        ],
                    }
                ],
            },
            {"title": "Brand partner", "dict": []},
        ],
    }


def test_parse_export_label_values_shape(tmp_path: Path) -> None:
    entries = [
        _label_values_entry(
            "https://www.instagram.com/reel/Da2_CrNoI9n/",
            caption="a real caption",
            username="chef_alice",
            full_name="Alice",
            hashtags=("food", "recipe"),
        )
    ]
    zip_path = _write_zip(
        tmp_path,
        "export.zip",
        {"your_instagram_activity/saved/saved_posts.json": json.dumps(entries)},
    )

    parsed = parse_export(zip_path)

    assert len(parsed.saved_items) == 1
    item = parsed.saved_items[0]
    assert item.external_id == "Da2_CrNoI9n"
    assert item.instagram_url == "https://www.instagram.com/reel/Da2_CrNoI9n/"
    assert item.media_type_guess == MediaType.REEL
    assert item.author_username == "chef_alice"
    assert item.author_full_name == "Alice"
    assert item.caption == "a real caption"
    assert item.hashtags == ["food", "recipe"]
    assert item.saved_at is not None


def _as_mojibake(text: str) -> str:
    """Reproduce the export's double-encoding exactly: UTF-8 bytes read
    back as Latin-1. Expressed as a round-trip rather than a literal
    because the damaged form contains unprintable bytes — U+2022 becomes
    'â\\x80¢', not the 'â¢' it looks like in a terminal."""
    return text.encode("utf-8").decode("latin-1")


def test_parse_export_label_values_repairs_mojibake(tmp_path: Path) -> None:
    """These exports write UTF-8 bytes that were decoded as Latin-1, so
    "które" arrives as "ktÃ³re"."""
    entries = [
        _label_values_entry(
            "https://www.instagram.com/p/ABC123abc/",
            caption=_as_mojibake("produkty, które polecam"),
            full_name=_as_mojibake("Monika • content creator"),
            hashtags=(_as_mojibake("książki"),),
        )
    ]
    zip_path = _write_zip(
        tmp_path,
        "export.zip",
        {"your_instagram_activity/saved/saved_posts.json": json.dumps(entries)},
    )

    item = parse_export(zip_path).saved_items[0]

    assert item.caption == "produkty, które polecam"
    assert item.author_full_name == "Monika • content creator"
    assert item.hashtags == ["książki"]


def test_parse_export_label_values_leaves_clean_text_alone(tmp_path: Path) -> None:
    """Text that isn't mojibake must survive the repair untouched."""
    entries = [
        _label_values_entry(
            "https://www.instagram.com/p/ABC123abc/", caption="café and naïve — fine"
        )
    ]
    zip_path = _write_zip(
        tmp_path,
        "export.zip",
        {"your_instagram_activity/saved/saved_posts.json": json.dumps(entries)},
    )

    assert parse_export(zip_path).saved_items[0].caption == "café and naïve — fine"


def test_parse_export_label_values_missing_optional_groups(tmp_path: Path) -> None:
    """Entries without Owner/Hashtags/Caption still parse — only the URL
    is load-bearing."""
    entries = [
        {
            "timestamp": 1700000000,
            "label_values": [
                {"label": "URL", "value": "https://www.instagram.com/p/ABC123abc/"}
            ],
        }
    ]
    zip_path = _write_zip(
        tmp_path,
        "export.zip",
        {"your_instagram_activity/saved/saved_posts.json": json.dumps(entries)},
    )

    item = parse_export(zip_path).saved_items[0]

    assert item.external_id == "ABC123abc"
    assert item.author_username is None
    assert item.caption is None
    assert item.hashtags == []


def test_parse_export_handles_both_shapes_in_one_file(tmp_path: Path) -> None:
    """Shape is chosen per entry, so a transitional export mixing both
    still imports completely."""
    entries = [
        {
            "title": "traveler_bob",
            "string_list_data": [
                {"href": "https://www.instagram.com/p/OLD123abcd/", "timestamp": 1700000000}
            ],
        },
        _label_values_entry("https://www.instagram.com/reel/NEW123abcd/", caption="new"),
    ]
    zip_path = _write_zip(
        tmp_path,
        "export.zip",
        {"your_instagram_activity/saved/saved_posts.json": json.dumps(entries)},
    )

    parsed = parse_export(zip_path)

    assert [i.external_id for i in parsed.saved_items] == ["OLD123abcd", "NEW123abcd"]
    assert parsed.saved_items[0].author_username == "traveler_bob"
    assert parsed.saved_items[0].caption is None
    assert parsed.saved_items[1].caption == "new"


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(document.cookie)",
        "data:text/html,<script>alert(1)</script>",
        "https://evil.example/phishing",
        "http://instagram.com.evil.example/p/ABC/",  # lookalike host, not instagram.com
    ],
)
def test_non_instagram_href_is_dropped_not_stored(tmp_path: Path, href: str) -> None:
    # Regression for audit finding S10: `href` is attacker-influenced
    # export data rendered as `<a href>` with no further validation in the
    # frontend, so only a real instagram.com URL may survive parsing.
    entries = [{"title": "someone", "string_list_data": [{"href": href, "timestamp": 1700000000}]}]
    zip_path = _write_zip(
        tmp_path,
        "export.zip",
        {"your_instagram_activity/saved/saved_posts.json": _saved_posts_json(entries)},
    )

    parsed = parse_export(zip_path)

    assert parsed.saved_items[0].instagram_url is None


def test_real_instagram_url_is_kept(tmp_path: Path) -> None:
    entries = [
        {
            "title": "someone",
            "string_list_data": [
                {"href": "https://www.instagram.com/p/ABC123abc/", "timestamp": 1700000000}
            ],
        }
    ]
    zip_path = _write_zip(
        tmp_path,
        "export.zip",
        {"your_instagram_activity/saved/saved_posts.json": _saved_posts_json(entries)},
    )

    parsed = parse_export(zip_path)

    assert parsed.saved_items[0].instagram_url == "https://www.instagram.com/p/ABC123abc/"
