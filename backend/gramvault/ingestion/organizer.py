"""Media organizer: content-addressed storage for media files pulled out
of an Instagram export ZIP.

Design:
    Every media file we manage to locate in the ZIP is hashed (sha256) and
    written to `<library_dir>/media/<first-2-hex-chars>/<full-hash>.<ext>`.
    Writing is a no-op if the destination already exists, so re-importing
    the same ZIP (or a different export that happens to contain the same
    file) never duplicates bytes on disk — dedupe is purely by content,
    not by source path.

    We never call `ZipFile.extract`/`extractall` with an attacker/export
    -controlled destination path — the destination filename here is
    always derived from the hash we just computed, never from the zip
    member name. The only zip-slip-sensitive step (resolving a JSON
    "uri" string to an actual zip member to *read*) happens in
    `parser._resolve_zip_member`, which already rejects unsafe member
    names before this module ever sees them.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

# Read size for hashing files already on disk — large enough to keep sha256
# fed, small enough that a 200 MB video never lands in memory whole.
_HASH_CHUNK_BYTES = 1024 * 1024

# Defaults for organize_zip_member()'s caps, matching ImportConfig's
# defaults (config.py) — kept here too so this module's tests and any
# other caller don't have to thread a Config through for a sane default.
_DEFAULT_MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
_DEFAULT_MAX_COMPRESSION_RATIO = 200


@dataclass
class OrganizedMediaFile:
    file_path: str  # POSIX path, relative to library_dir, e.g. "media/ab/ab34...ef.jpg"
    checksum: str  # full sha256 hex digest


# Extensions the library ever stores/serves. Deliberately excludes .html,
# .svg, .xhtml and anything else a browser would execute as a document —
# see audit finding S3: a crafted export could otherwise plant an HTML
# page under /media and have it served on the app's own origin.
ALLOWED_MEDIA_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".mp4", ".mov", ".m4v", ".webm"}
)

# (magic bytes, extension) pairs checked against a member's leading bytes.
# A member that matches none of these — including a RIFF container that
# isn't really WebP — is rejected outright, regardless of what extension
# the zip member or export JSON claimed.
_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF8", ".gif"),
    (b"\x1a\x45\xdf\xa3", ".webm"),
)

# ISO-BMFF (bytes 4:8 == b"ftyp") major brands, keyed to the extension they
# actually are — HEIC/HEIF, QuickTime .mov, and everything else MPEG-4
# share this container, so the brand at bytes 8:12 is the only way to tell
# them apart. Without this, every iPhone photo would be mis-stored as a
# playable-nowhere ".mp4".
_ISO_BMFF_BRANDS: dict[bytes, str] = {
    b"heic": ".heic",
    b"heix": ".heic",
    b"heim": ".heic",
    b"heis": ".heic",
    b"hevc": ".heic",
    b"hevx": ".heic",
    b"mif1": ".heif",
    b"msf1": ".heif",
    b"qt  ": ".mov",
    b"M4V ": ".m4v",
    b"M4VH": ".m4v",
    b"M4VP": ".m4v",
}


def sniff_extension(head: bytes) -> str | None:
    """Identify a media file by its magic bytes, never by a caller-supplied
    extension. Returns None for anything unrecognised."""
    for magic, ext in _MAGIC_SIGNATURES:
        if head.startswith(magic):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp":  # ISO-BMFF container
        return _ISO_BMFF_BRANDS.get(head[8:12], ".mp4")
    return None


def organize_zip_member(
    zf: zipfile.ZipFile,
    member_name: str,
    library_dir: Path,
    *,
    max_bytes: int = _DEFAULT_MAX_MEMBER_BYTES,
    max_ratio: int = _DEFAULT_MAX_COMPRESSION_RATIO,
) -> OrganizedMediaFile | None:
    """Read `member_name` out of an already-open ZIP, verify it's really a
    supported image/video by its magic bytes (never trusting the member's
    claimed extension), hash it, and write it (deduped, re-extensioned to
    match the sniffed type) under `library_dir/media/...`. Returns `None`
    if the member can't be read, isn't a recognised media type, or trips
    one of the size/ratio caps — so one bad/malicious media reference
    doesn't abort the whole import.

    Decompressed bytes are streamed straight to a temp file in 1 MiB
    chunks rather than held in memory: a member's declared `file_size` /
    `compress_size` is only what the ZIP directory claims, and a hostile
    export can lie (audit finding S9 — decompression bomb / OOM). The
    directory-claimed size is still checked up front as a cheap early
    reject; the running total during the actual read is the real guard.
    """
    try:
        info = zf.getinfo(member_name)
    except KeyError:
        return None
    if info.file_size > max_bytes:
        return None
    if info.compress_size > 0 and info.file_size / info.compress_size > max_ratio:
        return None

    media_dir = library_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    head = b""
    total = 0
    tmp_path: Path | None = None
    try:
        with zf.open(member_name) as src, tempfile.NamedTemporaryFile(dir=media_dir, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            while chunk := src.read(_HASH_CHUNK_BYTES):
                total += len(chunk)
                if total > max_bytes:
                    return None
                if len(head) < 16:
                    head += chunk[: 16 - len(head)]
                digest.update(chunk)
                tmp.write(chunk)
    except (KeyError, zipfile.BadZipFile, OSError):
        return None
    finally:
        if tmp_path is not None and (total > max_bytes or sniff_extension(head) is None):
            tmp_path.unlink(missing_ok=True)

    ext = sniff_extension(head)
    if ext is None or ext not in ALLOWED_MEDIA_EXTENSIONS or tmp_path is None:
        return None

    hex_digest = digest.hexdigest()
    subdir = media_dir / hex_digest[:2]
    subdir.mkdir(parents=True, exist_ok=True)
    dest = subdir / f"{hex_digest}{ext}"
    if dest.exists():
        tmp_path.unlink(missing_ok=True)
    else:
        os.replace(tmp_path, dest)
    return OrganizedMediaFile(file_path=dest.relative_to(library_dir).as_posix(), checksum=hex_digest)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def organize_local_file(
    source: Path, library_dir: Path, *, copy: bool = False
) -> OrganizedMediaFile | None:
    """Bring a media file that's already on disk into the library store.

    Same content-addressed layout as `organize_zip_member`, but the bytes
    are streamed rather than held in memory (these are full-size videos,
    not zip members we already decompressed).

    By default the file is hardlinked, so a large external download
    directory can be linked in without a second copy on disk; hardlinks
    are safe here because the store is content-addressed and its entries
    are never mutated in place. Falls back to a real copy when the source
    is on another filesystem (or `copy=True` is passed), and returns
    `None` if the file can't be read at all, so one unreadable file
    doesn't abort a whole run.
    """
    try:
        digest = _hash_file(source)
    except OSError:
        return None

    subdir = library_dir / "media" / digest[:2]
    subdir.mkdir(parents=True, exist_ok=True)
    ext = source.suffix.lower()
    dest = subdir / f"{digest}{ext}"

    if not dest.exists():
        try:
            if copy:
                shutil.copy2(source, dest)
            else:
                os.link(source, dest)
        except OSError:
            # Cross-device link, a filesystem without hardlinks, or a race
            # with another writer — a plain copy covers all three.
            try:
                shutil.copy2(source, dest)
            except OSError:
                return None

    return OrganizedMediaFile(file_path=dest.relative_to(library_dir).as_posix(), checksum=digest)
