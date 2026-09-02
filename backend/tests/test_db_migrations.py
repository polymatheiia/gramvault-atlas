"""Schema versioning + migration runner (`gramvault.db.session`).

Covers: fresh vs existing database paths, `user_version` tracking,
idempotency, the WAL pragma, and — most importantly — parity between
`schema.sql` (the fresh-install definition) and the sum of all migrations,
so the two can't silently drift.
"""

from __future__ import annotations

import re
import sqlite3

from gramvault.config import Config
from gramvault.db import session
from gramvault.db.session import (
    get_connection,
    init_db,
    latest_migration_version,
    migrate,
    schema_version,
)


def _normalise(sql: str) -> str:
    """Drop `-- ...` comments and collapse whitespace, so cosmetic
    differences (indentation, inline comments) between `schema.sql` and a
    migration don't fail the parity check — only real structural changes."""
    return " ".join(re.sub(r"--[^\n]*", "", sql).split())


def _objects(conn: sqlite3.Connection) -> dict[str, str]:
    """name -> normalised CREATE sql for every table/index in the DB
    (skipping SQLite's internal autoindexes, which have no `sql`)."""
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index') AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r["name"]: _normalise(r["sql"]) for r in rows}


class TestFreshDatabase:
    def test_applies_full_schema_and_stamps_latest_version(self, tmp_config: Config) -> None:
        conn = get_connection(tmp_config)
        try:
            init_db(conn)
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'"
            ).fetchone()
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
            ).fetchone()
            assert schema_version(conn) == latest_migration_version()
        finally:
            conn.close()

    def test_does_not_re_run_migrations_on_a_fresh_db(self, tmp_config: Config, monkeypatch) -> None:
        # A fresh DB is stamped straight to LATEST, so `migrate()` (called
        # again via session_scope etc.) must be a no-op — it must never try
        # to CREATE a table schema.sql already made.
        conn = get_connection(tmp_config)
        try:
            init_db(conn)
            before = schema_version(conn)
            assert migrate(conn) == before
        finally:
            conn.close()


class TestExistingDatabase:
    def test_pre_migration_db_is_brought_up_to_date(self, tmp_config: Config) -> None:
        # Simulate a database created before migrations existed: the v0
        # tables present, no `jobs` table, user_version 0.
        conn = get_connection(tmp_config)
        try:
            init_db(conn)
            conn.executescript(
                "DROP INDEX IF EXISTS idx_jobs_one_active_per_kind;"
                "DROP INDEX IF EXISTS idx_jobs_kind_status;"
                "DROP INDEX IF EXISTS idx_jobs_created_at;"
                "DROP TABLE IF EXISTS jobs;"
                "PRAGMA user_version = 0;"
            )
            conn.commit()
            assert not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='jobs'"
            ).fetchone()

            init_db(conn)  # existing-DB path -> runs migrations

            assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone()
            assert schema_version(conn) == latest_migration_version()
        finally:
            conn.close()

    def test_migrate_is_idempotent(self, tmp_db_conn: sqlite3.Connection) -> None:
        v1 = migrate(tmp_db_conn)
        v2 = migrate(tmp_db_conn)
        assert v1 == v2 == latest_migration_version()

    def test_version_ahead_of_disk_is_left_alone(
        self, tmp_db_conn: sqlite3.Connection
    ) -> None:
        tmp_db_conn.execute("PRAGMA user_version = 9999")
        assert migrate(tmp_db_conn) == 9999


