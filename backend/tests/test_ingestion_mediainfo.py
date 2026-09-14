"""`gramvault.ingestion.mediainfo.probe` — real ffprobe reads of
width/height/duration (audit finding R11), plus the graceful-failure
paths, mirroring `test_export_poster.py`'s house style for ffmpeg-suite
tooling tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gramvault.ai.keyframes import ffmpeg_available
from gramvault.ingestion.mediainfo import probe

needs_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe not on PATH")


def _make_test_video(path: Path, *, seconds: int = 2, size: str = "128x96") -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate=15", str(path)],
        capture_output=True,
        check=True,
    )


@needs_ffmpeg
def test_probes_width_height_and_duration(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    _make_test_video(video, seconds=2, size="128x96")

    dims = probe(video)

    assert dims is not None
    assert dims.width == 128
    assert dims.height == 96
    assert dims.duration_seconds is not None
    assert 1.5 < dims.duration_seconds < 2.5


@needs_ffmpeg
def test_probes_a_still_image_without_a_duration(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=64x48", "-frames:v", "1", str(image)],
        capture_output=True,
        check=True,
    )

    dims = probe(image)

    assert dims is not None
    assert dims.width == 64
    assert dims.height == 48


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert probe(tmp_path / "nope.mp4") is None


def test_corrupt_file_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "not-a-video.mp4"
    path.write_bytes(b"definitely not a video file")
    assert probe(path) is None


def test_returns_none_when_ffprobe_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("gramvault.ingestion.mediainfo.ffprobe_available", lambda: False)
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"irrelevant")
    assert probe(path) is None
