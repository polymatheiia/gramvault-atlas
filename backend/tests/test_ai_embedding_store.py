"""Regression test for audit finding S7: Chroma's anonymised telemetry
must be off, matching the README's "no telemetry" privacy claim."""

from __future__ import annotations

from pathlib import Path

from gramvault.ai.embedding_store import _client
from gramvault.config import Config, PathsConfig


def test_chroma_client_disables_anonymized_telemetry(tmp_path: Path) -> None:
    config = Config(
        paths=PathsConfig(
            library_dir=str(tmp_path / "library"),
            db_path=str(tmp_path / "data" / "gramvault.db"),
            chroma_dir=str(tmp_path / "data" / "chroma"),
        )
    )
    client = _client(config)
    assert client.get_settings().anonymized_telemetry is False
