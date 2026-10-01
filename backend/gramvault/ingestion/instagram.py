"""Pull the user's own *saved* posts from Instagram (integration plan §F).

Instagram's "Download Your Information" export (what `parser.py` reads) is
the supported, no-credentials path. This module is the opt-in alternative:
walk your saved feed directly with a logged-in session so a fresh reel
shows up in GramVault minutes after you save it, instead of on your next
data-export request.

How it stays a thin layer over what already exists:

  * login is **cookies only** — no password ever touches this code. Paste a
    cookie export (`connect_from_cookies`) or let the server read the local
    Firefox cookie store (`read_firefox_cookies`); either way the cookies
    are handed to Instaloader, which validates them with `test_login()` and
    caches a session file. The raw `sessionid` is never written to the DB.
  * the download itself produces the exact same shape as a real export —
    one `saved_posts.json` (`label_values` layout) plus media files whose
    names carry the shortcode — so `importer.import_zip` and
    `linker.link_local_media` ingest it completely unchanged.

Instaloader is an optional dependency (`pip install -e ".[instagram]"`);
every entry point raises a friendly `InstagramDependencyError` when it's
missing rather than failing at import time.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gramvault.config import Config, get_config
from gramvault.db.session import session_scope
from gramvault.ingestion.importer import import_zip
from gramvault.ingestion.linker import link_local_media

logger = logging.getLogger(__name__)

# Instaloader's own layout for the cached session; we add a sidecar JSON so
# GramVault knows which username to load without a network round-trip.
_STATE_FILENAME = "gramvault-pull.json"
_SESSION_PREFIX = "session-"

# Politeness delay between downloads — Instagram rate-limits its private
# endpoints aggressively and a tight loop gets the session flagged.
_MIN_DELAY = 3.0
_MAX_DELAY = 7.0

# Cookie names a usable Instagram session must contain.
_REQUIRED_COOKIE = "sessionid"
# Netscape cookies.txt prefix marking an HttpOnly cookie (curl's convention).
_HTTPONLY_PREFIX = "#HttpOnly_"
# Download failures in a row before the walk gives up (rate-limited or the
# session went stale) — what was downloaded so far is still imported.
_MAX_CONSECUTIVE_FAILURES = 5

_SAVED_POSTS_ARCNAME = "your_instagram_activity/saved/saved_posts.json"


# --- errors --------------------------------------------------------------


class InstagramError(Exception):
    """Base for every failure in this module. Messages are written to be
    shown directly in the UI/CLI without rewrapping."""


class InstagramDependencyError(InstagramError):
    """The optional `instaloader` package isn't installed."""


class InstagramAuthError(InstagramError):
    """Cookies were rejected by Instagram, or none usable were found."""


class InstagramNotConnectedError(InstagramError):
    """No cached session — `connect_from_cookies` hasn't run (or the
    session file was removed)."""


# --- data shapes --------------------------------------------------------


@dataclass
class SessionState:
    """What `GET /api/pull/session` reports. Never carries the cookie."""

    configured: bool
    username: str | None = None
    last_verified_at: str | None = None


@dataclass
class PullResult:
    scanned: int = 0
    new: int = 0
    downloaded: int = 0
    imported: int = 0
    linked: int = 0
    failed: int = 0
    stopped_reason: str = "completed"
    new_item_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "new": self.new,
            "downloaded": self.downloaded,
            "imported": self.imported,
            "linked": self.linked,
            "failed": self.failed,
            "stopped_reason": self.stopped_reason,
            "new_item_ids": self.new_item_ids,
        }


ProgressCb = Callable[[dict[str, Any]], None]
CancelCheck = Callable[[], bool]


# --- optional dependency ------------------------------------------------


def _load_instaloader() -> Any:
    """Import `instaloader`, or raise a friendly error. Isolated in one
    function so tests can monkeypatch it with a fake module."""
    try:
        import instaloader  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise InstagramDependencyError(
            "Pulling from Instagram needs the optional 'instaloader' package. "
            'Install it with:  pip install -e ".[instagram]"'
        ) from exc
    return instaloader


# --- cookie parsing / discovery ---------------------------------------


