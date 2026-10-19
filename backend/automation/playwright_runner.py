"""Owns the browser processes.

One persistent context PER PROFILE DIRECTORY, and several may be open at once —
one window per account, side by side, so opening a second account does not take
the first one away. Persistent (rather than a fresh context per run) because that
directory IS the session: cookies, local storage and the device fingerprint
Meetup ties a login to all live in it. A fresh context would mean a fresh login
and a fresh challenge every run.

The one rule that has not changed: never two Chrome processes on the SAME
profile — that corrupts it. Keying the registry on the profile directory is what
enforces it; a second ask for an open profile gets the window that is already up.

Worth knowing what running several costs: accounts open together are visible to
Meetup from one IP at the same time, which is the pattern that links a set of
accounts to each other. That is now the operator's call to make, not this
module's — it was previously prevented by closing the last window on every
switch, and the operator asked for the windows to stay.

    page = await open_page(profile=..., url=...)
    await close_browser(profile=...)   # one window
    await close_browser()              # all of them
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from backend import activity, config
from backend.automation import behavior

_playwright = None
# profile directory → its live context. THE registry: everything else in this
# module is a question asked of it.
_contexts: dict[Path, BrowserContext] = {}
# The window the profile-less calls mean — the last one opened or used. A
# screenshot or a probe says "the browser", and with several up that has to
# resolve to one of them rather than to a fresh default profile.
_active: Path | None = None
_lock = asyncio.Lock()
# The profiles WE are closing, so their own close events know the window did not
# vanish behind our back and stay quiet.
_closing: set[Path] = set()

# Launch flags live in behavior.py with the rest of the tuning. Not a claim of
# undetectability — a determined fingerprinter still wins. These stop the free
# giveaways: the infobar, navigator.webdriver, and the headless window size.
_CLEAN_ARGS = behavior.LAUNCH_ARGS
_AUTOMATION_ARGS = behavior.AUTOMATION_ARGS

# navigator.webdriver is set by the driver AFTER the flags are applied, so it has
# to be overwritten in the page itself. Added as an init script so it runs before
# any of Meetup's own code on every document, including iframes.
#
# The rest patches the properties a headless or driver-controlled Chrome gets
# wrong: an empty plugin list, a missing chrome runtime object, and a permissions
# API that answers "denied" for notifications while Notification.permission says
# "default" — a contradiction no real browser produces.
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
if (!window.chrome) { window.chrome = { runtime: {} }; }
if (navigator.plugins && navigator.plugins.length === 0) {
  Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
}
const _query = navigator.permissions && navigator.permissions.query;
if (_query) {
  navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : _query.call(navigator.permissions, p));
}
"""


def current_profile() -> Path | None:
    """The window a profile-less call means. See `_active`."""
    return _active if _active in _contexts else None


def open_profiles() -> list[Path]:
    """Every profile with a window up right now, in the order they were opened."""
    _prune()
    return list(_contexts)


def _alive(context: BrowserContext | None) -> bool:
    """Is this context still backed by a running browser?

    The operator closing the window with the X is a normal thing to do, and it
    leaves this module holding a handle to a dead Chrome. Every later call would
    then fail on that corpse — the reopen button being the one that hurts, since
    reopening is exactly what it is for. So liveness is asked, not assumed.
    """
    if context is None:
        return False
    browser = getattr(context, "browser", None)
    if browser is None:
        return True                    # nothing to ask; the close event covers it
    try:
        return browser.is_connected()
    except Exception:                  # noqa: BLE001 — an unaskable browser is a dead one
        return False


def _forget(profile: Path, reason: str) -> None:
    """Drop the handle to a window that is already gone.

    Only this profile's entry goes: the other accounts' windows are untouched,
    which is the whole point of keeping them in a registry rather than in one
    slot that every new browser overwrote.
    """
    global _active
    if _contexts.pop(profile, None) is None:
        return
    if _active == profile:
        # Whichever window is still up takes over as the one a profile-less call
        # means. None when that was the last of them.
        _active = next(iter(_contexts), None)
    activity.log("browser", f"{profile.name} was closed outside the app ({reason}) "
                            f"— it will relaunch on the next use", "info")


def _prune() -> None:
    """Forget every window that is no longer there. Safe to call unlocked: it
    only removes entries whose browser has already gone."""
    for profile, context in list(_contexts.items()):
        if not _alive(context):
            _forget(profile, "browser is gone")


def _on_context_closed(profile: Path) -> None:
    """Chrome went away: forget it immediately rather than at the next failure.

    Fires both when the operator closes the window and when we close it
    ourselves; `_closing` tells the two apart so an intentional close is not
    reported as a disappearance.
    """
    if profile in _closing:
        return
    _forget(profile, "window closed")


