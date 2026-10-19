"""Credentials, and what happens when we log them in.

Nothing here launches a browser: `meetup.login` is the seam, and it is stubbed.
What is under test is everything around it — what the API is allowed to see, what
gets written back to the store, and the sequencing that keeps a set of accounts
from looking like a set.

Tests are plain `def`s driving coroutines through `asyncio.run`, so the suite
needs no pytest-asyncio.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend import accounts, config, db, sessions
from tests.conftest import to_chrome_time

COOKIE = config.SESSION_COOKIE


@pytest.fixture
def no_gap(monkeypatch):
    """Collapse the inter-login pause so the suite doesn't sit for 8-20s."""
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(accounts.asyncio, "sleep", _sleep)
    return slept


@pytest.fixture
def stub_login(monkeypatch):
    """Replace the browser login with a scripted result. Returns the call log."""
    def _stub(result=None, results=None):
        calls: list[dict] = []
        queue = list(results or [])

        async def _login(*, profile, login, password):
            calls.append({"profile": profile, "login": login, "password": password})
            if queue:
                return queue.pop(0)
            return result or {"ok": True, "detail": "Logged in"}

        monkeypatch.setattr(accounts.meetup, "login", _login)
        return calls

    return _stub


# ── What the API may see ─────────────────────────────────────────────────

def test_listing_accounts_never_includes_the_password(account):
    row = accounts.list_accounts()[0]
    assert "password" not in row
    assert row["has_password"] is True
    assert row["login"] == "tester@example.com"


def test_public_scrubs_a_password_added_later():
    """_public is an allow-by-omission filter over the whole dict, so a field
    added to the store later cannot reintroduce the credential."""
    out = accounts._public({"id": 1, "login": "a@b.c", "password": "s3cret",
                            "note": "added later"})
    assert "s3cret" not in str(out)
    assert out["note"] == "added later"


def test_has_password_is_false_when_none_is_stored():
    out = accounts._public({"id": 1, "login": "a@b.c", "password": ""})
    assert out["has_password"] is False


def test_public_passes_none_through():
    assert accounts._public(None) is None


def test_reveal_returns_the_stored_password(account):
    assert accounts.reveal(account["id"]) == "hunter2"


def test_reveal_on_unknown_account_raises():
    with pytest.raises(KeyError):
        accounts.reveal(4242)


def test_add_returns_a_scrubbed_account(account):
    assert "password" not in account
    assert account["has_password"] is True


# ── Adding ───────────────────────────────────────────────────────────────

def test_login_is_required():
    with pytest.raises(ValueError, match="email or username"):
        accounts.add_account("Label", "   ", "pw")


def test_password_is_required():
    with pytest.raises(ValueError, match="[Pp]assword is required"):
        accounts.add_account("Label", "user@example.com", "")


def test_duplicate_login_is_rejected_case_insensitively(account):
    with pytest.raises(ValueError, match="already stored"):
        accounts.add_account("Other", "TESTER@example.com", "different")
    assert len(db.list_accounts()) == 1


def test_login_is_trimmed_before_storing():
    accounts.add_account("Spaced", "  user@example.com  ", "pw")
    assert db.list_accounts()[0]["login"] == "user@example.com"


def test_a_rejected_add_stores_nothing():
    with pytest.raises(ValueError):
        accounts.add_account("Label", "", "pw")
    assert db.list_accounts() == []


# ── Removing ─────────────────────────────────────────────────────────────

def test_remove_deletes_the_account_and_its_session(account, make_profile, future):
    make_profile(f"acct-{account['id']}",
                 [(".meetup.com", COOKIE, to_chrome_time(future))])
    assert accounts.remove_account(account["id"]) is True
    assert db.get_account(account["id"]) is None
    # A live Meetup session with nothing in the UI pointing at it is the thing
    # this is guarding against.
    assert not sessions.profile_dir(account["id"]).exists()


def test_remove_unknown_account_reports_false():
    assert accounts.remove_account(9999) is False


# ── Logging in ───────────────────────────────────────────────────────────

def test_login_reuses_a_live_session_instead_of_signing_in_again(
        account, make_profile, future, stub_login):
    """The cheapest login is the one that is skipped — resubmitting a form
    Meetup doesn't need is a free opportunity for it to raise a challenge."""
    make_profile(f"acct-{account['id']}",
                 [(".meetup.com", COOKIE, to_chrome_time(future))])
    calls = stub_login()

    result = asyncio.run(accounts.login_account(account["id"]))

    assert calls == []                       # the browser was never opened
    assert result["skipped"] is True
    assert result["session"]["logged_in"] is True
    assert db.get_account(account["id"])["last_result"] == "Already logged in"


