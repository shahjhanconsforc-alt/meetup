"""Saving a browser sign-in as an account's session, and reopening it.

This is the path that makes a Google/Apple/Facebook login usable: those never
touch Meetup's password form, so the profile's cookie store is the ONLY record
of them. Save it once and every later run reuses it.
"""
from __future__ import annotations

import asyncio

import pytest

from backend import accounts, config, db, sessions
from tests.conftest import to_chrome_time

COOKIE = config.SESSION_COOKIE


@pytest.fixture
def browser_stub(monkeypatch):
    """Records closes and navigations; `on_close` fakes Chrome's cookie flush."""
    calls = {"closed": [], "opened": [], "url": ""}
    hooks = {"on_close": None}

    async def _close(*, profile=None):
        calls["closed"].append(profile)
        if hooks["on_close"]:
            hooks["on_close"]()
        return {"closed": True}

    async def _current(profile=None):
        class Page:
            url = calls["url"]
        return Page()

    async def _open(*, profile=None, url=None):
        calls["opened"].append({"profile": profile, "url": url})
        calls["url"] = url or ""
        class Page:
            pass
        return Page()

    async def _sleep(_seconds):
        return None

    monkeypatch.setattr(accounts.browser, "close_browser", _close)
    monkeypatch.setattr(accounts.browser, "current_page", _current)
    monkeypatch.setattr(accounts.browser, "open_page", _open)
    monkeypatch.setattr(accounts.asyncio, "sleep", _sleep)
    calls["hooks"] = hooks
    return calls


# ── Saving ───────────────────────────────────────────────────────────────

def test_saving_closes_the_browser_first(account, browser_stub, make_profile, future):
    """Closing IS the save. Chrome keeps its cookie store in memory and holds
    the file locked while it runs, so a session is neither durable nor readable
    until the process exits cleanly."""
    browser_stub["hooks"]["on_close"] = lambda: make_profile(
        f"acct-{account['id']}", [(".meetup.com", COOKIE, to_chrome_time(future))])

    result = asyncio.run(accounts.save_session(account["id"]))

    assert browser_stub["closed"] == [sessions.profile_dir(account["id"])]
    assert result["ok"] is True
    assert result["session"]["logged_in"] is True


def test_a_saved_session_is_written_to_the_store(account, browser_stub,
                                                 make_profile, future):
    browser_stub["hooks"]["on_close"] = lambda: make_profile(
        f"acct-{account['id']}", [(".meetup.com", COOKIE, to_chrome_time(future))])

    asyncio.run(accounts.save_session(account["id"]))

    stored = db.get_account(account["id"])
    assert stored["logged_in"] is True
    assert stored["last_result"] == "Session saved"


def test_a_saved_session_is_locked_against_clear_all(account, browser_stub,
                                                     make_profile, future):
    """An SSO or hand-solved login cannot be recreated by the login form, so a
    blanket "clear all sessions" must not be able to take it."""
    browser_stub["hooks"]["on_close"] = lambda: make_profile(
        f"acct-{account['id']}", [(".meetup.com", COOKIE, to_chrome_time(future))])
    asyncio.run(accounts.save_session(account["id"]))

    result = sessions.clear_all()

    assert result["skipped"] == [account["id"]]
    assert sessions.profile_dir(account["id"]).exists()


def test_saving_nothing_says_so_rather_than_claiming_success(account, browser_stub):
    result = asyncio.run(accounts.save_session(account["id"]))

    assert result["ok"] is False
    assert result["saved"] is False
    assert "Sign in in the browser first" in result["detail"]
    assert db.get_account(account["id"])["logged_in"] is False


def test_an_expired_cookie_is_not_a_saved_session(account, browser_stub,
                                                  make_profile, past):
    browser_stub["hooks"]["on_close"] = lambda: make_profile(
        f"acct-{account['id']}", [(".meetup.com", COOKIE, to_chrome_time(past))])

    result = asyncio.run(accounts.save_session(account["id"]))

    assert result["ok"] is False
    assert "Session expired" in result["detail"]


def test_saving_an_unknown_account_raises(browser_stub):
    with pytest.raises(KeyError):
        asyncio.run(accounts.save_session(9999))


# ── Reopening ────────────────────────────────────────────────────────────

def test_open_session_opens_meetup_on_the_stored_profile(account, browser_stub,
                                                         make_profile, future):
    make_profile(f"acct-{account['id']}",
                 [(".meetup.com", COOKIE, to_chrome_time(future))])

    result = asyncio.run(accounts.open_session(account["id"]))

    assert result["ok"] is True
    assert browser_stub["opened"] == [{"profile": sessions.profile_dir(account["id"]),
                                       "url": config.BASE_URL}]


def test_open_session_refuses_when_there_is_nothing_stored(account, browser_stub):
    """Opening a logged-out window would look identical to a logged-in one until
    you clicked something — and the next run would then act half-signed-in."""
    result = asyncio.run(accounts.open_session(account["id"]))

    assert result["ok"] is False
    assert "No stored session" in result["detail"]
    assert browser_stub["opened"] == []


