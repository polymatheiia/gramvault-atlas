"""`gramvault openapi` — dump the backend OpenAPI schema for the frontend
type generator (plan §A6)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from gramvault.cli import app

runner = CliRunner()


def test_prints_valid_openapi_json_to_stdout() -> None:
    result = runner.invoke(app, ["openapi"])
    assert result.exit_code == 0
    schema = json.loads(result.stdout)
    assert schema["openapi"].startswith("3.")
    assert "/api/digests" in schema["paths"]


def test_writes_sorted_stable_json_to_a_file(tmp_path: Path) -> None:
    out = tmp_path / "openapi.json"
    first = runner.invoke(app, ["openapi", "--out", str(out)])
    assert first.exit_code == 0
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    # sorted keys → deterministic across runs (CI diffs the committed copy)
    assert runner.invoke(app, ["openapi"]).stdout == text
    schema = json.loads(text)
    assert list(schema.keys()) == sorted(schema.keys())
