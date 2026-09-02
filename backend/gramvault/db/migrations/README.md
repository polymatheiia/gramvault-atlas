# Schema migrations

Numbered files (`NNN_short_name.sql` or `NNN_short_name.py`) that bring an
existing GramVault database up to date. `db/session.py::migrate()` applies
every file whose `NNN` is greater than the database's `PRAGMA user_version`,
in order, advancing `user_version` after each file's whole contents commit
successfully.

`schema.sql` is the canonical full definition for a **fresh** database and
must stay in sync with the sum of all migrations — `tests/test_db_migrations.py`
fails if they drift.

## Rules

- **Re-runnable.** A migration that fails partway is re-applied as-is once the
  cause is fixed (`user_version` only advances on full success). Write
  `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`.
- **`ALTER TABLE ADD COLUMN` has no `IF NOT EXISTS` in SQLite** — do those in a
  `.py` migration that guards with a `PRAGMA table_info` check. A `.py`
  migration defines `def up(conn: sqlite3.Connection) -> None`.
- Keep migrations DDL-only where possible; no semicolons inside string
  literals in `.sql` files (they're run via `executescript`).
- After adding a migration, mirror the change into `schema.sql`.
