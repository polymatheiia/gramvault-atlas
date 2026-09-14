"""ffprobe-based media dimension/duration probing (audit finding R11).

`media_files.width`/`height`/`duration_seconds` have been in the schema
since the original scaffold but were never populated — nothing called
ffprobe. Best-effort, matching `export/poster.py`'s house style for
optional ffmpeg-suite tooling: if `ffprobe` isn't on PATH or the probe
fails for any reason (corrupt file, no video stream, a format ffprobe
doesn't recognise), this returns `None` rather than raising, since a
missing width/height/duration is a metadata gap, not a reason to fail an
import or link run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class MediaDimensions:
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None


def ffprobe_available() -> bool:
    """Return True if the `ffprobe` binary is discoverable on PATH."""
    return shutil.which("ffprobe") is not None


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def probe(path: Path) -> MediaDimensions | None:
    """ffprobe `path`'s primary video stream (photos have one too) for
    width/height, plus duration where ffprobe reports one (videos only —
    a photo's stream/format duration is absent or 0). `None` on any
    failure, including `ffprobe` not being installed."""
    if not ffprobe_available():
        return None
    path = Path(path)
    if not path.is_file():
        return None

    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), None
    )
    if video_stream is None:
        return None

    width = video_stream.get("width")
    height = video_stream.get("height")
    duration = _parse_float(video_stream.get("duration")) or _parse_float(
        data.get("format", {}).get("duration")
    )
    if width is None and height is None and duration is None:
        return None
    return MediaDimensions(width=width, height=height, duration_seconds=duration)
