"""Meetup accounts — the credentials, and logging them in.

Storage is db.py's; this module is the behaviour on top: what the API is allowed
to see (never the password, except through one explicit call), and driving a
login through a real browser profile.

Logins are deliberately sequential with a pause between them. A burst of
simultaneous logins from one IP is the clearest possible signal that a set of
accounts belongs together, and Meetup acts on exactly that.
"""
from __future__ import annotations

import asyncio
import random

from backend import activity, config, db, sessions, settings
from backend.automation import meetup
from backend.automation import playwright_runner as browser

# Between two account logins. Wide and randomised on purpose — see the module
# docstring. Not an operator setting: a value low enough to matter is a value
# low enough to get the whole set flagged.
_LOGIN_GAP_S = (8.0, 20.0)


def _public(account: dict | None) -> dict | None:
    """One account with its password removed. Every list/detail response goes
    through here, so a new field can't accidentally leak the credential."""
    if not account:
        return None
    out = {k: v for k, v in account.items() if k != "password"}
    out["has_password"] = bool(account.get("password"))
    return out


def list_accounts() -> list[dict]:
    return [_public(a) for a in db.list_accounts()]


def add_account(label: str, login: str, password: str) -> dict:
    login = (login or "").strip()
    if not login:
        raise ValueError("An email or username is required.")
    if not (password or ""):
        raise ValueError("A password is required.")
    existing = {a["login"].lower() for a in db.list_accounts() if a.get("login")}
    if login.lower() in existing:
        raise ValueError(f"{login} is already stored.")
    account = db.add_account({"label": label, "login": login, "password": password})
    activity.log("accounts", f"Added {label or login}", "ok")
    return _public(account)


def reveal(account_id: int) -> str:
    """The stored password. Behind its own endpoint so credentials aren't sitting
    in the accounts list response until actually asked for."""
    account = db.get_account(account_id)
    if not account:
        raise KeyError(f"No account {account_id}")
    return account.get("password") or ""


def remove_account(account_id: int) -> bool:
    account = db.get_account(account_id)
    if not account:
        return False
    # Delete the session FIRST. Leaving a logged-in profile behind for a deleted
    # account means a live Meetup session with nothing in the UI pointing at it —
    # and sessions.clear() looks the account up, so it has to still be there.
    try:
        sessions.clear(account_id)
    except KeyError:
        pass
    removed = db.remove_account(account_id)
    if removed:
        activity.log("accounts", f"Removed {account.get('label') or account_id}", "ok")
    return removed


async def _raise_window(page) -> None:
    """Put an already-open page in front. Best-effort — a window that refuses to
    come forward is not a reason to fail the open it was already part of."""
    try:
        await page.bring_to_front()
    except Exception:  # noqa: BLE001
        pass


async def open_account(account_id: int) -> dict:
    """Open a browser on this account's profile, without logging in.

    What "Manual login" on the accounts row does: it puts the operator in front
    of the session so they can sign in by hand, solve a challenge, or check what
    Meetup is showing this account. Nothing is typed and nothing is submitted —
    whatever session the operator leaves behind in the profile is the one every
    later run reuses.
    """
    account = db.get_account(account_id)
    if not account:
        raise KeyError(f"No account {account_id}")
    # Only navigate when the browser isn't already on Meetup. Pressing this a
    # second time while half-way through a hand-solved captcha would reload the
    # page and destroy the token — the same way the login flow used to.
    profile = sessions.profile_dir(account_id)
    page = await browser.current_page(profile)
    if config.BASE_URL not in (page.url or ""):
        page = await browser.open_page(profile=profile, url=config.BASE_URL)
    # Already up: raise it. The operator pressed open, so a window is what they
    # get — either a new one or the one hiding behind this app.
    await _raise_window(page)
    state = sessions.refresh(account_id)
    activity.log("accounts", f"Opened {account.get('label') or account_id}", "ok")
    return {"account": _public(db.get_account(account_id)), "session": state,
            "url": page.url if page else None}


