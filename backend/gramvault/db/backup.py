"""Backup and restore for the SQLite database and the Chroma vector
store (Phase 4 feature backlog — audit §5.3 "Backups").

    gramvault backup <dir>     write gramvault-backup-<timestamp>.tar.gz
    gramvault restore <file>   extract it back into the configured paths

A backup archive holds:
  - `gramvault.db`   — a *consistent* snapshot taken via
                        `sqlite3.Connection.backup()` (safe to run
                        against a live, possibly-busy database — unlike
                        copying the file, which can catch a WAL mid-write)
  - `chroma/`         — the whole persistent-client directory, tarred as-is
  - `config.yaml`     — safe to include, holds no secrets
  - `manifest.json`   — gramvault version, schema version, created_at

`secrets.yaml` (provider API keys, the auth token) is deliberately
**excluded** rather than encrypted — this module has no key-management
story, and shipping "encryption" with a hardcoded or trivially-derived
key would be worse than being upfront that it's excluded. Restoring a
backup never touches the current `secrets.yaml`.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from gramvault import __version__
from gramvault.config import Config
from gramvault.db.session import get_connection, schema_version

MANIFEST_NAME = "manifest.json"
DB_ARCNAME = "gramvault.db"
CHROMA_ARCNAME = "chroma"
CONFIG_ARCNAME = "config.yaml"


class BackupError(RuntimeError):
    """A backup/restore operation failed for a reason worth showing the user."""


@dataclass
class BackupManifest:
    gramvault_version: str
    schema_version: int
    created_at: str
    included_chroma: bool
    included_config: bool


def create_backup(config: Config, dest_dir: Path, config_path: Path | None = None) -> Path:
    """Write a `gramvault-backup-<UTC timestamp>.tar.gz` into `dest_dir`
    and return its path. `dest_dir` is created if it doesn't exist."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive_path = dest_dir / f"gramvault-backup-{timestamp}.tar.gz"

    db_path = config.resolved_db_path
    if not db_path.is_file():
        raise BackupError(f"No database found at {db_path}")

    chroma_dir = config.resolved_chroma_dir
    included_chroma = chroma_dir.is_dir()
    included_config = config_path is not None and config_path.is_file()

    with tempfile.TemporaryDirectory(prefix="gramvault-backup-") as tmp:
        tmp_db = Path(tmp) / DB_ARCNAME
        # `sqlite3.Connection.backup()` (not a plain file copy) so a
        # concurrently-running server (WAL mode, possibly mid-write) still
        # yields a consistent snapshot rather than a torn one.
        source_conn = get_connection(config)
        try:
            dest_conn = sqlite3.connect(str(tmp_db))
            try:
                source_conn.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            source_conn.close()

        verify_conn = sqlite3.connect(str(tmp_db))
        try:
            snapshot_schema_version = schema_version(verify_conn)
        finally:
            verify_conn.close()

        manifest = BackupManifest(
            gramvault_version=__version__,
            schema_version=snapshot_schema_version,
            created_at=datetime.now(UTC).isoformat(),
            included_chroma=included_chroma,
            included_config=included_config,
        )
        (Path(tmp) / MANIFEST_NAME).write_text(
            json.dumps(manifest.__dict__, indent=2), encoding="utf-8"
        )

        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(tmp_db, arcname=DB_ARCNAME)
            tar.add(Path(tmp) / MANIFEST_NAME, arcname=MANIFEST_NAME)
            if included_chroma:
                tar.add(chroma_dir, arcname=CHROMA_ARCNAME)
            if included_config and config_path is not None:
                tar.add(config_path, arcname=CONFIG_ARCNAME)

    return archive_path


def read_manifest(archive_path: Path) -> BackupManifest:
    with tarfile.open(archive_path, "r:gz") as tar:
        try:
            member = tar.getmember(MANIFEST_NAME)
        except KeyError as exc:
            raise BackupError(f"{archive_path} is not a gramvault backup (no {MANIFEST_NAME})") from exc
        fileobj = tar.extractfile(member)
        if fileobj is None:
            raise BackupError(f"{archive_path} is not a gramvault backup (empty {MANIFEST_NAME})")
        data = json.loads(fileobj.read())
    return BackupManifest(**data)


def restore_backup(config: Config, archive_path: Path) -> BackupManifest:
    """Extract `archive_path` over the configured db/chroma paths.
    Destructive — overwrites the current database and vector store.
    `config.yaml` and `secrets.yaml` are never touched, even if the
    archive contains a `config.yaml` snapshot (informational only)."""
    if not archive_path.is_file():
        raise BackupError(f"No such backup file: {archive_path}")

    manifest = read_manifest(archive_path)

    db_path = config.resolved_db_path
    chroma_dir = config.resolved_chroma_dir
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # `filter="data"` (PEP 706) is Python 3.12+ only; this project supports
    # 3.11 too, so pass it only when available rather than requiring 3.12.
    extract_kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}

    with (
        tarfile.open(archive_path, "r:gz") as tar,
        tempfile.TemporaryDirectory(prefix="gramvault-restore-") as tmp,
    ):
        tar.extractall(tmp, **extract_kwargs)  # noqa: S202 -- trusted, locally-produced archive
        tmp_db = Path(tmp) / DB_ARCNAME
        if not tmp_db.is_file():
            raise BackupError(f"{archive_path} has no {DB_ARCNAME}")
        shutil.copy2(tmp_db, db_path)

        tmp_chroma = Path(tmp) / CHROMA_ARCNAME
        if tmp_chroma.is_dir():
            if chroma_dir.exists():
                shutil.rmtree(chroma_dir)
            shutil.copytree(tmp_chroma, chroma_dir)

    return manifest