async def status() -> dict:
    # Asked, not remembered: a hand-closed window must read as closed here too,
    # or the Scraping gate stays unlocked over a browser that is not there.
    _prune()
    browsers = [{"profile": str(profile), "name": profile.name,
                 "pages": len(context.pages)}
                for profile, context in _contexts.items()]
    active = _active if _active in _contexts else None
    return {
        # `open` and `profile` describe the ACTIVE window, as they always have —
        # every existing caller reads them that way. `profiles`/`browsers` are
        # the whole set, for the callers that have to know about all of them.
        "open": bool(_contexts),
        "profile": str(active) if active else None,
        "profiles": [row["profile"] for row in browsers],
        "browsers": browsers,
        "count": len(browsers),
        "pages": len(_contexts[active].pages) if active else 0,
        "headless": config.PLAYWRIGHT_HEADLESS,
        "channel": config.PLAYWRIGHT_CHANNEL,
        "proxy": bool(config.PROXY_SERVER),
    }


def _proxy_label() -> str:
    """The proxy as it may appear in a log — host:port only.

    Credentials must never reach the activity feed or the terminal: the feed is
    rendered in a browser, screenshotted, and pasted into issues.
    """
    server = config.PROXY_SERVER
    if not server:
        return "none"
    host = server.split("://")[-1].split("@")[-1]
    return host


async def _launch(profile: Path) -> BrowserContext:
    global _playwright, _active

    if _playwright is None:
        _playwright = await async_playwright().start()

    profile.mkdir(parents=True, exist_ok=True)
    clean = config.PLAYWRIGHT_CLEAN_LAUNCH
    args = list(_CLEAN_ARGS) if clean else []

    context = await _playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        # Real Chrome, not the bundled Chromium: Chromium reports a different
        # user-agent, ships no Widevine and no proprietary codecs, and is a
        # rarity among real visitors. The channel is what makes it the browser
        # already installed on the machine.
        channel=config.PLAYWRIGHT_CHANNEL or None,
        headless=config.PLAYWRIGHT_HEADLESS,
        slow_mo=config.PLAYWRIGHT_SLOWMO or 0,
        args=args,
        # Stop Playwright adding its own automation flags. --enable-automation
        # paints the "controlled by automated test software" infobar and sets
        # several JS tells; --no-sandbox is a combination almost no real desktop
        # Chrome runs with, so its presence is itself a signal.
        # PLAYWRIGHT_NO_SANDBOX keeps --no-sandbox for a root container.
        ignore_default_args=([a for a in _AUTOMATION_ARGS
                              if not (config.PLAYWRIGHT_NO_SANDBOX and a == "--no-sandbox")]
                             if clean else None),
        proxy=config.playwright_proxy(),
        # no_viewport + --start-maximized gives the real window's dimensions.
        # A fixed viewport reports the same size on every machine, and the
        # default 1280x720 is close to a headless fingerprint.
        no_viewport=True,
        locale="en-US",
        timezone_id=None,              # inherit the host's — a mismatch with the
                                       # proxy's IP is itself a detection signal
    )
    if clean:
        await context.add_init_script(_STEALTH_JS)

    context.set_default_timeout(30_000)
    _contexts[profile] = context
    _active = profile
    # Bound to THIS profile, so the window that closes is the entry that goes.
    context.on("close", lambda _c, p=profile: _on_context_closed(p))
    others = len(_contexts) - 1
    activity.log("browser",
                 f"Launched {config.PLAYWRIGHT_CHANNEL or 'chromium'} on "
                 f"{profile.name} (proxy {_proxy_label()})"
                 + (f" — {others} other window(s) stay open" if others else ""),
                 "ok")
    return context


def _restore_if_missing(profile: Path) -> None:
    """If this profile isn't on disk but MinIO holds a snapshot, unpack it first.

    The point of the whole persistent-profile design is to avoid re-authenticating:
    a fresh login from an unfamiliar device is what triggers email codes and
    captchas. A rebuilt container or a cleared volume would otherwise force one
    on every account at once — which is far more visible than any single login.

    Best-effort. A missing snapshot or a dead MinIO just means a normal launch.
    """
    if profile.exists() or not profile.name.startswith("acct-"):
        return
    account_id = profile.name.split("-", 1)[1]
    try:
        from services.storage import session_storage

        result = session_storage.restore(account_id, profile)
        if result.get("restored"):
            activity.log("sessions",
                         f"Restored {profile.name} from MinIO "
                         f"({result['files']} files)", "ok")
    except Exception as exc:  # noqa: BLE001 — never block a launch on storage
        activity.log("sessions", f"No snapshot for {profile.name}: "
                                 f"{type(exc).__name__}", "info")