def parse_cookies(text: str) -> dict[str, str]:
    """Read cookies from any of the shapes a browser / extension exports.

    Accepted (auto-detected):
      * JSON object   `{"sessionid": "...", "csrftoken": "..."}`
      * JSON array    `[{"name": "sessionid", "value": "..."}, ...]`
        (Cookie-Editor / EditThisCookie)
      * Netscape      `cookies.txt`
    """
    text = text.strip()
    if not text:
        return {}

    if text[:1] in "{[":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InstagramAuthError(
                f"That doesn't parse as JSON ({exc}). Paste the cookie export "
                "exactly as the extension gave it, or a Netscape cookies.txt file."
            ) from exc
        if isinstance(data, dict):
            jar = {str(k): str(v) for k, v in data.items()}
        elif isinstance(data, list):
            jar = {
                str(c["name"]): str(c["value"])
                for c in data
                if isinstance(c, dict) and "name" in c and "value" in c
            }
        else:
            jar = {}
    else:
        jar = {}
        for line in text.splitlines():
            line = line.strip()
            # HttpOnly cookies — `sessionid` among them — are written as
            # `#HttpOnly_<domain>\t...`, which a plain "skip # comments"
            # rule threw away along with the real comments.
            if line.startswith(_HTTPONLY_PREFIX):
                line = line[len(_HTTPONLY_PREFIX) :]
            elif not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and "instagram" in parts[0]:
                jar[parts[5]] = parts[6]

    return {k: v for k, v in jar.items() if v}


def _firefox_profile_roots() -> list[Path]:
    home = Path.home()
    return [
        home / ".mozilla" / "firefox",
        home / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
        home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
        # macOS
        home / "Library" / "Application Support" / "Firefox" / "Profiles",
    ]


def read_firefox_cookies() -> dict[str, str] | None:
    """Best-effort: pull instagram.com cookies straight from this machine's
    Firefox cookie store. Returns the cookie jar from the most recently
    updated profile that holds a live-looking `sessionid`, or None.

    Only works when the browser you're logged into Instagram in runs on the
    same machine (and as the same user) as the GramVault server.
    """
    candidates: list[Path] = []
    for root in _firefox_profile_roots():
        if root.is_dir():
            candidates.extend(root.glob("*/cookies.sqlite"))
    candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)

    for db_path in candidates:
        try:
            conn = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
        except sqlite3.OperationalError:
            continue
        try:
            rows = conn.execute(
                "SELECT name, value FROM moz_cookies WHERE host LIKE '%instagram.com'"
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        finally:
            conn.close()
        jar = {name: value for name, value in rows if value}
        if _REQUIRED_COOKIE in jar:
            logger.info("read %d instagram.com cookie(s) from %s", len(jar), db_path)
            return jar
    return None


# --- session file management -----------------------------------------


def _session_path(config: Config, username: str) -> Path:
    return config.resolved_pull_session_dir / f"{_SESSION_PREFIX}{username}"


def _state_path(config: Config) -> Path:
    return config.resolved_pull_session_dir / _STATE_FILENAME


def _write_state(config: Config, username: str) -> SessionState:
    now = datetime.now(tz=UTC).isoformat()
    path = _state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"username": username, "last_verified_at": now}), encoding="utf-8"
    )
    _chmod_600(path)
    return SessionState(configured=True, username=username, last_verified_at=now)


def _chmod_600(path: Path) -> None:
    with contextlib.suppress(OSError):  # e.g. Windows
        path.chmod(0o600)


def get_session_state(config: Config | None = None) -> SessionState:
    """What's cached on disk — no network call. `configured` is true only
    when both the sidecar state and the Instaloader session file exist."""
    config = config or get_config()
    state_path = _state_path(config)
    if not state_path.is_file():
        return SessionState(configured=False)
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return SessionState(configured=False)
    username = data.get("username")
    if not username or not _session_path(config, username).is_file():
        return SessionState(configured=False)
    return SessionState(
        configured=True,
        username=username,
        last_verified_at=data.get("last_verified_at"),
    )


def disconnect(config: Config | None = None) -> None:
    """Remove the cached session + state. Does not revoke the session on
    Instagram's side — the UI tells the user to do that themselves."""
    config = config or get_config()
    state = get_session_state(config)
    if state.username:
        _unlink(_session_path(config, state.username))
    _unlink(_state_path(config))
    # A stale session file for a different username shouldn't linger either.
    session_dir = config.resolved_pull_session_dir
    if session_dir.is_dir():
        for path in session_dir.glob(f"{_SESSION_PREFIX}*"):
            _unlink(path)


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


# --- connect --------------------------------------------------------