def test_login_signs_in_when_the_session_is_expired(
        account, make_profile, past, future, stub_login):
    profile = make_profile(f"acct-{account['id']}",
                           [(".meetup.com", COOKIE, to_chrome_time(past))])
    calls = stub_login()

    result = asyncio.run(accounts.login_account(account["id"]))

    assert len(calls) == 1
    assert calls[0]["login"] == "tester@example.com"
    assert calls[0]["password"] == "hunter2"     # the real credential reaches the form
    assert calls[0]["profile"] == sessions.profile_dir(account["id"])
    assert result["skipped"] is False


def test_a_failed_login_is_recorded_not_raised(account, stub_login):
    stub_login({"ok": False, "detail": "Meetup asked for a code or captcha"})

    result = asyncio.run(accounts.login_account(account["id"]))

    assert result["ok"] is False
    stored = db.get_account(account["id"])
    assert stored["last_result"] == "Meetup asked for a code or captcha"
    assert stored["logged_in"] is False


def test_login_writes_the_session_state_back(account, stub_login, make_profile, future):
    """After a login the store must reflect the cookie file, not the browser's
    say-so — `refresh` re-reads disk and that is what the panel renders."""
    def _login(*, profile, login, password):
        make_profile(f"acct-{account['id']}",
                     [(".meetup.com", COOKIE, to_chrome_time(future))])
        return {"ok": True, "detail": "Logged in"}

    async def _async_login(**kw):
        return _login(**kw)

    import backend.automation.meetup as meetup
    original = meetup.login
    meetup.login = _async_login
    try:
        result = asyncio.run(accounts.login_account(account["id"]))
    finally:
        meetup.login = original

    assert result["session"]["logged_in"] is True
    stored = db.get_account(account["id"])
    assert stored["logged_in"] is True
    assert stored["last_result"] == "Logged in"
    assert stored["last_login"]


def test_login_on_unknown_account_raises(stub_login):
    stub_login()
    with pytest.raises(KeyError):
        asyncio.run(accounts.login_account(9999))


def test_login_with_no_stored_password_fails_cleanly(monkeypatch, stub_login):
    """meetup.login's own contract: missing credentials is a result, not a raise."""
    acct = accounts.add_account("Empty", "empty@example.com", "pw")
    db.update_account(acct["id"], {"password": ""})
    calls = stub_login({"ok": False, "detail": "No credentials stored"})

    result = asyncio.run(accounts.login_account(acct["id"]))

    assert calls[0]["password"] == ""
    assert result["ok"] is False
    assert db.get_account(acct["id"])["last_result"] == "No credentials stored"


# ── Manual login (the "just open meetup.com" button) ─────────────────────

@pytest.fixture
def stub_open(monkeypatch):
    """Replace the browser with a fake page. Returns the navigation log.

    `page.url` starts blank — a browser that has not been anywhere — so the
    default is that a navigation is needed.
    """
    calls: list[dict] = []

    class FakePage:
        url = ""

    page = FakePage()

    async def _current(profile=None):
        return page

    async def _open(*, profile=None, url=None):
        calls.append({"profile": profile, "url": url})
        page.url = url or ""
        return page

    monkeypatch.setattr(accounts.browser, "current_page", _current)
    monkeypatch.setattr(accounts.browser, "open_page", _open)
    return calls


def test_manual_login_opens_meetup_on_that_accounts_profile(account, stub_open):
    result = asyncio.run(accounts.open_account(account["id"]))

    assert stub_open == [{"profile": sessions.profile_dir(account["id"]),
                          "url": config.BASE_URL}]
    assert result["url"] == config.BASE_URL


def test_manual_login_twice_does_not_reload_meetup(account, stub_open):
    """Pressing it again half-way through a hand-solved captcha would reload the
    page and destroy the token — the same loop the login flow used to cause."""
    asyncio.run(accounts.open_account(account["id"]))
    asyncio.run(accounts.open_account(account["id"]))

    assert len(stub_open) == 1          # the second press left the page alone


def test_manual_login_types_nothing(account, stub_open, stub_login):
    """The whole point of the button: it hands the window to the operator. A
    login submitted behind their back is the thing they opened it to avoid."""
    calls = stub_login()
    asyncio.run(accounts.open_account(account["id"]))
    assert calls == []


def test_manual_login_rereads_the_session(account, stub_open, make_profile, future):
    """So the row's dot is right the moment the window opens — the profile may
    already hold a good session, which is why nothing needs typing."""
    make_profile(f"acct-{account['id']}",
                 [(".meetup.com", COOKIE, to_chrome_time(future))])
    result = asyncio.run(accounts.open_account(account["id"]))

    assert result["session"]["logged_in"] is True
    assert db.get_account(account["id"])["logged_in"] is True