async def get_context(profile: Path | str | None = None) -> BrowserContext:
    """The live context for `profile`, launching one if it has no window yet.

    Every OTHER profile's window is left alone — a second account opens beside
    the first rather than in place of it.

    With no profile: the active window, or the default profile when nothing is
    open. "The browser" is what a screenshot or a probe asks for, and with
    several up that has to mean one of them rather than a fresh default profile
    nobody asked to launch.
    """
    global _active
    async with _lock:
        target = Path(profile) if profile else (_active or
                                                config.PLAYWRIGHT_USER_DATA_DIR)
        existing = _contexts.get(target)
        # A window the operator closed by hand is not an open one. Dropped before
        # the lookup answers, or pressing open on that same account would hand
        # back the dead context instead of launching a new window.
        if existing is not None and not _alive(existing):
            _forget(target, "browser is gone")
            existing = None
        if existing is not None:
            _active = target
            return existing
        _restore_if_missing(target)
        return await _launch(target)


async def current_page(profile: Path | str | None = None) -> Page:
    """The context's page with NO navigation at all.

    For callers that must look at where the browser already is before deciding
    to move it. Navigating destroys page state that can be expensive to recreate
    — a half-filled form, or a captcha the operator has just solved by hand.
    """
    context = await get_context(profile)
    try:
        return context.pages[0] if context.pages else await context.new_page()
    except Exception:                  # noqa: BLE001 — the window died mid-call
        # Between the liveness check and this line the browser can still go: the
        # operator's click on the X and this request are racing. Forget it and
        # launch once more — a second failure is a real one and is raised.
        async with _lock:
            for target, live in list(_contexts.items()):
                if live is context:
                    _forget(target, "page could not be opened")
        context = await get_context(profile)
        return context.pages[0] if context.pages else await context.new_page()


async def open_page(profile: Path | str | None = None,
                    url: str | None = None) -> Page:
    """A page on `profile`, navigated to `url` if given.

    Reuses the context's existing page rather than opening a second tab: a run
    that opens a tab per step leaves a browser with forty of them, and Meetup
    treats a burst of parallel tabs on one session as scripted.
    """
    page = await current_page(profile)
    target = url if url is not None else (config.PLAYWRIGHT_START_URL or None)
    if target:
        try:
            await page.goto(target, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001 — a slow page isn't a dead browser
            activity.log("browser", f"Navigation to {target} was slow: "
                                    f"{type(exc).__name__}", "warn")
    return page


async def _close_one(profile: Path) -> dict:
    """Close ONE profile's window. Caller holds the lock."""
    global _active
    context = _contexts.get(profile)
    if context is None:
        return {"closed": False, "detail": "That profile is not open"}
    _closing.add(profile)
    try:
        await context.close()
    except Exception as exc:  # noqa: BLE001 — an already-dead browser is still closed
        activity.log("browser", f"Close was not clean: {type(exc).__name__}", "warn")
    finally:
        _closing.discard(profile)
    _contexts.pop(profile, None)
    if _active == profile:
        _active = next(iter(_contexts), None)
    activity.log("browser", f"Closed {profile.name}", "ok")
    return {"closed": True, "profile": profile.name}


async def close_browser(profile: Path | str | None = None) -> dict:
    """Close one account's window, or every open one when no profile is given.

    Naming the profile closes THAT window and nothing else — with several
    accounts up, "close this one" must not shut the others down mid-run. No
    profile means all of them, which is what the panel's Close browser button
    and a session-wide clear both mean.
    """
    async with _lock:
        if profile is not None:
            return await _close_one(Path(profile))
        closed = []
        for target in list(_contexts):
            result = await _close_one(target)
            if result.get("closed"):
                closed.append(result["profile"])
        return {"closed": bool(closed), "profiles": closed, "count": len(closed),
                "detail": f"Closed {len(closed)} browser(s)" if closed
                          else "Nothing was open"}


async def shutdown() -> None:
    """Close every browser AND stop Playwright — called on server shutdown so no
    Chrome process is left behind holding a profile open."""
    global _playwright
    async with _lock:
        for target in list(_contexts):
            await _close_one(target)
        if _playwright is not None:
            try:
                await _playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            _playwright = None


async def screenshot(name: str = "debug") -> str | None:
    """Save a screenshot of the active window's page under data/screenshots/.

    Returns the path the /shots mount serves it from, or None when nothing is
    open. Best-effort: a failed screenshot must never break the run it was
    supposed to be documenting.
    """
    context = _contexts.get(_active) if _active else None
    if context is None or not context.pages:
        return None
    shots = config.BACKEND_DIR / "data" / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    path = shots / f"{name}.png"
    try:
        await context.pages[0].screenshot(path=str(path), full_page=False)
    except Exception as exc:  # noqa: BLE001
        activity.log("browser", f"Screenshot failed: {type(exc).__name__}", "warn")
        return None
    return f"/shots/{path.name}"