async def save_session(account_id: int) -> dict:
    """Persist whatever is signed in in the open browser as this account's session.

    Closing the browser IS the save. Chrome keeps its cookie store in memory and
    holds the file with an exclusive lock while it runs — on Windows not even a
    read gets through — so a session is neither durable nor readable until the
    process exits cleanly and flushes. A kill would lose the write; `close()`
    does not.

    This is what makes a Google/Apple/Facebook sign-in usable here. Those never
    touch Meetup's password form, so the session in the profile is the ONLY
    record of them: save it once and every later run reuses it.
    """
    account = db.get_account(account_id)
    if not account:
        raise KeyError(f"No account {account_id}")

    label = account.get("label") or account.get("login") or f"#{account_id}"
    await browser.close_browser(profile=sessions.profile_dir(account_id))
    await asyncio.sleep(1.5)          # let Chrome release the profile's files

    state = sessions.refresh(account_id)
    if state["logged_in"]:
        # Pin it. A session earned through SSO or a hand-solved captcha cannot be
        # recreated by the login form, so a blanket "clear all sessions" must not
        # be able to take it.
        sessions.lock(account_id)
        db.update_account(account_id, {"last_result": "Session saved"})
        activity.log("accounts", f"{label}: session saved and locked", "ok")
        return {"ok": True, "saved": True, "locked": True, "session": state,
                "account": _public(db.get_account(account_id)),
                "detail": "Session saved — it will be reused from now on"}

    db.update_account(account_id, {"last_result": "No session to save"})
    activity.log("accounts", f"{label}: nothing to save — {state['detail']}", "warn")
    return {"ok": False, "saved": False, "session": state,
            "account": _public(db.get_account(account_id)),
            "detail": f"No Meetup session in that profile ({state['detail']}). "
                      f"Sign in in the browser first, then save."}


async def open_session(account_id: int) -> dict:
    """Open the browser on this account's STORED session.

    Refuses when there isn't one rather than opening a logged-out window that
    looks identical to a logged-in one until you click something.
    """
    account = db.get_account(account_id)
    if not account:
        raise KeyError(f"No account {account_id}")

    state = sessions._read_session(sessions.profile_dir(account_id))  # noqa: SLF001
    if not state["logged_in"]:
        if state.get("locked_by_browser"):
            # Already open on this profile — that IS the stored session, live.
            page = await browser.current_page(sessions.profile_dir(account_id))
            await _raise_window(page)
            return {"ok": True, "already_open": True, "session": state,
                    "url": page.url if page else None,
                    "detail": "That session is already open in the browser"}
        return {"ok": False, "session": state,
                "detail": f"No stored session for this account ({state['detail']}). "
                          f"Use Manual login, sign in, then Save session."}

    profile = sessions.profile_dir(account_id)
    page = await browser.current_page(profile)
    if config.BASE_URL not in (page.url or ""):
        page = await browser.open_page(profile=profile, url=config.BASE_URL)
    await _raise_window(page)
    state = sessions.refresh(account_id)
    label = account.get("label") or account.get("login") or f"#{account_id}"
    activity.log("accounts", f"Opened stored session for {label}", "ok")
    return {"ok": True, "already_open": False, "session": state,
            "account": _public(db.get_account(account_id)),
            "url": config.BASE_URL, "detail": "Stored session opened"}


async def open_messages(account_id: int) -> dict:
    """Open the stored session and click the header's Messages icon.

    The click matters: following the link the header already offers is what a
    person does, where a bare goto to /messages/ is a URL nobody typed. It also
    proves the session is real — the icon only exists on a signed-in header, so
    reaching the inbox this way IS the check that the stored session still works.
    """
    opened = await open_session(account_id)
    if not opened["ok"]:
        return opened

    page = await browser.current_page(sessions.profile_dir(account_id))
    result = await meetup.open_inbox(page)
    label = (db.get_account(account_id) or {}).get("label") or account_id
    activity.log("accounts", f"{label}: {result['detail']}",
                 "ok" if result["ok"] else "error")
    return {**result, "session": opened["session"],
            "url": page.url if page else None}


