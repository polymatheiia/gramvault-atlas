"""`gramvault backup` / `gramvault restore` CLI commands."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from gramvault.cli import app
from gramvault.config import Config
from gramvault.db.session import get_connection, init_db, session_scope

runner = CliRunner()


def _seed(tmp_config: Config) -> None:
    conn = get_connection(tmp_config)
    init_db(conn)
    conn.execute("INSERT INTO authors (username) VALUES ('alice')")
    conn.commit()
    conn.close()


def test_backup_then_restore_round_trip(tmp_config: Config, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("gramvault.config.get_config", lambda: tmp_config)
    monkeypatch.setattr("gramvault.cli.get_config", lambda: tmp_config)
    monkeypatch.setattr("gramvault.config.get_config_path", lambda: tmp_path / "config.yaml")
    _seed(tmp_config)

    dest = tmp_path / "backups"
    result = runner.invoke(app, ["backup", str(dest)])
    assert result.exit_code == 0, result.stdout
    assert "Wrote" in result.stdout
    assert "secrets.yaml was NOT included" in result.stdout

    archives = list(dest.glob("gramvault-backup-*.tar.gz"))
    assert len(archives) == 1

    with session_scope(tmp_config) as conn:
        conn.execute("INSERT INTO authors (username) VALUES ('bob')")

    result = runner.invoke(app, ["restore", str(archives[0]), "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "Restored" in result.stdout

    with session_scope(tmp_config) as conn:
        usernames = {r["username"] for r in conn.execute("SELECT username FROM authors")}
    assert usernames == {"alice"}


def test_restore_without_yes_prompts_and_aborts_on_no(tmp_config: Config, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("gramvault.config.get_config", lambda: tmp_config)
    monkeypatch.setattr("gramvault.cli.get_config", lambda: tmp_config)
    monkeypatch.setattr("gramvault.config.get_config_path", lambda: tmp_path / "config.yaml")
    _seed(tmp_config)

    dest = tmp_path / "backups"
    runner.invoke(app, ["backup", str(dest)])
    archive = next(dest.glob("gramvault-backup-*.tar.gz"))

    result = runner.invoke(app, ["restore", str(archive)], input="n\n")
    assert result.exit_code != 0


def test_restore_unknown_archive_fails_cleanly(tmp_config: Config, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("gramvault.config.get_config", lambda: tmp_config)
    monkeypatch.setattr("gramvault.cli.get_config", lambda: tmp_config)
    result = runner.invoke(app, ["restore", str(tmp_path / "nope.tar.gz"), "--yes"])
    assert result.exit_code != 0
