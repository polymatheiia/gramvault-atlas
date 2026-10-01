"""Shared FastAPI dependencies for the api.routes_* modules."""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path

from gramvault.config import Config, get_config, get_config_path


def get_config_dependency() -> Config:
    """FastAPI dependency wrapper around `gramvault.config.get_config`.

    Routes should depend on this (rather than importing `get_config`
    directly) so tests can override it per-app via
    `app.dependency_overrides[get_config_dependency] = lambda: tmp_config`.
    """
    return get_config()


def get_config_path_dependency() -> Path:
    """FastAPI dependency wrapper around `gramvault.config.get_config_path`.

    Only needed by routes that write back to config.yaml (currently just
    the Obsidian vault-path save endpoint). Tests must override this to a
    tmp_path file — the default resolves against cwd and would otherwise
    write to the real project's config.yaml.
    """
    return get_config_path()


def inclusive_date_to(value: datetime | None) -> datetime | None:
    """Upper bound for a `date_to` filter. A bare date (`YYYY-MM-DD`, what
    the gallery's date input sends) parses to midnight, so used as-is
    `taken_at <= date_to` excluded everything on that day after 00:00 — a
    one-day range matched nothing. A midnight bound means "through the end
    of that day"."""
    if value is None or value.time() != time.min:
        return value
    return value.replace(hour=23, minute=59, second=59, microsecond=999_999)