def connect_from_cookies(cookies: dict[str, str], config: Config | None = None) -> SessionState:
    """Validate `cookies` against Instagram and cache a session file.

    Raises `InstagramAuthError` if `sessionid` is absent or Instagram
    rejects the session (it's stale — log in again and re-export).
    """
    config = config or get_config()
    if _REQUIRED_COOKIE not in cookies:
        raise InstagramAuthError(
            f"No '{_REQUIRED_COOKIE}' cookie in that export. Make sure you're logged "
            "into Instagram in the browser you exported from, and that the export "
            "includes instagram.com cookies."
        )

    instaloader = _load_instaloader()
    loader = instaloader.Instaloader(max_connection_attempts=1)
    loader.context._session.cookies.update(cookies)
    try:
        who = loader.test_login()
    except Exception as exc:  # noqa: BLE001 - network/parse errors all mean "couldn't verify"
        raise InstagramAuthError(f"Couldn't reach Instagram to verify the session: {exc}") from exc
    if not who:
        raise InstagramAuthError(
            "Instagram rejected those cookies — the session is stale. Log in again "
            "in your browser, then export and paste fresh cookies."
        )

    loader.context.username = who
    session_path = _session_path(config, who)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    loader.save_session_to_file(str(session_path))
    _chmod_600(session_path)
    logger.info("instagram: connected as @%s, session cached at %s", who, session_path)
    return _write_state(config, who)


def connect_from_local_browser(config: Config | None = None) -> SessionState:
    """`read_firefox_cookies` + `connect_from_cookies`. Raises
    `InstagramAuthError` when no local Firefox session is found."""
    jar = read_firefox_cookies()
    if not jar:
        raise InstagramAuthError(
            "No Instagram cookies found in a local Firefox profile. Either you're "
            "logged in on a different machine, or you use another browser — paste a "
            "cookie export instead."
        )
    return connect_from_cookies(jar, config)


# --- pull ----------------------------------------------------------


def _known_shortcodes(config: Config) -> set[str]:
    with session_scope(config) as conn:
        rows = conn.execute(
            "SELECT external_id FROM items WHERE external_id IS NOT NULL"
        ).fetchall()
    return {row["external_id"] for row in rows}


def _entry_for_post(post: Any) -> dict[str, Any]:
    """One saved Post shaped like an entry in Instagram's `saved_posts.json`
    export (`label_values` layout), so `parser.py` reads it unchanged."""
    url = f"https://www.instagram.com/p/{post.shortcode}/"
    try:
        owner = post.owner_username or "unknown"
    except Exception:  # noqa: BLE001 - owner lookup can hit the network and fail
        owner = "unknown"
    try:
        hashtags = sorted(post.caption_hashtags or [])
    except Exception:  # noqa: BLE001
        hashtags = []
    label_values = [
        {"label": "URL", "href": url, "value": url},
        {"label": "Caption", "value": post.caption or ""},
        {"label": "Title", "value": ""},
        {
            "title": "Hashtags",
            "dict": [{"dict": [{"label": "Name", "value": f"#{h}"}]} for h in hashtags],
        },
        {
            "title": "Owner",
            "dict": [
                {
                    "dict": [
                        {"label": "URL", "value": f"https://www.instagram.com/{owner}/"},
                        {"label": "Name", "value": owner},
                        {"label": "Username", "value": owner},
                    ]
                }
            ],
        },
    ]
    return {
        "timestamp": int(post.date_utc.replace(tzinfo=UTC).timestamp()),
        "media": [],
        "label_values": label_values,
    }


def _build_loader(instaloader: Any, media_dir: Path) -> Any:
    return instaloader.Instaloader(
        dirname_pattern=str(media_dir),
        filename_pattern="{date_utc:%Y-%m-%d_%H-%M-%S}_UTC_{shortcode}",
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
        max_connection_attempts=2,
    )


def _sleep_jitter() -> None:
    time.sleep(_MIN_DELAY + (_MAX_DELAY - _MIN_DELAY) * os.urandom(1)[0] / 255)


