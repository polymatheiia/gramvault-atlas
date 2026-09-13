"""007: cooperative cancellation for import jobs.

Import (`gramvault.ingestion.importer.run_import`) used to run entirely
on the FastAPI event loop inside the request handler (audit finding R2)
— nothing else on the server could be served until it finished, and
`POST /api/import/jobs/{id}/cancel` was a best-effort no-op because the
loop never checked back in. Now that import runs in a worker thread
(`asyncio.to_thread`) and returns 202 immediately, cancellation needs
the same `cancel_requested` flag the generic `jobs` table already uses
(`api/jobs.py::JobContext.cancelled`) so a cancel request made mid-run
is actually seen between items.

`import_jobs` predates the generic `jobs` table and keeps its own shape
(see schema.sql's note by `export_jobs`), so the flag is added here
rather than migrating import onto `jobs`. A cancelled run still ends in
`status='failed'` with `error_message='cancelled by user'` — the CHECK
constraint on `status` isn't relaxed to add a `'cancelled'` value, since
SQLite can't alter a CHECK constraint without rebuilding the table.

`.py` migration (ALTER TABLE ADD COLUMN, guarded) per the same pattern
as 003; `schema.sql` carries the same column inline.
"""

from __future__ import annotations

import sqlite3


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def up(conn: sqlite3.Connection) -> None:
    if not _has_column(conn, "import_jobs", "cancel_requested"):
        conn.execute(
            "ALTER TABLE import_jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
        )
