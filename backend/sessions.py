"""Stored browser sessions — read from disk, never from a browser.

One Chrome profile per account under backend/data/profiles/<id>/, plus the
default profile from config.PLAYWRIGHT_USER_DATA_DIR. Whether an account is
logged in is decided by ONE thing: does its profile's cookie store hold an
unexpired Meetup session cookie.

That is deliberately a disk read and nothing more. Launching a browser to ask
Meetup "am I logged in?" costs seconds per account, shows up in their telemetry
as a burst of identical sessions, and is exactly the traffic that gets a set of
accounts flagged together. Reading the cookie file is ~5ms and invisible.

    describe(account)     one account's session, as the UI shows it
    sweep()               re-read every account's session from disk
    profile_dir(id)       where an account's profile lives
    clear(id)             delete one stored session
    lock/unlock(id)       protect a session from a sweep clearing it
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from backend import activity, config, db

PROFILES_DIR = config.BACKEND_DIR / "data" / "profiles"

# Chrome writes cookies to an SQLite file in one of two places depending on
# version. Both are checked, newest wins.
_COOKIE_PATHS = ("Default/Network/Cookies", "Default/Cookies",
                 "Network/Cookies", "Cookies")

# Chrome stores cookie expiry as microseconds since 1601-01-01 (the Windows FILETIME
# epoch), not the Unix epoch. This is the offset between them, in microseconds.
_CHROME_EPOCH_OFFSET = 11_644_473_600_000_000


def profile_dir(account_id) -> Path:
    """Where one account's Chrome profile lives.

    Prefixed rather than a bare id so the directory is self-describing on disk —
    `profiles/7` next to a stray `profiles/tmp` says nothing about what it is.
    """
    return PROFILES_DIR / f"acct-{account_id}"


def default_profile() -> dict:
    """The default profile — not an account, but it does hold a login (the
    MU_USERNAME one), so the Sessions panel lists it alongside the accounts."""
    path = config.PLAYWRIGHT_USER_DATA_DIR
    state = _read_session(path)
    return {
        "id": None,
        "label": "Default profile",
        "login": config.MU_USERNAME or "—",
        "profile": str(path),
        **state,
    }


def _cookie_file(profile: Path) -> Path | None:
    """The newest cookie DB inside `profile`, or None if there isn't one."""
    found = [profile / rel for rel in _COOKIE_PATHS]
    found = [p for p in found if p.is_file()]
    if not found:
        return None
    return max(found, key=lambda p: p.stat().st_mtime)


def _read_session(profile: Path) -> dict:
    """Look for an unexpired Meetup session cookie in this profile.

    Returns {logged_in, expires_at, checked_at, detail}. Never raises: a missing,
    locked or corrupt profile is simply "logged out" with the reason in `detail`.
    """
    now = datetime.now(timezone.utc)
    out = {"logged_in": False, "expires_at": None,
           "checked_at": now.isoformat(timespec="seconds"), "detail": ""}

    path = Path(profile)
    if not path.is_dir():
        out["detail"] = "No profile yet"
        return out

    cookies = _cookie_file(path)
    if cookies is None:
        out["detail"] = "No cookie store"
        return out

    # Chrome holds the cookie DB open with a lock while it runs, and SQLite will
    # refuse or block on it. Copy it first — we only ever read, so a snapshot a
    # few milliseconds old is exactly as good as the live file.
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "cookies.db"
        try:
            shutil.copy2(cookies, snapshot)
        except PermissionError:
            # Windows: Chrome holds the cookie DB with an exclusive lock while it
            # runs — not even a read gets through. So this is not a broken
            # profile, it is a live one, and the fix is an action rather than a
            # diagnosis: close the browser and the session becomes readable.
            out["detail"] = "Browser is open — close it to read the session"
            out["locked_by_browser"] = True
            return out
        except OSError as exc:
            out["detail"] = f"Cookie store unreadable ({type(exc).__name__})"
            return out
        try:
            conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT expires_utc FROM cookies "
                    "WHERE name = ? AND host_key LIKE '%meetup.com' "
                    "ORDER BY expires_utc DESC LIMIT 1",
                    (config.SESSION_COOKIE,),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            out["detail"] = f"Cookie store unreadable ({exc.__class__.__name__})"
            return out

    if not row:
        out["detail"] = "No session cookie"
        return out

    raw = row[0] or 0
    if raw <= 0:
        # A session cookie with no expiry lives until the browser closes. It is
        # on disk, so treat it as valid — Chrome only persists it when it is.
        out.update(logged_in=True, detail="Session cookie (no expiry)")
        return out

    expires = datetime.fromtimestamp(
        (raw - _CHROME_EPOCH_OFFSET) / 1_000_000, tz=timezone.utc)
    out["expires_at"] = expires.isoformat(timespec="seconds")
    if expires <= now:
        out["detail"] = "Session expired"
        return out
    out.update(logged_in=True, detail="Logged in")
    return out


