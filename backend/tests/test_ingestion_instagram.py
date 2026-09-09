"""Tests for gramvault.ingestion.instagram — the opt-in §F "pull my saved
posts" path.

`instaloader` is an optional dependency and isn't installed in the test
env, so every test swaps in a fake module via
`instagram._load_instaloader`. The real code under test is: cookie
parsing, session-file bookkeeping, the saved-feed walk (stop-on-known,
max cap, cancel), and that a walk's output is handed to the *real*
`import_zip` + `link_local_media` unchanged.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from gramvault.config import Config, PathsConfig, PullConfig
from gramvault.db.session import session_scope
from gramvault.ingestion import instagram

# --- a fake instaloader module -------------------------------------------


class _FakePost:
    def __init__(self, shortcode: str, *, owner: str = "someone", caption: str = "hi"):
        self.shortcode = shortcode
        self.owner_username = owner
        self.caption = caption
        self.caption_hashtags = ["tag"]
        self.date_utc = datetime(2026, 1, 2, 3, 4, 5)


class _ConnectionException(Exception):
    pass


class _FakeLoader:
    # class-level knobs the tests set
    saved_posts: list[_FakePost] = []
    login_user: str | None = "tester"
    fail_downloads: set[str] = set()

    def __init__(self, **kwargs):
        self.dirname_pattern = kwargs.get("dirname_pattern")
        self.context = SimpleNamespace(
            _session=SimpleNamespace(cookies={}), username=None
        )

    def test_login(self):
        return _FakeLoader.login_user

    def save_session_to_file(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("fake-session", encoding="utf-8")

    def load_session_from_file(self, username, path):
        if not Path(path).is_file():
            raise FileNotFoundError(path)

    def download_post(self, post, target=""):
        if post.shortcode in _FakeLoader.fail_downloads:
            raise _ConnectionException("boom")
        out = Path(self.dirname_pattern)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"2026-01-02_03-04-05_UTC_{post.shortcode}.jpg").write_bytes(
            b"\xff\xd8\xff" + post.shortcode.encode()
        )


class _FakeProfile:
    @staticmethod
    def own_profile(context):
        return SimpleNamespace(get_saved_posts=lambda: iter(list(_FakeLoader.saved_posts)))


def _fake_module() -> SimpleNamespace:
    return SimpleNamespace(
        Instaloader=_FakeLoader,
        Profile=_FakeProfile,
        exceptions=SimpleNamespace(ConnectionException=_ConnectionException),
    )


@pytest.fixture(autouse=True)
def _fake_instaloader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(instagram, "_load_instaloader", _fake_module)
    _FakeLoader.saved_posts = []
    _FakeLoader.login_user = "tester"
    _FakeLoader.fail_downloads = set()


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        paths=PathsConfig(
            library_dir=str(tmp_path / "library"),
            db_path=str(tmp_path / "data" / "gramvault.db"),
            chroma_dir=str(tmp_path / "data" / "chroma"),
        ),
        pull=PullConfig(enabled=True, session_dir=str(tmp_path / "ig")),
    )


def _init_db(cfg: Config) -> None:
    from gramvault.db.session import get_connection, init_db

    conn = get_connection(cfg)
    init_db(conn)
    conn.close()


# --- cookie parsing -----------------------------------------------------


def test_parse_cookies_accepts_json_object():
    assert instagram.parse_cookies('{"sessionid": "abc", "csrftoken": "x"}') == {
        "sessionid": "abc",
        "csrftoken": "x",
    }


def test_parse_cookies_accepts_cookie_editor_array():
    text = '[{"name": "sessionid", "value": "abc"}, {"name": "ds_user_id", "value": "42"}]'
    assert instagram.parse_cookies(text) == {"sessionid": "abc", "ds_user_id": "42"}


def test_parse_cookies_accepts_netscape():
    text = (
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tabc\n"
        ".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tx\n"
        ".example.com\tTRUE\t/\tTRUE\t0\tirrelevant\tnope\n"
    )
    assert instagram.parse_cookies(text) == {"sessionid": "abc", "csrftoken": "x"}


def test_parse_cookies_drops_empty_values():
    assert instagram.parse_cookies('{"sessionid": "abc", "blank": ""}') == {"sessionid": "abc"}


def test_parse_cookies_rejects_garbage():
    with pytest.raises(instagram.InstagramAuthError):
        instagram.parse_cookies("{not json")


# --- connect / session state -----------------------------------------


def test_connect_requires_sessionid(cfg: Config):
    with pytest.raises(instagram.InstagramAuthError, match="sessionid"):
        instagram.connect_from_cookies({"csrftoken": "x"}, cfg)


def test_connect_writes_session_and_state(cfg: Config):
    state = instagram.connect_from_cookies({"sessionid": "abc"}, cfg)
    assert state.configured and state.username == "tester"
    assert (cfg.resolved_pull_session_dir / "session-tester").is_file()

    reloaded = instagram.get_session_state(cfg)
    assert reloaded.configured and reloaded.username == "tester"
    assert reloaded.last_verified_at == state.last_verified_at


def test_connect_rejects_stale_cookies(cfg: Config):
    _FakeLoader.login_user = None
    with pytest.raises(instagram.InstagramAuthError, match="stale"):
        instagram.connect_from_cookies({"sessionid": "dead"}, cfg)


def test_session_state_unconfigured_when_nothing_cached(cfg: Config):
    assert instagram.get_session_state(cfg).configured is False


def test_disconnect_removes_session(cfg: Config):
    instagram.connect_from_cookies({"sessionid": "abc"}, cfg)
    instagram.disconnect(cfg)
    assert instagram.get_session_state(cfg).configured is False
    assert not (cfg.resolved_pull_session_dir / "session-tester").exists()


def test_pull_without_session_raises(cfg: Config):
    _init_db(cfg)
    with pytest.raises(instagram.InstagramNotConnectedError):
        instagram.pull_saved(cfg, _sleep=lambda: None)


# --- the walk + ingest ------------------------------------------------


def _external_ids(cfg: Config) -> set[str]:
    with session_scope(cfg) as conn:
        return {r["external_id"] for r in conn.execute("SELECT external_id FROM items")}


def _media_count(cfg: Config, external_id: str) -> int:
    with session_scope(cfg) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM media_files JOIN items ON items.id = media_files.item_id "
            "WHERE items.external_id = ?",
            (external_id,),
        ).fetchone()[0]


def test_pull_downloads_imports_and_links_new_posts(cfg: Config):
    _init_db(cfg)
    instagram.connect_from_cookies({"sessionid": "abc"}, cfg)
    _FakeLoader.saved_posts = [_FakePost("NEWaaa"), _FakePost("NEWbbb")]

    result = instagram.pull_saved(cfg, max_count=50, _sleep=lambda: None)

    assert result.new == 2
    assert result.downloaded == 2
    assert result.imported == 2
    assert result.linked == 2
    assert result.stopped_reason == "completed"
    assert _external_ids(cfg) == {"NEWaaa", "NEWbbb"}
    assert _media_count(cfg, "NEWaaa") == 1
    assert sorted(result.new_item_ids) == result.new_item_ids
    assert len(result.new_item_ids) == 2


def test_pull_stops_once_it_catches_up_with_the_library(cfg: Config):
    _init_db(cfg)
    instagram.connect_from_cookies({"sessionid": "abc"}, cfg)
    with session_scope(cfg) as conn:
        conn.execute(
            "INSERT INTO items (external_id, media_type) VALUES ('OLD1', 'photo'), ('OLD2', 'photo')"
        )
    _FakeLoader.saved_posts = [
        _FakePost("NEWaaa"),
        _FakePost("OLD1"),
        _FakePost("OLD2"),
        _FakePost("NEVER"),  # past the cutoff — must not be walked
    ]

    result = instagram.pull_saved(
        cfg, max_count=50, stop_after_known=2, _sleep=lambda: None
    )

    assert result.new == 1
    assert result.stopped_reason == "caught up with the library"
    assert "NEVER" not in _external_ids(cfg)


def test_pull_full_ignores_the_known_cutoff(cfg: Config):
    _init_db(cfg)
    instagram.connect_from_cookies({"sessionid": "abc"}, cfg)
    with session_scope(cfg) as conn:
        conn.execute("INSERT INTO items (external_id, media_type) VALUES ('OLD1', 'photo')")
    _FakeLoader.saved_posts = [
        _FakePost("OLD1"),
        _FakePost("OLD1"),
        _FakePost("NEWaaa"),
    ]

    result = instagram.pull_saved(
        cfg, max_count=50, stop_after_known=1, download_all=True, _sleep=lambda: None
    )
    assert "NEWaaa" in _external_ids(cfg)
    assert result.new == 1


def test_pull_respects_max_count(cfg: Config):
    _init_db(cfg)
    instagram.connect_from_cookies({"sessionid": "abc"}, cfg)
    _FakeLoader.saved_posts = [_FakePost(f"SC{i:03d}") for i in range(10)]

    result = instagram.pull_saved(cfg, max_count=3, _sleep=lambda: None)

    assert result.scanned == 4  # 3 walked + the one that trips the cap
    assert result.new == 3
    assert "the 3-post limit" in result.stopped_reason


def test_pull_cancel_still_imports_what_was_downloaded(cfg: Config):
    _init_db(cfg)
    instagram.connect_from_cookies({"sessionid": "abc"}, cfg)
    _FakeLoader.saved_posts = [_FakePost("AAA111"), _FakePost("BBB222"), _FakePost("CCC333")]

    calls = {"n": 0}

    def cancel_after_one() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    result = instagram.pull_saved(
        cfg, max_count=50, cancel_check=cancel_after_one, _sleep=lambda: None
    )

    assert result.stopped_reason == "cancelled"
    assert result.new == 1
    assert _external_ids(cfg) == {"AAA111"}


def test_pull_counts_download_failures(cfg: Config):
    _init_db(cfg)
    instagram.connect_from_cookies({"sessionid": "abc"}, cfg)
    _FakeLoader.fail_downloads = {"BADbbb"}
    _FakeLoader.saved_posts = [_FakePost("GOODaa"), _FakePost("BADbbb")]

    result = instagram.pull_saved(cfg, max_count=50, _sleep=lambda: None)

    assert result.new == 2
    assert result.downloaded == 1
    assert result.failed == 1
    assert _external_ids(cfg) == {"GOODaa"}  # only the downloaded one imported


def test_pull_with_no_new_posts_is_a_noop(cfg: Config):
    _init_db(cfg)
    instagram.connect_from_cookies({"sessionid": "abc"}, cfg)
    _FakeLoader.saved_posts = []

    result = instagram.pull_saved(cfg, _sleep=lambda: None)
    assert result.new == 0
    assert result.imported == 0
    assert _external_ids(cfg) == set()


# --- local-browser cookie discovery --------------------------------------


def test_connect_from_local_browser_errors_without_firefox(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(instagram, "read_firefox_cookies", lambda: None)
    with pytest.raises(instagram.InstagramAuthError, match="different machine"):
        instagram.connect_from_local_browser(cfg)


def test_read_firefox_cookies_reads_moz_cookies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import sqlite3

    profile = tmp_path / "ff" / "abcd.default"
    profile.mkdir(parents=True)
    db = profile / "cookies.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT)")
    conn.executemany(
        "INSERT INTO moz_cookies VALUES (?, ?, ?)",
        [
            ("sessionid", "live", ".instagram.com"),
            ("csrftoken", "tok", ".instagram.com"),
            ("sb", "x", ".google.com"),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(instagram, "_firefox_profile_roots", lambda: [tmp_path / "ff"])
    jar = instagram.read_firefox_cookies()
    assert jar == {"sessionid": "live", "csrftoken": "tok"}
