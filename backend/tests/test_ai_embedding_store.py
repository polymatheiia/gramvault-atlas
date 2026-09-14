"""Regression test for audit finding S7: Chroma's anonymised telemetry
must be off, matching the README's "no telemetry" privacy claim."""

from __future__ import annotations

from pathlib import Path

from gramvault.ai.embedding_store import _client
from gramvault.config import Config, PathsConfig


def _config_for(tmp_path: Path, chroma_subdir: str = "chroma") -> Config:
    return Config(
        paths=PathsConfig(
            library_dir=str(tmp_path / "library"),
            db_path=str(tmp_path / "data" / "gramvault.db"),
            chroma_dir=str(tmp_path / "data" / chroma_subdir),
        )
    )


def test_chroma_client_disables_anonymized_telemetry(tmp_path: Path) -> None:
    client = _client(_config_for(tmp_path))
    assert client.get_settings().anonymized_telemetry is False


def test_client_is_cached_per_chroma_dir(tmp_path: Path) -> None:
    """R9: a fresh PersistentClient per call was real per-request overhead
    (it opens its own on-disk index). Same chroma_dir -> the same client
    instance; a different chroma_dir still gets its own."""
    config_a = _config_for(tmp_path, "chroma-a")
    config_b = _config_for(tmp_path, "chroma-b")

    assert _client(config_a) is _client(config_a)
    assert _client(config_a) is not _client(config_b)
