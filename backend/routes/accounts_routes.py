import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import accounts, db, sessions

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


class AccountBody(BaseModel):
    label: str = ""
    login: str = ""
    password: str = ""


class UpdateBody(BaseModel):
    """Every field optional — exclude_unset means an omitted one is left alone,
    so sending {"label": "x"} cannot blank the password."""
    label: str | None = None
    login: str | None = None
    password: str | None = None


class FirstChatTextBody(BaseModel):
    text: str = ""
    image: str | None = None


class LoginBody(BaseModel):
    ids: list[int] = []


@router.get("")
async def index():
    return {"accounts": accounts.list_accounts(), "stats": accounts.stats()}


@router.post("")
async def create(body: AccountBody):
    try:
        return {"account": accounts.add_account(body.label, body.login, body.password)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/{account_id}")
async def update(account_id: int, body: UpdateBody):
    """Edit label / login / password in place. The profile — and so the stored
    session — is deliberately untouched."""
    try:
        return {"account": accounts.update_account(
            account_id, body.model_dump(exclude_unset=True))}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{account_id}/secret")
async def secret(account_id: int):
    """Backs the eye icon. Kept off the list response so passwords aren't sitting
    in the page until actually asked for."""
    try:
        return {"password": accounts.reveal(account_id)}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/{account_id}")
async def delete(account_id: int):
    if not accounts.remove_account(account_id):
        raise HTTPException(404, f"No account {account_id}")
    return {"deleted": account_id}


@router.post("/{account_id}/open")
async def open_one(account_id: int):
    try:
        return await accounts.open_account(account_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/login")
async def login(body: LoginBody):
    if not body.ids:
        raise HTTPException(400, "Select at least one account first.")
    return await accounts.login_accounts(body.ids)


# ─── Sessions ────────────────────────────────────────────────────────────


@router.get("/sessions")
async def sessions_index():
    """Every stored session, read from disk — the source of truth. Includes the
    default profile, which is not an account but does hold a login.

    `active` means a browser is open on that profile right now. sessions.py can
    infer it from the cookie DB being locked, which only happens on Windows, so
    the runner's own answer is joined in here: it is the authoritative one, and
    it works everywhere.
    """
    from backend.automation import playwright_runner as browser

    state = await browser.status()
    # Every window that is up, not just one: accounts open side by side, so
    # several rows can be Active at the same time. `profiles` is the full set;
    # `profile` alone is the older single-window shape.
    open_profiles = {p for p in (state.get("profiles")
                                 or ([state.get("profile")] if state.get("profile")
                                     else [])) if p}
    rows = []
    for account in db.list_accounts():
        row = sessions.describe(account)
        if row["profile"] in open_profiles:
            row["active"] = True
            row["detail"] = "Active — open in the browser now"
        rows.append(row)
    return {"sessions": rows, "default_profile": sessions.default_profile()}


@router.post("/sessions/check")
async def sessions_check_all():
    """Check Login Status — every stored session, straight off disk.

    Reads each profile's cookie store; no browser is launched and no login is
    attempted. A valid, unexpired session cookie means Logged In; anything else
    (no profile, no cookie, expired) is Logged Out.
    """
    result = sessions.sweep()
    return {**result, "accounts": accounts.list_accounts()}


@router.post("/sessions/clear")
async def sessions_clear_all():
    """Delete every stored session — account profiles AND the default profile.
    Accounts and credentials are kept."""
    from backend.automation import playwright_runner as browser

    # Chrome holds its profile's files open, so deletion silently fails while
    # it's alive. Every window goes — this clears every profile — and the
    # processes get a moment to actually exit.
    await browser.close_browser()
    await asyncio.sleep(1.2)

    result = sessions.clear_all()
    if not result["ok"]:
        # say so loudly rather than report a clear that didn't happen
        raise HTTPException(409, result["detail"])
    return result


@router.get("/{account_id}/session")
async def session_of(account_id: int):
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, f"No account {account_id}")
    return sessions.describe(account)


@router.post("/{account_id}/session/check")
async def session_check(account_id: int):
    """Re-read one account's stored session from disk. No browser."""
    try:
        sessions.refresh(account_id)
        return accounts._public(db.get_account(account_id))  # noqa: SLF001
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{account_id}/session/close")
async def session_close(account_id: int):
    """Close just this account's window, leaving its session on disk."""
    from backend.automation import playwright_runner as browser

    return await browser.close_browser(profile=sessions.profile_dir(account_id))


@router.post("/{account_id}/session/save")
async def session_save(account_id: int):
    """Persist the open browser's sign-in as this account's session.

    Not a 4xx when there is nothing to save: "you aren't signed in yet" is a
    normal answer the panel shows, not a client error.
    """
    try:
        return await accounts.save_session(account_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{account_id}/session/open")
async def session_open(account_id: int):
    """Open the browser on this account's stored session."""
    try:
        result = await accounts.open_session(account_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    if not result.get("ok"):
        raise HTTPException(409, result["detail"])
    return result


@router.post("/{account_id}/message-first")
async def message_first(account_id: int, body: FirstChatTextBody):
    """Open the stored session and message the first conversation.

    Returns 200 even when the send fails: the roster and the send status are the
    result the panel renders, so "opened the inbox, found 5 chats, send didn't
    go" is data to show, not an error to swallow. Only an unknown account 404s.
    """
    try:
        return await accounts.send_first_chat(account_id, body.text, image=body.image)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{account_id}/messages")
async def messages(account_id: int):
    """Open the stored session and click through to the inbox."""
    try:
        result = await accounts.open_messages(account_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    if not result.get("ok"):
        raise HTTPException(409, result["detail"])
    return result


@router.post("/{account_id}/session/lock")
async def session_lock(account_id: int):
    return sessions.lock(account_id)


@router.post("/{account_id}/session/unlock")
async def session_unlock(account_id: int):
    return sessions.unlock(account_id)


@router.post("/{account_id}/logout")
async def logout(account_id: int):
    """Log out: close the window and delete the stored session."""
    from backend.automation import playwright_runner as browser

    await browser.close_browser(profile=sessions.profile_dir(account_id))
    await asyncio.sleep(1.0)          # let Chrome release the profile's files
    try:
        return sessions.clear(account_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/{account_id}/session")
async def session_clear(account_id: int):
    from backend.automation import playwright_runner as browser

    try:
        # This account's window only — the other accounts' stay up.
        await browser.close_browser(profile=sessions.profile_dir(account_id))
        return sessions.clear(account_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{account_id}/new-login")
async def new_login(account_id: int):
    """Drop the stored session, then log in fresh."""
    try:
        return await accounts.new_login(account_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
