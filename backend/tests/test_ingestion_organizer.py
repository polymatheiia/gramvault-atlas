"""Regression tests for audit finding S3: the media organizer must sniff
magic bytes and reject anything that isn't a recognised image/video,
regardless of what extension the zip member (or the export JSON) claims."""

from __future__ import annotations

import zipfile
from pathlib import Path

from gramvault.ingestion.organizer import (
    ALLOWED_MEDIA_EXTENSIONS,
    organize_zip_member,
    sniff_extension,
)

_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
_GIF = b"GIF89a" + b"\x00" * 20
_HTML = b"<html><script>alert(document.cookie)</script></html>"
_EXE = b"MZ\x90\x00" + b"\x00" * 20


def _iso_bmff(brand: bytes) -> bytes:
    # size(4) + "ftyp"(4) + major_brand(4) + ...
    return b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 20


def test_sniff_extension_identifies_real_media() -> None:
    assert sniff_extension(_JPEG) == ".jpg"
    assert sniff_extension(_PNG) == ".png"
    assert sniff_extension(_GIF) == ".gif"


def test_sniff_extension_distinguishes_iso_bmff_brands() -> None:
    # Regression: HEIC/HEIF and .mov share the same ISO-BMFF container as
    # MP4 — the major brand at bytes 8:12 is the only way to tell them
    # apart. Collapsing everything to ".mp4" stores an iPhone photo as an
    # unplayable "video".
    assert sniff_extension(_iso_bmff(b"heic")) == ".heic"
    assert sniff_extension(_iso_bmff(b"mif1")) == ".heif"
    assert sniff_extension(_iso_bmff(b"qt  ")) == ".mov"
    assert sniff_extension(_iso_bmff(b"M4V ")) == ".m4v"
    assert sniff_extension(_iso_bmff(b"isom")) == ".mp4"


def test_sniff_extension_rejects_non_media() -> None:
    assert sniff_extension(_HTML) is None
    assert sniff_extension(_EXE) is None
    assert sniff_extension(b"") is None


def test_organize_zip_member_rejects_html_disguised_as_jpg(tmp_path: Path) -> None:
    zip_path = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("media/x.jpg", _HTML)
    library_dir = tmp_path / "library"
    with zipfile.ZipFile(zip_path) as zf:
        result = organize_zip_member(zf, "media/x.jpg", library_dir)
    assert result is None
    assert not (library_dir / "media").exists() or not any((library_dir / "media").rglob("*"))


def test_organize_zip_member_rejects_svg_and_exe(tmp_path: Path) -> None:
    zip_path = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("media/x.svg", b"<svg onload=alert(1)></svg>")
        zf.writestr("media/x.exe", _EXE)
    library_dir = tmp_path / "library"
    with zipfile.ZipFile(zip_path) as zf:
        assert organize_zip_member(zf, "media/x.svg", library_dir) is None
        assert organize_zip_member(zf, "media/x.exe", library_dir) is None


def test_organize_zip_member_accepts_and_reextensions_real_jpeg(tmp_path: Path) -> None:
    zip_path = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        # Claimed extension is .png but the bytes are a real JPEG --
        # stored under the sniffed extension, not the claimed one.
        zf.writestr("media/x.png", _JPEG)
    library_dir = tmp_path / "library"
    with zipfile.ZipFile(zip_path) as zf:
        result = organize_zip_member(zf, "media/x.png", library_dir)
    assert result is not None
    assert result.file_path.endswith(".jpg")
    assert Path(result.file_path).suffix in ALLOWED_MEDIA_EXTENSIONS
    assert (library_dir / result.file_path).is_file()
