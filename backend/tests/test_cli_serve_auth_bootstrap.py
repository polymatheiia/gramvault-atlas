"""Regression tests for audit finding S2's auth-by-default bootstrap:
`gramvault serve` generates and persists a token on first run, and
refuses a non-loopback bind with no token unless --insecure is passed."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from gramvault import config as config_module
from gramvault.cli import app

runner = CliRunner()


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"paths:\n  db_path: {tmp_path / 'data' / 'gv.db'}\n"
        f"  library_dir: {tmp_path / 'library'}\n"
        f"  chroma_dir: {tmp_path / 'data' / 'chroma'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(config_module.CONFIG_PATH_ENV_VAR, str(config_path))
    config_module.get_config.cache_clear()
    yield config_path
    config_module.get_config.cache_clear()


def test_serve_generates_and_persists_a_token_on_first_run(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_run = MagicMock()
    monkeypatch.setattr("uvicorn.run", mock_run)

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0
    assert "Generated an API token" in result.stdout
    secrets_path = isolated_config.parent / "secrets.yaml"
    assert secrets_path.exists()
    written = yaml.safe_load(secrets_path.read_text())
    assert written["auth"]["token"]
    mock_run.assert_called_once()


def test_serve_does_not_regenerate_an_existing_token(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gramvault.config import update_secrets

    update_secrets({"auth": {"token": "existing-token"}}, isolated_config)
    monkeypatch.setattr("uvicorn.run", MagicMock())

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0
    assert "Generated an API token" not in result.stdout
    secrets_path = isolated_config.parent / "secrets.yaml"
    assert yaml.safe_load(secrets_path.read_text())["auth"]["token"] == "existing-token"


def test_serve_refuses_non_loopback_bind_with_auth_disabled(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With auth.disabled=true, ensure_auth_token() deliberately mints no
    # token — that's the "I really do want this open" escape hatch (see
    # AuthConfig.disabled) — so this is the reachable case for the
    # non-loopback-without-a-token refusal (a bare first run instead
    # always gets an auto-generated token, see the test above).
    from gramvault.config import update_secrets

    update_secrets({"auth": {"disabled": True}}, isolated_config)
    monkeypatch.setattr("uvicorn.run", MagicMock())

    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code == 2
    assert "Refusing to bind" in result.stdout


def test_serve_insecure_flag_allows_non_loopback_bind(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gramvault.config import update_secrets

    update_secrets({"auth": {"disabled": True}}, isolated_config)
    mock_run = MagicMock()
    monkeypatch.setattr("uvicorn.run", mock_run)

    result = runner.invoke(app, ["serve", "--host", "0.0.0.0", "--insecure"])

    assert result.exit_code == 0
    mock_run.assert_called_once()


def test_token_command_show_and_rotate(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("uvicorn.run", MagicMock())
    runner.invoke(app, ["serve"])  # bootstraps a token

    show = runner.invoke(app, ["token", "show"])
    assert show.exit_code == 0
    first_token = show.stdout.strip()
    assert first_token

    rotate = runner.invoke(app, ["token", "rotate"])
    assert rotate.exit_code == 0

    show_again = runner.invoke(app, ["token", "show"])
    assert show_again.stdout.strip() != first_token


def test_generated_token_is_enforced_without_a_config_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config.yaml anywhere (a bare `pip install` + `gramvault serve`):
    the token written to ./secrets.yaml must be the one the running server
    enforces. It used to be written but never read back — `get_config()`
    only looked for secrets.yaml beside a config.yaml it had found — so the
    API ran wide open while the CLI printed a token."""
    monkeypatch.delenv(config_module.CONFIG_PATH_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("uvicorn.run", MagicMock())
    config_module.get_config.cache_clear()

    first = runner.invoke(app, ["serve"])
    assert first.exit_code == 0
    assert "Generated an API token" in first.stdout
    written = yaml.safe_load((tmp_path / "secrets.yaml").read_text())["auth"]["token"]

    config_module.get_config.cache_clear()
    assert config_module.get_config().auth.token == written

    second = runner.invoke(app, ["serve"])
    assert second.exit_code == 0
    assert "Generated an API token" not in second.stdout
    config_module.get_config.cache_clear()


def test_serve_host_override_reaches_the_app_config(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--host` bound the address, but the app's Host allowlist came from
    config.yaml's `server.host`, so every request to the overridden address
    was rejected with 400 "Invalid host header"."""
    for name in ("GRAMVAULT_SERVER__HOST", "GRAMVAULT_SERVER__PORT"):
        monkeypatch.setenv(name, "placeholder")  # so monkeypatch restores "unset"
        monkeypatch.delenv(name)
    monkeypatch.setattr("uvicorn.run", MagicMock())

    result = runner.invoke(app, ["serve", "--host", "100.64.0.5", "--port", "9123"])

    assert result.exit_code == 0
    config_module.get_config.cache_clear()
    server = config_module.get_config().server
    assert (server.host, server.port) == ("100.64.0.5", 9123)

    from fastapi.testclient import TestClient

    from gramvault.main import create_app

    app_config = config_module.get_config().model_copy(deep=True)
    app_config.auth.token = None
    with TestClient(create_app(app_config), base_url="http://100.64.0.5:9123") as client:
        assert client.get("/api/health").status_code == 200


def test_migrate_on_a_fresh_database_creates_the_schema(isolated_config: Path) -> None:
    """Migrations assume the base schema; on an empty DB `gramvault migrate`
    crashed with "no such table: items"."""
    result = runner.invoke(app, ["migrate"])

    assert result.exit_code == 0, result.output
    assert "fresh database" in result.stdout
    status = runner.invoke(app, ["migrate", "--status"])
    from gramvault.db.session import latest_migration_version

    assert f"schema version: {latest_migration_version()}" in status.stdout