def _state_for(account: dict) -> dict:
    """One account's session state, with the live-browser case handled.

    While Chrome runs it holds the cookie DB under an exclusive lock, so the
    read comes back "unreadable". That is NOT "logged out" — it is the opposite,
    a session open in front of the operator. Reporting it as logged out would
    turn a card grey the instant its session was opened, and writing it back
    would erase the one record of a login that may have cost a captcha.

    So on a locked profile the last state read from disk is kept, and the
    session is marked `active`.
    """
    state = _read_session(profile_dir(account["id"]))
    if state.get("locked_by_browser"):
        return {**state,
                "logged_in": bool(account.get("logged_in")),
                "expires_at": account.get("session_expires"),
                "active": True,
                "detail": "Active — open in the browser now"}
    return {**state, "active": False}


def describe(account: dict) -> dict:
    """One account's session as the Accounts panel renders it."""
    path = profile_dir(account["id"])
    return {
        "id": account["id"],
        "label": account.get("label") or account.get("login") or f"#{account['id']}",
        "login": account.get("login") or "",
        "profile": str(path),
        "locked": bool(account.get("session_locked")),
        **_state_for(account),
    }


def refresh(account_id: int) -> dict:
    """Re-read ONE account's session and write the result back to the store."""
    account = db.get_account(account_id)
    if not account:
        raise KeyError(f"No account {account_id}")
    state = _state_for(account)
    _remember(account_id, state)
    return state


def _remember(account_id: int, state: dict) -> None:
    """Write a read-back state to the store.

    A locked profile writes only `last_checked`: its logged_in/expiry came from
    what the store already held, so writing them back is a no-op at best and, if
    the read had been trusted, an erasure at worst.
    """
    changes = {"last_checked": state["checked_at"]}
    if not state.get("active"):
        changes["logged_in"] = state["logged_in"]
        changes["session_expires"] = state["expires_at"]
    db.update_account(account_id, changes)


def sweep() -> dict:
    """Re-read every account's session from disk.

    Called on boot because sessions expire while the server is closed: whatever
    the store last recorded is a guess, and the cookie files are the truth —
    except for a profile a live browser holds locked, which keeps what it had.
    """
    checked = live = active = 0
    for account in db.list_accounts():
        state = _state_for(account)
        _remember(account["id"], state)
        checked += 1
        live += 1 if state["logged_in"] else 0
        active += 1 if state.get("active") else 0
    note = f"Swept {checked} session(s) — {live} logged in"
    if active:
        note += f", {active} open in the browser"
    activity.log("sessions", note, "ok")
    return {"checked": checked, "logged_in": live, "active": active}


def lock(account_id: int) -> dict:
    """Protect a session from clear_all().

    A hard-won login (one that passed an email code or a captcha) is worth more
    than the convenience of a blanket clear, so it can be pinned.
    """
    db.update_account(account_id, {"session_locked": True})
    return {"id": account_id, "locked": True}


def unlock(account_id: int) -> dict:
    db.update_account(account_id, {"session_locked": False})
    return {"id": account_id, "locked": False}


def clear(account_id: int) -> dict:
    """Delete one account's stored session. The account and its credentials stay."""
    account = db.get_account(account_id)
    if not account:
        raise KeyError(f"No account {account_id}")
    path = profile_dir(account_id)
    ok, detail = _rmtree(path)
    db.update_account(account_id, {"logged_in": False, "session_expires": None})
    activity.log("sessions", f"Cleared session for {account.get('label') or account_id}",
                 "ok" if ok else "warn")
    return {"id": account_id, "cleared": ok, "detail": detail}


def clear_all() -> dict:
    """Delete every stored session — account profiles AND the default profile.

    Locked sessions are skipped. Accounts and credentials are always kept: this
    clears logins, not who you are.
    """
    cleared, skipped, failed = [], [], []
    for account in db.list_accounts():
        if account.get("session_locked"):
            skipped.append(account["id"])
            continue
        ok, detail = _rmtree(profile_dir(account["id"]))
        (cleared if ok else failed).append(account["id"] if ok else f"{account['id']}: {detail}")
        db.update_account(account["id"], {"logged_in": False, "session_expires": None})

    ok, detail = _rmtree(config.PLAYWRIGHT_USER_DATA_DIR)
    if not ok:
        failed.append(f"default: {detail}")

    result = {
        "ok": not failed,
        "cleared": cleared,
        "skipped": skipped,
        "failed": failed,
        "detail": "; ".join(failed) if failed else f"Cleared {len(cleared)} session(s)",
    }
    activity.log("sessions", result["detail"], "ok" if result["ok"] else "error")
    return result


def _rmtree(path: Path) -> tuple[bool, str]:
    """Delete a profile directory. Returns (ok, detail).

    Chrome holds a live profile's files open, so deletion silently half-fails
    while it is alive — the caller closes the browser first and waits. A profile
    that was never created is a success, not a failure: the desired state (no
    stored session) already holds.
    """
    path = Path(path)
    if not path.exists():
        return True, "Nothing to clear"
    try:
        shutil.rmtree(path)
        return True, "Cleared"
    except (OSError, PermissionError) as exc:
        return False, f"Chrome may still be running ({type(exc).__name__})"
