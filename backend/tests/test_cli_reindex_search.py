"""`gramvault reindex-search` — manual full rebuild of items_fts."""

from __future__ import annotations

from typer.testing import CliRunner

from gramvault.cli import app
from gramvault.config import Config
from gramvault.db.session import session_scope

runner = CliRunner()


def test_rebuilds_and_reports_count(tmp_config: Config, monkeypatch) -> None:
    monkeypatch.setattr("gramvault.config.get_config", lambda: tmp_config)
    monkeypatch.setattr("gramvault.cli.get_config", lambda: tmp_config)
    with session_scope(tmp_config) as conn:
        conn.execute("INSERT INTO items (media_type, caption) VALUES ('photo', 'hello')")
        conn.execute("INSERT INTO items (media_type, caption) VALUES ('photo', 'world')")
        conn.execute("DELETE FROM items_fts")

    result = runner.invoke(app, ["reindex-search"])
    assert result.exit_code == 0
    assert "2 item(s) indexed" in result.stdout

    with session_scope(tmp_config) as conn:
        assert conn.execute("SELECT count(*) FROM items_fts").fetchone()[0] == 2