def test_open_session_does_not_re_navigate_a_browser_already_on_meetup(
        account, browser_stub, make_profile, future):
    make_profile(f"acct-{account['id']}",
                 [(".meetup.com", COOKIE, to_chrome_time(future))])
    browser_stub["url"] = config.BASE_URL + "/messages/"

    asyncio.run(accounts.open_session(account["id"]))

    assert browser_stub["opened"] == []


def test_a_profile_held_open_by_chrome_counts_as_already_open(account, browser_stub,
                                                              monkeypatch):
    """Windows locks the cookie DB while Chrome runs, so "unreadable" there means
    the session is live in front of the operator — not that it is missing."""
    monkeypatch.setattr(sessions, "_read_session", lambda _p: {
        "logged_in": False, "expires_at": None, "checked_at": "now",
        "detail": "Browser is open — close it to read the session",
        "locked_by_browser": True})

    result = asyncio.run(accounts.open_session(account["id"]))

    assert result["ok"] is True
    assert result["already_open"] is True
    assert browser_stub["opened"] == []


def test_open_session_on_unknown_account_raises(browser_stub):
    with pytest.raises(KeyError):
        asyncio.run(accounts.open_session(9999))


# ── The sessions route marks what the browser has open ───────────────────

def test_the_route_marks_the_open_profile_active(account, monkeypatch,
                                                 make_profile, future):
    """The cookie-lock signal only exists on Windows. The runner knows which
    profile it has open on every platform, so the route joins that in."""
    from fastapi.testclient import TestClient

    from backend.main import app
    from backend.automation import playwright_runner

    make_profile(f"acct-{account['id']}",
                 [(".meetup.com", COOKIE, to_chrome_time(future))])

    async def _status():
        return {"open": True, "profile": str(sessions.profile_dir(account["id"])),
                "pages": 1}

    monkeypatch.setattr(playwright_runner, "status", _status)

    with TestClient(app) as client:
        rows = client.get("/api/accounts/sessions").json()["sessions"]

    row = next(r for r in rows if r["id"] == account["id"])
    assert row["active"] is True
    assert "Active" in row["detail"]


def test_the_route_leaves_other_accounts_inactive(account, monkeypatch,
                                                  make_profile, future):
    from fastapi.testclient import TestClient

    from backend.main import app
    from backend.automation import playwright_runner

    make_profile(f"acct-{account['id']}",
                 [(".meetup.com", COOKIE, to_chrome_time(future))])

    async def _status():
        return {"open": True, "profile": r"C:\somewhere\else", "pages": 1}

    monkeypatch.setattr(playwright_runner, "status", _status)

    with TestClient(app) as client:
        rows = client.get("/api/accounts/sessions").json()["sessions"]

    row = next(r for r in rows if r["id"] == account["id"])
    assert row["active"] is False
    assert row["logged_in"] is True          # still stored, just not open


# ── Editing an account without losing its session ────────────────────────

def test_the_login_can_be_corrected_without_touching_the_profile(
        account, make_profile, future):
    """Before this, fixing a wrong address meant delete-and-re-add — which
    deletes the profile directory, and the profile IS the session."""
    make_profile(f"acct-{account['id']}",
                 [(".meetup.com", COOKIE, to_chrome_time(future))])

    updated = accounts.update_account(account["id"],
                                      {"login": "real@example.com", "label": "Google"})

    assert updated["login"] == "real@example.com"
    assert updated["label"] == "Google"
    assert sessions.profile_dir(account["id"]).exists()
    assert sessions.describe(db.get_account(account["id"]))["logged_in"] is True


def test_an_omitted_password_is_left_alone(account):
    accounts.update_account(account["id"], {"label": "Renamed"})
    assert db.get_account(account["id"])["password"] == "hunter2"


def test_an_update_cannot_blank_the_login(account):
    with pytest.raises(ValueError, match="email or username"):
        accounts.update_account(account["id"], {"login": "  "})


def test_an_update_cannot_collide_with_another_account(account):
    other = accounts.add_account("Other", "other@example.com", "pw")
    with pytest.raises(ValueError, match="already stored"):
        accounts.update_account(other["id"], {"login": "TESTER@example.com"})


def test_an_account_can_keep_its_own_login_on_update(account):
    updated = accounts.update_account(account["id"],
                                      {"login": "tester@example.com", "label": "Same"})
    assert updated["label"] == "Same"


def test_the_update_response_carries_no_password(account):
    updated = accounts.update_account(account["id"], {"password": "new-secret"})
    assert "password" not in updated
    assert "new-secret" not in str(updated)
    assert db.get_account(account["id"])["password"] == "new-secret"


def test_updating_an_unknown_account_raises():
    with pytest.raises(KeyError):
        accounts.update_account(9999, {"label": "x"})