async def send_first_chat(account_id: int, text: str,
                          image: str | None = None) -> dict:
    """Open the STORED session, click through to the inbox, message the first chat.

    Deliberately the stored-session path, not a login: the account this exists
    for signed in through Google, which never touches Meetup's password form, so
    login_account() could never authenticate it — but its cookies are in the
    profile, and open_session() opens straight onto them.

    Returns {ok, detail, conversations, name, ...}: the roster of who is in the
    inbox, which one was messaged, and whether it sent — everything the panel
    shows as a temporary result. A failed send is a reported status, not an
    exception.
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "detail": "Write the message first", "conversations": []}

    opened = await open_session(account_id)   # raises KeyError for an unknown id
    if not opened["ok"]:
        return {**opened, "conversations": []}

    page = await browser.current_page(sessions.profile_dir(account_id))
    try:
        result = await meetup.message_first_chat(page, text, image=image)
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"
        activity.log("accounts", f"First-chat send failed — {detail}", "error")
        return {"ok": False, "detail": detail, "conversations": []}

    label = (db.get_account(account_id) or {}).get("label") or account_id
    activity.log("accounts", f"{label}: {result['detail']}",
                 "ok" if result["ok"] else "error")
    return {**result, "session": opened["session"]}


def update_account(account_id: int, changes: dict) -> dict:
    """Edit an account's label, login or password. The profile is NOT touched.

    Without this the only way to fix a wrong address was to delete the account
    and add it again — which deletes its profile directory, and the profile IS
    the session. Correcting a typo would destroy a login that cost a captcha.
    """
    account = db.get_account(account_id)
    if not account:
        raise KeyError(f"No account {account_id}")

    allowed = {}
    if "label" in changes:
        allowed["label"] = (changes["label"] or "").strip()
    if "login" in changes:
        login = (changes["login"] or "").strip()
        if not login:
            raise ValueError("An email or username is required.")
        clash = {a["login"].lower() for a in db.list_accounts()
                 if a.get("login") and a["id"] != int(account_id)}
        if login.lower() in clash:
            raise ValueError(f"{login} is already stored.")
        allowed["login"] = login
    if "password" in changes and changes["password"]:
        allowed["password"] = changes["password"]

    if not allowed:
        return _public(account)
    updated = db.update_account(account_id, allowed)
    activity.log("accounts", f"Updated {updated.get('label') or account_id}", "ok")
    return _public(updated)


async def login_account(account_id: int) -> dict:
    """Log ONE account in, reusing its stored session when it's still valid."""
    account = db.get_account(account_id)
    if not account:
        raise KeyError(f"No account {account_id}")

    label = account.get("label") or account.get("login") or f"#{account_id}"
    state = sessions._read_session(sessions.profile_dir(account_id))  # noqa: SLF001
    if state["logged_in"]:
        # Already logged in on disk. Logging in again would submit a form Meetup
        # doesn't need and can challenge — the cheapest login is the one skipped.
        db.update_account(account_id, {"logged_in": True,
                                       "last_result": "Already logged in"})
        activity.log("accounts", f"{label}: already logged in", "ok")
        # `id` at the top level, not only inside `account`: a caller logging
        # several in gets a list back and has to be able to say WHICH one each
        # line is about without digging into a nested record.
        return {"id": account_id, "label": label,
                "account": _public(db.get_account(account_id)), "session": state,
                "skipped": True}

    result = await meetup.login(
        profile=sessions.profile_dir(account_id),
        login=account.get("login") or "",
        password=account.get("password") or "",
    )
    state = sessions.refresh(account_id)
    db.update_account(account_id, {
        "last_login": state["checked_at"],
        "last_result": result.get("detail") or ("Logged in" if result.get("ok") else "Failed"),
    })
    activity.log("accounts", f"{label}: {result.get('detail')}",
                 "ok" if result.get("ok") else "error")
    return {"id": account_id, "label": label,
            "account": _public(db.get_account(account_id)), "session": state,
            "skipped": False, **result}


async def login_accounts(ids: list[int]) -> dict:
    """Log several accounts in, ONE AT A TIME with a randomised gap.

    Sequential is the point — see the module docstring. A failure is recorded and
    the rest continue: one bad password shouldn't abandon the other nine.
    """
    results = []
    pending = [int(i) for i in ids]
    for index, account_id in enumerate(pending):
        try:
            results.append(await login_account(account_id))
        except KeyError as exc:
            results.append({"id": account_id, "ok": False, "detail": str(exc)})
        except Exception as exc:  # noqa: BLE001
            results.append({"id": account_id, "ok": False,
                            "detail": f"{type(exc).__name__}: {exc}"})
        if index < len(pending) - 1:
            await asyncio.sleep(random.uniform(*_LOGIN_GAP_S))

    ok = sum(1 for r in results if r.get("ok") or r.get("skipped"))
    activity.log("accounts", f"Login sweep: {ok}/{len(pending)} logged in",
                 "ok" if ok == len(pending) else "warn")
    return {"results": results, "logged_in": ok, "total": len(pending),
            "accounts": list_accounts()}


async def new_login(account_id: int) -> dict:
    """Drop the stored session, then log in fresh.

    For when a session exists but is wrong — logged in as the wrong account, or
    stuck in a state Meetup won't clear on its own.
    """
    account = db.get_account(account_id)
    if not account:
        raise KeyError(f"No account {account_id}")
    await browser.close_browser(profile=sessions.profile_dir(account_id))
    await asyncio.sleep(1.2)          # let Chrome release the profile's files
    sessions.clear(account_id)
    return await login_account(account_id)


def stats() -> dict:
    accounts = db.list_accounts()
    return {
        "total": len(accounts),
        "logged_in": sum(1 for a in accounts if a.get("logged_in")),
        "settings": {"max_messages_per_run": settings.current()["max_messages_per_run"]},
    }
