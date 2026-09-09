"""Extract a single still "poster" frame from a video for the Obsidian
export (plan §G6).

Mobile Obsidian renders an embedded `.mp4` poorly, so a video note embeds
a poster image above a plain link to the clip instead. This is a
best-effort helper: if `ffmpeg` isn't on PATH or the grab fails, the
caller falls back to a plain link with no poster.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from gramvault.ai.keyframes import ffmpeg_available

# Grab the frame a second in — the very first frame of a reel is often
# black or a title card.
_SEEK_SECONDS = 1


def poster_frame(video_path: Path, dest_path: Path) -> bool:
    """Write a single JPEG poster frame of `video_path` to `dest_path`.

    Returns True on success, False if ffmpeg is unavailable or the grab
    failed (a short clip, a corrupt file, …). Never raises.
    """
    if not ffmpeg_available():
        return False
    video_path = Path(video_path)
    dest_path = Path(dest_path)
    if not video_path.is_file():
        return False

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(_SEEK_SECONDS),
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(dest_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0 or not dest_path.is_file():
        # A clip shorter than the seek offset: retry from the first frame.
        cmd_retry = ["ffmpeg", "-y", "-i", str(video_path), "-frames:v", "1", "-q:v", "3", str(dest_path)]
        try:
            retry = subprocess.run(
                cmd_retry, capture_output=True, text=True, check=False, timeout=30
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return retry.returncode == 0 and dest_path.is_file()
    return True


__all__ = ["poster_frame"]
