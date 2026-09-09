"""`gramvault.export.poster.poster_frame` — real ffmpeg grab of a still
frame, plus the graceful-failure paths (§G6)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gramvault.ai.keyframes import ffmpeg_available
from gramvault.export.poster import poster_frame

needs_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not on PATH")


def _make_test_video(path: Path, *, seconds: int = 2) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"testsrc=duration={seconds}:size=128x128:rate=15",
            str(path),
        ],
        capture_output=True,
        check=True,
    )


@needs_ffmpeg
def test_writes_a_jpeg_poster(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    _make_test_video(video)
    dest = tmp_path / "out" / "clip.poster.jpg"

    assert poster_frame(video, dest) is True
    assert dest.is_file()
    assert dest.stat().st_size > 0
    assert dest.read_bytes()[:2] == b"\xff\xd8"  # JPEG SOI marker


@needs_ffmpeg
def test_falls_back_to_first_frame_for_a_clip_shorter_than_the_seek(tmp_path: Path) -> None:
    # testsrc can't be < 1s reliably; use a ~0.3s clip via a frame count.
    video = tmp_path / "tiny.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10",
         "-frames:v", "3", str(video)],
        capture_output=True,
        check=True,
    )
    dest = tmp_path / "tiny.poster.jpg"
    assert poster_frame(video, dest) is True
    assert dest.is_file()


def test_missing_source_returns_false(tmp_path: Path) -> None:
    assert poster_frame(tmp_path / "nope.mp4", tmp_path / "x.jpg") is False


def test_returns_false_when_ffmpeg_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("gramvault.export.poster.ffmpeg_available", lambda: False)
    (tmp_path / "clip.mp4").write_bytes(b"not really a video")
    assert poster_frame(tmp_path / "clip.mp4", tmp_path / "x.jpg") is False
