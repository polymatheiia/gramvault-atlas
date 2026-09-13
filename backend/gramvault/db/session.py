"""SQLite connection/session management + schema migrations.

Design choice — plain sqlite3 + schema.sql, not an ORM:
    GramVault is a single-user, local-first app with a modest, stable
    schema. We chose the stdlib `sqlite3` module with a hand-written
    `schema.sql` (CREATE TABLE IF NOT EXISTS) instead of SQLAlchemy or
    another ORM, to keep the dependency footprint small — this project
    already pulls in heavy AI dependencies (chromadb, faster-whisper,
    etc.) elsewhere. Callers may issue raw SQL via the `sqlite3.Connection`
    returned by `get_connection()` / `session_scope()`.

    If a future revision decides an ORM is warranted after all, swap this
    module's internals but keep the public functions
    (`get_connection`, `init_db`, `migrate`, `session_scope`,
    `get_db_path`, `schema_version`) stable so callers don't need to
    change.

Schema versioning:
    `PRAGMA user_version` tracks which numbered migrations have been
    applied. `db/migrations/NNN_*.sql` (or `.py`) files bring an existing
    database up to date; `schema.sql` is the canonical full definition
    for a *fresh* database and must be kept in sync with the migrations
    by hand (a parity test in `tests/test_db_migrations.py` enforces it).

    - Fresh DB (no `items` table): apply `schema.sql`, stamp
      `user_version` to the highest migration number.
    - Existing DB: apply every migration whose number is greater than the
      current `user_version`, in order, each in its own transaction.

    Migrations must be re-runnable (write `CREATE TABLE IF NOT EXISTS`,
    guard `ALTER TABLE ADD COLUMN` in a `.py` migration with a
    `PRAGMA table_info` check) so that a migration that fails partway can
    simply be re-applied after the cause is fixed — `user_version` is
    only advanced once a migration's whole file succeeds.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from gramvault.config import Config, get_config

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME_RE = re.compile(r"^(\d+)_.+\.(sql|py)$")

# Paths whose schema has already been brought current in this process.
# `session_scope()` used to call `init_db()` — a `sqlite_master` lookup plus,
# on every call, a directory scan of `db/migrations/` — on every connection,
# i.e. on every request and every job progress/cancellation check. Once a
# path has been initialised in-process, further opens skip straight to the
# connection: the schema can't drift under us within a single run.
_initialized_paths: set[str] = set()

# GramVault's default category taxonomy for a saved-reels library. The
# single source of truth — the `categories` table is seeded from this on a
# fresh database (`init_db`) and by migration `002_categories.py` on an
# existing one; after that it's user-editable via `/api/library/categories`.
# The descriptions double as the rubric the LLM classifier is handed.
DEFAULT_CATEGORIES: list[tuple[str, str]] = [
    ("workouts", "Exercise routines, physio and rehab, mobility, gym, running, sport training."),
    (
        "psychology",
        "Mental health, therapy concepts, ADHD / neurodivergence, emotional regulation, "
        "relationships and attachment, self-help.",
    ),
    ("books/manga", "Book and manga recommendations, reading lists, specific titles and authors."),
    ("movies/tv/anime", "Film, TV and anime recommendations, titles, watchlists."),
    ("memes", "Jokes and humour with no practical takeaway."),
    ("recipes", "Cooking recipes — ingredients, methods, food preparation."),
    (
        "lifehacks",
        "Practical tips and tricks: cleaning, organising, kitchen, travel, DIY, everyday tech.",
    ),
    ("websites", "Useful websites, apps, tools and online services."),
    ("astrology", "Astrology, natal charts, tarot, manifestation, law of assumption."),
    ("research", "A specific study, scientific finding, paper or data-driven claim."),
    ("home ideas", "Interior design, furniture, decor, home organisation, houseplants."),
    ("beauty", "Skincare, makeup, haircare, fragrance, grooming techniques and products."),
    ("animals", "Pets, wildlife, animal care / training / behaviour, animal content."),
    ("other", "Anything that doesn't fit the categories above."),
]


def seed_default_categories(conn: sqlite3.Connection) -> None:
    """Insert `DEFAULT_CATEGORIES` into the `categories` table, skipping any
    that already exist (by name). Idempotent; requires the table to exist."""
    for order, (name, description) in enumerate(DEFAULT_CATEGORIES):
        conn.execute(
            "INSERT INTO categories (name, sort_order, description) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO NOTHING",
            (name, order, description),
        )


def get_db_path(config: Config | None = None) -> Path:
    """Resolve the configured SQLite database file path."""
    config = config or get_config()
    return config.resolved_db_path


def get_connection(config: Config | None = None) -> sqlite3.Connection:
    """Open a new SQLite connection, creating parent directories as needed.

    Applies the pragmas GramVault relies on for safe concurrent access:
      - `foreign_keys = ON`   — enforce the schema's FK constraints.
      - `journal_mode = WAL`  — readers don't block the writer and vice
        versa, so a gallery page load during an enrichment run doesn't
        hit "database is locked". Persistent (set once per DB file);
        skipped for in-memory databases, which can't use WAL.
      - `busy_timeout = 5000` — wait up to 5s for a lock instead of
        failing immediately.
      - `synchronous = NORMAL` — safe with WAL, much faster than FULL.

    Callers are responsible for closing the connection (or use
    `session_scope()` below for automatic commit/rollback/close).
    """
    db_path = get_db_path(config)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if ":memory:" not in str(db_path):
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


# --- migrations -------------------------------------------------------------


def _discover_migrations() -> list[tuple[int, Path]]:
    """Every `NNN_*.sql` / `NNN_*.py` file under `db/migrations/`, sorted
    by number. Returns `[]` if the directory doesn't exist yet."""
    if not _MIGRATIONS_DIR.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for path in _MIGRATIONS_DIR.iterdir():
        match = _MIGRATION_NAME_RE.match(path.name)
        if match:
            found.append((int(match.group(1)), path))
    found.sort(key=lambda pair: pair[0])
    return found


