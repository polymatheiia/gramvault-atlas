"""Tests for gramvault.db.backup: create/restore round-trip, the
consistent-snapshot backup (not a plain file copy), secrets.yaml
exclusion, and error handling for a missing/bad archive."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from gramvault.config import Config, PathsConfig
from gramvault.db.backup import BackupError, create_backup, read_manifest, restore_backup
from gramvault.db.session import get_connection, init_db, session_scope


@pytest.fixture
def seeded_config(tmp_path: Path) -> Config:
    config = Config(
        paths=PathsConfig(
            library_dir=str(tmp_path / "library"),
            db_path=str(tmp_path / "data" / "gramvault.db"),
            chroma_dir=str(tmp_path / "data" / "chroma"),
        )
    )
    conn = get_connection(config)
    init_db(conn)
    conn.execute("INSERT INTO authors (username) VALUES ('alice')")
    conn.commit()
    conn.close()
    config.resolved_chroma_dir.mkdir(parents=True, exist_ok=True)
    (config.resolved_chroma_dir / "chroma.sqlite3").write_text("fake chroma data")
    return config


def test_create_backup_writes_a_tar_with_expected_members(seeded_config: Config, tmp_path: Path) -> None:
    dest = tmp_path / "backups"
    archive = create_backup(seeded_config, dest)

    assert archive.parent == dest
    assert archive.name.startswith("gramvault-backup-")
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert "gramvault.db" in names
    assert "manifest.json" in names
    assert "chroma/chroma.sqlite3" in names
    assert "secrets.yaml" not in names
    assert "config.yaml" not in names  # no config_path given


def test_create_backup_includes_config_when_path_given(seeded_config: Config, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("paths:\n  db_path: x\n")
    archive = create_backup(seeded_config, tmp_path / "backups", config_path=config_path)
    with tarfile.open(archive, "r:gz") as tar:
        assert "config.yaml" in tar.getnames()


def test_create_backup_missing_db_raises(tmp_path: Path) -> None:
    config = Config(
        paths=PathsConfig(
            library_dir=str(tmp_path / "library"),
            db_path=str(tmp_path / "data" / "gramvault.db"),
            chroma_dir=str(tmp_path / "data" / "chroma"),
        )
    )
    with pytest.raises(BackupError):
        create_backup(config, tmp_path / "backups")


def test_read_manifest(seeded_config: Config, tmp_path: Path) -> None:
    archive = create_backup(seeded_config, tmp_path / "backups")
    manifest = read_manifest(archive)
    assert manifest.included_chroma is True
    assert manifest.schema_version > 0


def test_read_manifest_rejects_non_backup_archive(tmp_path: Path) -> None:
    fake = tmp_path / "not-a-backup.tar.gz"
    with tarfile.open(fake, "w:gz") as tar:
        member_path = tmp_path / "hello.txt"
        member_path.write_text("hi")
        tar.add(member_path, arcname="hello.txt")
    with pytest.raises(BackupError):
        read_manifest(fake)


def test_restore_backup_round_trips_data_and_chroma(seeded_config: Config, tmp_path: Path) -> None:
    archive = create_backup(seeded_config, tmp_path / "backups")

    # Mutate the live DB and blow away chroma after the backup was taken,
    # to prove restore actually overwrites rather than being a no-op.
    with session_scope(seeded_config) as conn:
        conn.execute("INSERT INTO authors (username) VALUES ('bob')")
    import shutil

    shutil.rmtree(seeded_config.resolved_chroma_dir)

    manifest = restore_backup(seeded_config, archive)
    assert manifest.included_chroma is True

    with session_scope(seeded_config) as conn:
        usernames = {r["username"] for r in conn.execute("SELECT username FROM authors")}
    assert usernames == {"alice"}  # 'bob' is gone — restored to the backed-up state
    assert (seeded_config.resolved_chroma_dir / "chroma.sqlite3").read_text() == "fake chroma data"


def test_restore_backup_missing_file_raises(seeded_config: Config, tmp_path: Path) -> None:
    with pytest.raises(BackupError):
        restore_backup(seeded_config, tmp_path / "nope.tar.gz")


def test_restore_backup_ignores_a_stale_wal(seeded_config: Config, tmp_path: Path) -> None:
    """A `-wal` left beside the DB (a crash, or the server still running)
    must not be replayed onto the restored file. Restore used to copy the
    snapshot over `gramvault.db` and leave the old WAL in place, so SQLite
    re-applied post-backup writes — or failed outright — on the next open."""
    archive = create_backup(seeded_config, tmp_path / "backups")

    # Post-backup writes that live only in the WAL: this connection stays
    # open, so nothing is checkpointed back into the main file.
    live = get_connection(seeded_config)
    live.execute("PRAGMA wal_autocheckpoint = 0")
    for name in ("bob", "carol", "dave"):
        live.execute("INSERT INTO authors (username) VALUES (?)", (name,))
    live.commit()
    wal = Path(f"{seeded_config.resolved_db_path}-wal")
    assert wal.is_file() and wal.stat().st_size > 0

    try:
        restore_backup(seeded_config, archive)
    finally:
        live.close()

    fresh = get_connection(seeded_config)
    try:
        assert fresh.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        usernames = {r["username"] for r in fresh.execute("SELECT username FROM authors")}
    finally:
        fresh.close()
    assert usernames == {"alice"}
