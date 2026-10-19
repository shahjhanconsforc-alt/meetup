"""Is this account logged in? — sessions.py reads that off the cookie store.

The whole login state of the app rests on one SQLite read, so these cover the
states a real profile directory turns up in: absent, present but empty, holding
a live cookie, holding an expired one, and unreadable.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from backend import config, db, sessions
from tests.conftest import to_chrome_time

COOKIE = config.SESSION_COOKIE  # "MEETUP_SESSION"


# ── Reading a profile ────────────────────────────────────────────────────

def test_missing_profile_is_logged_out(tmp_path):
    state = sessions._read_session(tmp_path / "nope")
    assert state["logged_in"] is False
    assert state["detail"] == "No profile yet"


def test_profile_without_cookie_store_is_logged_out(tmp_path):
    profile = tmp_path / "empty"
    profile.mkdir()
    state = sessions._read_session(profile)
    assert state["logged_in"] is False
    assert state["detail"] == "No cookie store"


def test_cookie_store_without_session_cookie_is_logged_out(make_profile, future):
    """The real acct-3 state: meetup.com cookies present, but none of them auth.

    A browser-id or consent cookie proves the site was visited, not that anyone
    signed in — treating those as a session is how a logged-out account gets
    handed to a campaign.
    """
    profile = make_profile("visited", [
        (".meetup.com", "MEETUP_BROWSER_ID", to_chrome_time(future)),
        (".meetup.com", "OptanonConsent", to_chrome_time(future)),
    ])
    state = sessions._read_session(profile)
    assert state["logged_in"] is False
    assert state["detail"] == "No session cookie"


def test_unexpired_session_cookie_is_logged_in(make_profile, future):
    profile = make_profile("live", [(".meetup.com", COOKIE, to_chrome_time(future))])
    state = sessions._read_session(profile)
    assert state["logged_in"] is True
    assert state["detail"] == "Logged in"
    assert state["expires_at"].startswith(str(future.year))


def test_expired_session_cookie_is_logged_out(make_profile, past):
    profile = make_profile("stale", [(".meetup.com", COOKIE, to_chrome_time(past))])
    state = sessions._read_session(profile)
    assert state["logged_in"] is False
    assert state["detail"] == "Session expired"
    assert state["expires_at"] is not None      # still reported, so the UI can say when


def test_zero_expiry_cookie_counts_as_logged_in(make_profile):
    """Chrome only writes a no-expiry session cookie to disk when it is live."""
    profile = make_profile("nonpersistent", [(".meetup.com", COOKIE, 0)])
    state = sessions._read_session(profile)
    assert state["logged_in"] is True
    assert state["detail"] == "Session cookie (no expiry)"
    assert state["expires_at"] is None


def test_www_host_key_also_counts(make_profile, future):
    """Meetup sets the session on `.meetup.com`, but a host-scoped copy on
    `www.meetup.com` is the same session and must not read as logged out."""
    profile = make_profile("www", [("www.meetup.com", COOKIE, to_chrome_time(future))])
    assert sessions._read_session(profile)["logged_in"] is True


def test_other_sites_session_cookie_is_ignored(make_profile, future):
    """A cookie of the same name from another domain is not a Meetup login."""
    profile = make_profile("other", [("example.com", COOKIE, to_chrome_time(future))])
    assert sessions._read_session(profile)["logged_in"] is False


def test_newest_of_several_cookie_files_wins(make_profile, tmp_path, future, past):
    """Chrome moved the cookie DB between versions; both paths are checked and
    the freshest file is the one that reflects reality."""
    profile = make_profile("dual", [(".meetup.com", COOKIE, to_chrome_time(past))],
                           rel="Default/Cookies")
    newer = profile / "Default/Network/Cookies"
    newer.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(newer)
    conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT,"
                 " expires_utc INTEGER)")
    conn.execute("INSERT INTO cookies VALUES (?,?,?,?)",
                 (".meetup.com", COOKIE, "x", to_chrome_time(future)))
    conn.commit()
    conn.close()
    # Make the new file unambiguously newer than the one make_profile wrote.
    import os
    stale = os.stat(profile / "Default/Cookies").st_mtime
    os.utime(newer, (stale + 100, stale + 100))

    assert sessions._read_session(profile)["logged_in"] is True


def test_corrupt_cookie_store_is_logged_out_not_an_exception(tmp_path):
    """A half-written or locked profile must degrade to "logged out", never take
    the sweep down with it — one bad profile would otherwise hide every other
    account's state on boot."""
    profile = tmp_path / "corrupt"
    (profile / "Default/Network").mkdir(parents=True)
    (profile / "Default/Network/Cookies").write_bytes(b"not a database at all")
    state = sessions._read_session(profile)
    assert state["logged_in"] is False
    assert "unreadable" in state["detail"]