def test_manual_login_response_carries_no_password(account, stub_open):
    result = asyncio.run(accounts.open_account(account["id"]))
    assert "password" not in result["account"]
    assert result["account"]["has_password"] is True


def test_manual_login_on_unknown_account_raises(stub_open):
    with pytest.raises(KeyError):
        asyncio.run(accounts.open_account(9999))


# ── Logging several in ───────────────────────────────────────────────────

def test_logins_run_one_at_a_time_with_a_randomised_gap(no_gap, stub_login):
    """Simultaneous logins from one IP is the clearest signal that a set of
    accounts belongs together, so the gap is not optional and not fixed."""
    ids = [accounts.add_account(f"A{i}", f"a{i}@example.com", "pw")["id"]
           for i in range(3)]
    calls = stub_login()

    asyncio.run(accounts.login_accounts(ids))

    assert [c["login"] for c in calls] == ["a0@example.com", "a1@example.com",
                                           "a2@example.com"]
    assert len(no_gap) == 2                          # gaps between, not after the last
    assert len(set(no_gap)) == len(no_gap)           # randomised, not a metronome
    low, high = accounts._LOGIN_GAP_S
    assert all(low <= s <= high for s in no_gap)


def test_no_gap_after_a_single_login(no_gap, stub_login):
    acct = accounts.add_account("Solo", "solo@example.com", "pw")
    stub_login()
    asyncio.run(accounts.login_accounts([acct["id"]]))
    assert no_gap == []


def test_one_bad_password_does_not_abandon_the_rest(no_gap, stub_login):
    ids = [accounts.add_account(f"A{i}", f"a{i}@example.com", "pw")["id"]
           for i in range(3)]
    stub_login(results=[
        {"ok": True, "detail": "Logged in"},
        {"ok": False, "detail": "Wrong password"},
        {"ok": True, "detail": "Logged in"},
    ])

    result = asyncio.run(accounts.login_accounts(ids))

    assert len(result["results"]) == 3
    assert result["logged_in"] == 2
    assert result["total"] == 3


def test_an_unknown_id_in_the_sweep_is_reported_not_fatal(no_gap, stub_login):
    good = accounts.add_account("Good", "good@example.com", "pw")["id"]
    stub_login()

    result = asyncio.run(accounts.login_accounts([9999, good]))

    assert result["results"][0]["ok"] is False
    assert result["results"][1]["ok"] is True
    assert result["logged_in"] == 1


def test_an_exploding_login_is_caught_and_named(no_gap, monkeypatch):
    acct = accounts.add_account("Boom", "boom@example.com", "pw")

    async def _boom(**kwargs):
        raise RuntimeError("Chrome would not start")

    monkeypatch.setattr(accounts.meetup, "login", _boom)

    result = asyncio.run(accounts.login_accounts([acct["id"]]))

    assert result["logged_in"] == 0
    assert "RuntimeError: Chrome would not start" in result["results"][0]["detail"]


def test_sweep_response_carries_scrubbed_accounts(no_gap, stub_login, account):
    stub_login()
    result = asyncio.run(accounts.login_accounts([account["id"]]))
    assert all("password" not in a for a in result["accounts"])


# ── new_login ────────────────────────────────────────────────────────────

def test_new_login_drops_the_session_before_signing_in(
        account, make_profile, future, stub_login, monkeypatch):
    """"Log in fresh" must actually be fresh: with the old profile still on disk
    login_account would short-circuit on it and change nothing."""
    make_profile(f"acct-{account['id']}",
                 [(".meetup.com", COOKIE, to_chrome_time(future))])
    closed: list = []

    async def _close(*, profile):
        closed.append(profile)

    async def _sleep(_seconds):
        pass

    monkeypatch.setattr(accounts.browser, "close_browser", _close)
    monkeypatch.setattr(accounts.asyncio, "sleep", _sleep)
    calls = stub_login()

    result = asyncio.run(accounts.new_login(account["id"]))

    assert closed == [sessions.profile_dir(account["id"])]   # Chrome released the files
    assert len(calls) == 1                                   # a real login, not a skip
    assert result["skipped"] is False


def test_new_login_on_unknown_account_raises():
    with pytest.raises(KeyError):
        asyncio.run(accounts.new_login(9999))


# ── stats ────────────────────────────────────────────────────────────────

def test_stats_counts_logged_in_accounts(make_profile, future, past):
    live = accounts.add_account("Live", "live@example.com", "pw")
    accounts.add_account("Dead", "dead@example.com", "pw")
    make_profile(f"acct-{live['id']}", [(".meetup.com", COOKIE, to_chrome_time(future))])
    sessions.sweep()

    result = accounts.stats()

    assert result["total"] == 2
    assert result["logged_in"] == 1
    assert "max_messages_per_run" in result["settings"]