def pull_saved(
    config: Config | None = None,
    *,
    max_count: int = 400,
    stop_after_known: int = 5,
    download_all: bool = False,
    progress_cb: ProgressCb | None = None,
    cancel_check: CancelCheck | None = None,
    _sleep: Callable[[], None] = _sleep_jitter,
) -> PullResult:
    """Walk the saved feed newest-first, download anything not already in the
    library, then hand the result to `import_zip` + `link_local_media`.

    Synchronous and slow (deliberate rate-limit delays) — the API runs it in
    a worker thread. `progress_cb` gets a dict after every post; when
    `cancel_check()` turns true the walk stops and whatever was downloaded so
    far is still imported.
    """
    config = config or get_config()
    instaloader = _load_instaloader()
    exc_mod = instaloader.exceptions

    state = get_session_state(config)
    if not state.configured or not state.username:
        raise InstagramNotConnectedError(
            "Not connected to Instagram yet — add a session on the Pull page first."
        )
    username = state.username

    def emit(**extra: Any) -> None:
        if progress_cb is not None:
            progress_cb({"scanned": result.scanned, "new": result.new, "downloaded": result.downloaded, **extra})

    def cancelled() -> bool:
        return cancel_check is not None and cancel_check()

    known = _known_shortcodes(config)
    logger.info("instagram pull: %d shortcodes already in the library", len(known))

    result = PullResult()
    tmp_root = Path(tempfile.mkdtemp(prefix="gramvault-pull-"))
    media_dir = tmp_root / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    try:
        loader = _build_loader(instaloader, media_dir)
        try:
            loader.load_session_from_file(username, str(_session_path(config, username)))
        except FileNotFoundError as exc:
            raise InstagramNotConnectedError(
                "The cached Instagram session file is gone — reconnect on the Pull page."
            ) from exc

        who = loader.test_login()
        if not who:
            raise InstagramAuthError(
                "The saved Instagram session is no longer valid — reconnect with fresh cookies."
            )
        _write_state(config, who)

        entries: list[dict[str, Any]] = []
        consecutive_known = 0
        consecutive_failures = 0

        profile = instaloader.Profile.own_profile(loader.context)
        try:
            for post in profile.get_saved_posts():
                if cancelled():
                    result.stopped_reason = "cancelled"
                    break
                result.scanned += 1
                if result.scanned > max_count:
                    result.stopped_reason = f"reached the {max_count}-post limit"
                    break

                shortcode = post.shortcode
                if shortcode in known:
                    consecutive_known += 1
                    if not download_all and consecutive_known >= stop_after_known:
                        result.stopped_reason = "caught up with the library"
                        break
                    emit()
                    continue
                consecutive_known = 0
                result.new += 1

                try:
                    loader.download_post(post, target="")
                    entries.append(_entry_for_post(post))
                    result.downloaded += 1
                    consecutive_failures = 0
                except exc_mod.InstaloaderException as exc:
                    # Any per-post failure — a 403/400 on one post, a post
                    # deleted mid-walk — not just ConnectionException (the
                    # only one caught before): anything else aborted the
                    # walk, and the `finally` below discarded every download.
                    result.failed += 1
                    consecutive_failures += 1
                    logger.warning("instagram pull: %s failed: %s", shortcode, exc)
                    if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                        result.stopped_reason = (
                            f"stopped after {consecutive_failures} failed downloads in a row "
                            f"(last: {exc})"
                        )
                        break
                emit()
                _sleep()
        except exc_mod.InstaloaderException as exc:
            # The saved-feed walk itself failed (rate limited, session
            # expired mid-walk): import what was downloaded so far.
            logger.warning("instagram pull: walking the saved feed failed: %s", exc)
            result.stopped_reason = f"stopped early: {exc}"

        if not entries:
            return result

        entries.sort(key=lambda e: e["timestamp"])
        zip_path = tmp_root / "saved_delta.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                _SAVED_POSTS_ARCNAME,
                json.dumps({"saved_saved_media": entries}, ensure_ascii=False, indent=1),
            )

        before_ids = _all_item_ids(config)
        job = import_zip(zip_path, config)
        result.imported = max(0, (job.processed_items or 0) - (job.failed_items or 0))
        after_ids = _all_item_ids(config)
        result.new_item_ids = sorted(after_ids - before_ids)

        # copy=True: the temp media tree is about to be deleted, and it may be
        # on a different filesystem than the library store (no hardlinks).
        report = link_local_media(media_dir, config, copy=True)
        result.linked = report.items_linked
        logger.info("instagram pull: %s; %s", result.as_dict(), report.summary())
        return result
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _all_item_ids(config: Config) -> set[int]:
    with session_scope(config) as conn:
        return {row["id"] for row in conn.execute("SELECT id FROM items")}