def test_read_is_non_destructive(make_profile, future):
    """The cookie DB is copied before reading. Chrome holds the live file open,
    and opening it read-write would risk the profile a login was paid for."""
    profile = make_profile("intact", [(".meetup.com", COOKIE, to_chrome_time(future))])
    cookies = profile / "Default/Network/Cookies"
    before = cookies.read_bytes()
    sessions._read_session(profile)
    assert cookies.read_bytes() == before


def test_chrome_epoch_conversion_round_trips(make_profile):
    """The 1601-vs-1970 offset is the one number here that silently produces a
    wrong answer rather than an error — a bad offset reads every live cookie as
    expired (or every expired one as live)."""
    when = datetime(2030, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    profile = make_profile("epoch", [(".meetup.com", COOKIE, to_chrome_time(when))])
    state = sessions._read_session(profile)
    assert state["expires_at"] == when.isoformat(timespec="seconds")


# ── The store-writing wrappers ───────────────────────────────────────────

def test_describe_shows_the_account_and_its_session(account, make_profile, future):
    make_profile(f"acct-{account['id']}",
                 [(".meetup.com", COOKIE, to_chrome_time(future))])
    row = sessions.describe(db.get_account(account["id"]))
    assert row["id"] == account["id"]
    assert row["login"] == "tester@example.com"
    assert row["logged_in"] is True
    assert row["locked"] is False
    assert "password" not in row


def test_refresh_writes_the_session_back_to_the_store(account, make_profile, future):
    make_profile(f"acct-{account['id']}",
                 [(".meetup.com", COOKIE, to_chrome_time(future))])
    state = sessions.refresh(account["id"])
    stored = db.get_account(account["id"])
    assert state["logged_in"] is True
    assert stored["logged_in"] is True
    assert stored["session_expires"] == state["expires_at"]
    assert stored["last_checked"] == state["checked_at"]


def test_refresh_clears_a_stale_logged_in_flag(account, make_profile, past):
    """store.json's flag is a guess; the cookie file is the truth. A session that
    expired while the server was closed must not still read as logged in."""
    db.update_account(account["id"], {"logged_in": True})
    make_profile(f"acct-{account['id']}", [(".meetup.com", COOKIE, to_chrome_time(past))])
    sessions.refresh(account["id"])
    assert db.get_account(account["id"])["logged_in"] is False


# ── A profile a live browser holds open ──────────────────────────────────

@pytest.fixture
def locked_profile(monkeypatch):
    """Chrome running: the cookie DB is there but cannot be read at all."""
    real = sessions._read_session

    def _locked(profile):
        state = real(profile)
        return {**state, "logged_in": False, "expires_at": None,
                "detail": "Browser is open — close it to read the session",
                "locked_by_browser": True}

    monkeypatch.setattr(sessions, "_read_session", _locked)


def test_an_open_browser_does_not_read_as_logged_out(account, locked_profile):
    """Unreadable is not logged out — it is the opposite. Reporting it as logged
    out turns a card grey the instant its session is opened."""
    db.update_account(account["id"], {"logged_in": True,
                                      "session_expires": "2027-08-10T12:39:24+00:00"})

    row = sessions.describe(db.get_account(account["id"]))

    assert row["active"] is True
    assert row["logged_in"] is True                     # remembered, not re-read
    assert row["expires_at"] == "2027-08-10T12:39:24+00:00"
    assert "Active" in row["detail"]


def test_refresh_on_an_open_browser_does_not_erase_the_session(account, locked_profile):
    """The bug this guards: a Check-login-status while the browser was open wrote
    logged_in=False, destroying the one record of a login that may have cost a
    captcha to obtain."""
    db.update_account(account["id"], {"logged_in": True,
                                      "session_expires": "2027-08-10T12:39:24+00:00"})

    sessions.refresh(account["id"])

    stored = db.get_account(account["id"])
    assert stored["logged_in"] is True
    assert stored["session_expires"] == "2027-08-10T12:39:24+00:00"
    assert stored["last_checked"]                       # the check still happened


def test_sweep_counts_an_open_browser_as_active(account, locked_profile):
    db.update_account(account["id"], {"logged_in": True})
    result = sessions.sweep()
    assert result == {"checked": 1, "logged_in": 1, "active": 1}


def test_an_open_browser_with_nothing_stored_stays_logged_out(account, locked_profile):
    """Active but never signed in: the window is open, the session isn't there
    yet. It must not claim a session it doesn't have."""
    row = sessions.describe(db.get_account(account["id"]))
    assert row["active"] is True
    assert row["logged_in"] is False


def test_a_readable_profile_is_not_active(account, make_profile, future):
    make_profile(f"acct-{account['id']}",
                 [(".meetup.com", COOKIE, to_chrome_time(future))])
    row = sessions.describe(db.get_account(account["id"]))
    assert row["active"] is False
    assert row["logged_in"] is True


def test_refresh_on_unknown_account_raises():
    with pytest.raises(KeyError):
        sessions.refresh(9999)


def test_sweep_counts_live_sessions(make_profile, future, past):
    from backend import accounts
    live = accounts.add_account("Live", "live@example.com", "pw")
    dead = accounts.add_account("Dead", "dead@example.com", "pw")
    make_profile(f"acct-{live['id']}", [(".meetup.com", COOKIE, to_chrome_time(future))])
    make_profile(f"acct-{dead['id']}", [(".meetup.com", COOKIE, to_chrome_time(past))])

    result = sessions.sweep()
    assert result == {"checked": 2, "logged_in": 1, "active": 0}
    assert db.get_account(live["id"])["logged_in"] is True
    assert db.get_account(dead["id"])["logged_in"] is False


def test_profile_dir_is_self_describing(account):
    assert sessions.profile_dir(account["id"]).name == f"acct-{account['id']}"


# ── Clearing ─────────────────────────────────────────────────────────────

def test_clear_deletes_the_profile_but_keeps_the_credentials(account, make_profile, future):
    make_profile(f"acct-{account['id']}",
                 [(".meetup.com", COOKIE, to_chrome_time(future))])
    result = sessions.clear(account["id"])

    assert result["cleared"] is True
    assert not sessions.profile_dir(account["id"]).exists()
    stored = db.get_account(account["id"])
    assert stored is not None
    assert stored["password"] == "hunter2"     # clearing a login is not deleting an account
    assert stored["logged_in"] is False


def test_clear_with_no_profile_is_a_success(account):
    """The desired state — no stored session — already holds."""
    result = sessions.clear(account["id"])
    assert result["cleared"] is True
    assert result["detail"] == "Nothing to clear"


def test_clear_all_skips_locked_sessions(make_profile, future):
    from backend import accounts
    keep = accounts.add_account("Keep", "keep@example.com", "pw")
    drop = accounts.add_account("Drop", "drop@example.com", "pw")
    make_profile(f"acct-{keep['id']}", [(".meetup.com", COOKIE, to_chrome_time(future))])
    make_profile(f"acct-{drop['id']}", [(".meetup.com", COOKIE, to_chrome_time(future))])
    sessions.sweep()                    # both now genuinely read as logged in
    sessions.lock(keep["id"])

    result = sessions.clear_all()

    assert result["ok"] is True
    assert result["skipped"] == [keep["id"]]
    assert result["cleared"] == [drop["id"]]
    assert sessions.profile_dir(keep["id"]).exists()
    assert not sessions.profile_dir(drop["id"]).exists()
    # A locked session's flag must survive too, or the UI shows it as logged out.
    assert db.get_account(keep["id"])["logged_in"] is True


def test_lock_and_unlock_round_trip(account):
    sessions.lock(account["id"])
    assert db.get_account(account["id"])["session_locked"] is True
    sessions.unlock(account["id"])
    assert db.get_account(account["id"])["session_locked"] is False


def test_clear_all_keeps_every_account(make_profile, future):
    from backend import accounts
    accounts.add_account("A", "a@example.com", "pw")
    accounts.add_account("B", "b@example.com", "pw")
    sessions.clear_all()
    assert len(db.list_accounts()) == 2


def test_default_profile_reports_the_env_login(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MU_USERNAME", "env-user@example.com")
    monkeypatch.setattr(config, "PLAYWRIGHT_USER_DATA_DIR", tmp_path / "chrome-profile")
    row = sessions.default_profile()
    assert row["id"] is None
    assert row["login"] == "env-user@example.com"
    assert row["logged_in"] is False