class TestSchemaParity:
    def test_fresh_schema_equals_baseline_plus_migrations(self, tmp_path) -> None:
        """`schema.sql` on a fresh DB must produce exactly the same tables
        and indexes as (schema.sql with the post-baseline objects removed)
        + every migration. This is what stops the two from drifting."""
        fresh = get_connection(Config.model_validate({"paths": {"db_path": str(tmp_path / "a.db")}}))
        migrated = get_connection(
            Config.model_validate({"paths": {"db_path": str(tmp_path / "b.db")}})
        )
        try:
            init_db(fresh)

            # Build the "old database" the way migrations expect to find it:
            # apply schema.sql, then strip everything migration 001 adds and
            # reset the version, then run the migrations.
            init_db(migrated)
            migrated.executescript(
                "DROP INDEX IF EXISTS idx_jobs_one_active_per_kind;"
                "DROP INDEX IF EXISTS idx_jobs_kind_status;"
                "DROP INDEX IF EXISTS idx_jobs_created_at;"
                "DROP TABLE IF EXISTS jobs;"
                "PRAGMA user_version = 0;"
            )
            migrated.commit()
            migrate(migrated)

            assert _objects(fresh) == _objects(migrated)
            assert schema_version(fresh) == schema_version(migrated)
        finally:
            fresh.close()
            migrated.close()


class TestConnectionPragmas:
    def test_wal_and_busy_timeout_are_set(self, tmp_config: Config) -> None:
        conn = get_connection(tmp_config)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        finally:
            conn.close()

    def test_migrations_dir_exists_and_is_numbered(self) -> None:
        found = session._discover_migrations()
        assert found, "expected at least one migration on disk"
        numbers = [n for n, _ in found]
        assert numbers == sorted(numbers)
        assert numbers[0] == 1


class TestCategoriesMigration:
    def test_seeds_14_categories_and_adds_item_columns(
        self, tmp_config: Config
    ) -> None:
        conn = get_connection(tmp_config)
        try:
            init_db(conn)  # fresh -> schema.sql seeds via seed_default_categories()
            names = [r["name"] for r in conn.execute("SELECT name FROM categories ORDER BY sort_order")]
            assert len(names) == 14
            assert names[0] == "workouts" and names[-1] == "other"
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
            assert {
                "category_id",
                "category_source",
                "category_confidence",
                "category_reason",
                "category_updated_at",
            } <= cols
        finally:
            conn.close()

    def test_migration_002_is_rerunnable(self, tmp_config: Config) -> None:
        conn = get_connection(tmp_config)
        try:
            init_db(conn)
            # Re-applying 002's up() must not error or duplicate categories.
            for _, path in session._discover_migrations():
                if path.name.startswith("002"):
                    session._apply_migration(conn, path)
            assert conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 14
        finally:
            conn.close()


class TestOcrMigration:
    def test_adds_ocr_and_provenance_columns_to_media_files(
        self, tmp_config: Config
    ) -> None:
        conn = get_connection(tmp_config)
        try:
            init_db(conn)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(media_files)")}
            assert {
                "ocr_text",
                "ocr_attempted_at",
                "ocr_model",
                "vision_model",
                "transcript_model",
            } <= cols
        finally:
            conn.close()

    def test_migration_003_is_rerunnable(self, tmp_config: Config) -> None:
        conn = get_connection(tmp_config)
        try:
            init_db(conn)
            for _, path in session._discover_migrations():
                if path.name.startswith("003"):
                    session._apply_migration(conn, path)  # must not raise
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(media_files)")}
            assert "ocr_text" in cols
        finally:
            conn.close()

    def test_applies_to_a_pre_003_database(self, tmp_path) -> None:
        from gramvault.db.session import migrate

        conn = get_connection(
            Config.model_validate({"paths": {"db_path": str(tmp_path / "old.db")}})
        )
        try:
            init_db(conn)
            # Simulate a DB stamped before 003 by dropping the columns it adds.
            conn.executescript(
                "DROP INDEX IF EXISTS idx_media_files_ocr_pending;"
                "ALTER TABLE media_files DROP COLUMN ocr_text;"
                "ALTER TABLE media_files DROP COLUMN ocr_attempted_at;"
                "ALTER TABLE media_files DROP COLUMN ocr_model;"
                "ALTER TABLE media_files DROP COLUMN vision_model;"
                "ALTER TABLE media_files DROP COLUMN transcript_model;"
                "PRAGMA user_version = 2;"
            )
            conn.commit()
            migrate(conn)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(media_files)")}
            assert "ocr_attempted_at" in cols
            assert schema_version(conn) == latest_migration_version()
        finally:
            conn.close()
