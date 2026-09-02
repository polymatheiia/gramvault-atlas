"""`gramvault import-categories <csv>` — backfilling item categories from
a hand-labelled CSV (the reels-workflow `categories_final.csv` shape)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from gramvault.cli import app
from gramvault.config import Config
from gramvault.db.session import session_scope

runner = CliRunner()


def _seed_items(config: Config) -> list[int]:
    with session_scope(config) as conn:
        ids = []
        for ext in ("a", "b", "c"):
            cur = conn.execute(
                "INSERT INTO items (external_id, media_type, caption) VALUES (?, 'reel', ?)",
                (ext, f"caption {ext}"),
            )
            ids.append(cur.lastrowid)
    return ids


def test_backfills_matching_rows_and_reports_the_rest(
    tmp_config: Config, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("gramvault.cli.get_config", lambda: tmp_config)
    a, b, c = _seed_items(tmp_config)

    csv_path = tmp_path / "categories_final.csv"
    csv_path.write_text(
        "id,category,username\n"
        f"{a},recipes,x\n"
        f"{b},psychology,y\n"
        f"{c},not-a-real-category,z\n"
        "9999,recipes,gone\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["import-categories", str(csv_path)])
    assert result.exit_code == 0, result.output
    assert "Categorised 2 item(s)." in result.output
    assert "no such item" in result.output
    assert "unknown category 'not-a-real-category'" in result.output

    with session_scope(tmp_config) as conn:
        rows = {
            r["id"]: (r["name"], r["category_source"])
            for r in conn.execute(
                "SELECT items.id, categories.name, items.category_source "
                "FROM items LEFT JOIN categories ON categories.id = items.category_id"
            )
        }
    assert rows[a] == ("recipes", "manual")
    assert rows[b] == ("psychology", "manual")
    assert rows[c] == (None, None)


def test_rejects_a_csv_without_the_required_columns(
    tmp_config: Config, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("gramvault.cli.get_config", lambda: tmp_config)
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("foo,bar\n1,2\n", encoding="utf-8")
    result = runner.invoke(app, ["import-categories", str(csv_path)])
    assert result.exit_code == 1
    assert "must have `id` and `category`" in result.output