def latest_migration_version() -> int:
    """Highest migration number on disk (0 if there are no migrations)."""
    migrations = _discover_migrations()
    return migrations[-1][0] if migrations else 0


def schema_version(conn: sqlite3.Connection) -> int:
    """The database's current `PRAGMA user_version`."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _apply_migration(conn: sqlite3.Connection, path: Path) -> None:
    if path.suffix == ".sql":
        conn.executescript(path.read_text(encoding="utf-8"))
        return
    # .py migration: must define `up(conn: sqlite3.Connection) -> None`.
    spec = importlib.util.spec_from_file_location(f"_gv_migration_{path.stem}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"could not load migration module {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "up"):
        raise RuntimeError(f"migration {path.name} has no `up(conn)` function")
    module.up(conn)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every pending migration in order, advancing `user_version`
    after each one commits. Returns the resulting version.

    Idempotent: migrations already reflected in `user_version` are
    skipped, so this is safe to call on every startup / connection.
    """
    current = schema_version(conn)
    target = latest_migration_version()
    if current > target:
        logger.warning(
            "database user_version (%d) is ahead of the newest migration (%d) — "
            "running an older build against a newer database?",
            current,
            target,
        )
        return current

    for version, path in _discover_migrations():
        if version <= current:
            continue
        logger.info("applying migration %s", path.name)
        _apply_migration(conn, path)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        current = version
    return current


def init_db(conn: sqlite3.Connection) -> None:
    """Bring `conn`'s database to the current schema.

    Fresh database (no `items` table): apply `schema.sql` in full and
    stamp `user_version` to the newest migration number. Existing
    database: apply pending migrations only. Both paths are idempotent.
    """
    has_items = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'items'"
    ).fetchone()

    if has_items is None:
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(f"PRAGMA user_version = {latest_migration_version()}")
        seed_default_categories(conn)
        conn.commit()
    else:
        migrate(conn)


@contextmanager
def session_scope(config: Config | None = None) -> Iterator[sqlite3.Connection]:
    """Context manager: opens a connection, ensures the schema is current,
    commits on clean exit, rolls back on exception, always closes.

    Example:
        with session_scope() as conn:
            conn.execute("INSERT INTO authors (username) VALUES (?)", ("alice",))
    """
    conn = get_connection(config)
    try:
        db_path = str(get_db_path(config))
        if db_path not in _initialized_paths:
            init_db(conn)
            _initialized_paths.add(db_path)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
