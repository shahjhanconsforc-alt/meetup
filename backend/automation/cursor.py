"""Pointer and keyboard, at two levels.

**In-page (Playwright).** `element.click()` teleports the cursor: no mousemove
events at all, then a click at the exact centre of the target. `Pointer` traces
an eased arc to a jittered point inside it instead.

**OS-level (Windows).** The stronger measure, and the one this module exists
for. Playwright's clicks are dispatched by the renderer — they carry
`isTrusted: true` but arrive with a timing and geometry signature that is
exactly the sort of synthetic event a bot check can tell apart from a person.
`real_click()` moves the actual Windows cursor and presses the actual button, so
what reaches the page is indistinguishable from a hand, because it is one.

The same applies to Chrome's omnibox: it is browser chrome, outside the DOM
entirely, so Playwright cannot touch it at all. Typing a URL there instead of
calling `page.goto()` produces a navigation with no automation signature.

Windows-only: pywinauto/pyautogui are marked win32 in requirements.txt. On Linux
and in Docker every desktop function logs once and returns False, and the run
continues on Playwright — which is headless-native. `real_click()` falls back to
an in-page click, so nothing here is load-bearing for correctness, only for how
the input looks.
"""
from __future__ import annotations

import asyncio
import math
import random
import sys

from backend import activity
from backend.automation import behavior

IS_WINDOWS = sys.platform == "win32"

# Filled on first use, so importing this module never fails on Linux.
_desktop = None
_desktop_tried = False


def _get_desktop():
    """pyautogui, or None where there is no desktop to drive."""
    global _desktop, _desktop_tried
    if _desktop_tried:
        return _desktop
    _desktop_tried = True
    if not IS_WINDOWS:
        activity.log("browser", "Desktop input unavailable (not Windows) — "
                                "falling back to in-page events", "info")
        return None
    try:
        import pyautogui

        pyautogui.FAILSAFE = True      # slam the pointer into a corner to abort
        pyautogui.PAUSE = 0            # we do our own, randomised, pacing
        _desktop = pyautogui
    except Exception as exc:  # noqa: BLE001
        activity.log("browser", f"Desktop input unavailable: {type(exc).__name__}", "warn")
    return _desktop


def available() -> bool:
    """Whether OS-level input can be used at all."""
    return _get_desktop() is not None


# ── In-page pointer ───────────────────────────────────────────────


def _bezier(start, end, steps: int) -> list[tuple[float, float]]:
    """Points along a quadratic curve from start to end.

    The control point is offset perpendicular to the straight line by a random
    fraction of its length, which is what makes the path bow the way a hand's
    does. A straight interpolation would be just as "moved" and just as
    obviously generated.
    """
    (x0, y0), (x1, y1) = start, end
    dx, dy = x1 - x0, y1 - y0
    distance = math.hypot(dx, dy) or 1.0
    bow = random.uniform(-0.22, 0.22) * distance
    cx = (x0 + x1) / 2 - dy / distance * bow
    cy = (y0 + y1) / 2 + dx / distance * bow

    points = []
    for i in range(steps + 1):
        t = behavior.ease_out_quad(i / steps)
        u = 1 - t
        points.append((u * u * x0 + 2 * u * t * cx + t * t * x1,
                       u * u * y0 + 2 * u * t * cy + t * t * y1))
    return points


# Swift pacing, for skimming a row of controls rather than committing an action.
SWIFT_TRAVEL_S = (0.06, 0.16)
SWIFT_PRE_CLICK_S = (0.04, 0.10)


class Pointer:
    """Tracks where the cursor is, so each move starts where the last one ended.

    Playwright's mouse has no readable position. Without this every move would
    start from (0, 0) and the trail would be a star out of the top-left corner.
    """

    def __init__(self, page):
        self.page = page
        self.x = random.uniform(80, 400)
        self.y = random.uniform(80, 400)

    async def move_to(self, x: float, y: float, steps: int | None = None,
                      travel: tuple[float, float] | None = None) -> None:
        steps = steps or random.randint(14, 30)
        # Spread the sampled travel time across the steps, so a long move takes
        # longer than a short one instead of every move taking the same time.
        per_step = behavior.pick(travel or behavior.POINTER_TRAVEL_S) / steps
        for px, py in _bezier((self.x, self.y), (x, y), steps):
            await self.page.mouse.move(px, py)
            await asyncio.sleep(per_step * random.uniform(0.6, 1.4))
        self.x, self.y = x, y

    async def move_to_element(self, locator, travel=None) -> bool:
        """Move to a jittered point inside `locator`. False if it has no box."""
        box = await locator.bounding_box()
        if not box:
            return False
        await self.move_to(*behavior.jitter(box), travel=travel)
        return True

    async def click(self, locator, *, swift: bool = False) -> bool:
        """Move to the element, pause, press. Falls back to a plain click if the
        element has no box — reaching the target matters more than the path.

        `swift` is for flicking through a strip of tabs: still a curved path to
        an off-centre point, just at the speed someone actually skims a row of
        controls. The slow pacing is sized for actions with a consequence — a
        send, an RSVP — and applying it to read-only browsing made twenty tabs
        take minutes without protecting anything.
        """
        try:
            await locator.scroll_into_view_if_needed(timeout=5_000)
        except Exception:  # noqa: BLE001 — already visible, or gone
            pass
        if not await self.move_to_element(
                locator, travel=SWIFT_TRAVEL_S if swift else None):
            await locator.click()
            return False
        await behavior.beat(SWIFT_PRE_CLICK_S if swift else behavior.PRE_CLICK_S)
        await self.page.mouse.down()
        await asyncio.sleep(random.uniform(0.04, 0.13))   # real click dwell time
        await self.page.mouse.up()
        return True

    async def drift(self) -> None:
        """A small aimless move — what a hand does while its owner reads."""
        await self.move_to(self.x + random.uniform(-90, 90),
                           self.y + random.uniform(-70, 70),
                           steps=random.randint(6, 14))


# ── OS-level input ────────────────────────────────────────────────


async def real_click(page, locator, pointer: "Pointer | None" = None) -> bool:
    """Click with the REAL Windows cursor. Returns True if it did.

    Falls back to an in-page click where no desktop is available, so callers
    never have to branch — see the module docstring on why this matters for the
    login button in particular.
    """
    from backend import settings as app_settings

    desktop = _get_desktop() if app_settings.current()["os_level_input"] else None
    try:
        await locator.scroll_into_view_if_needed(timeout=5_000)
    except Exception:  # noqa: BLE001
        pass

    box = await locator.bounding_box()
    if desktop is None or not box:
        pointer = pointer or Pointer(page)
        await pointer.click(locator)
        return False

    try:
        # Viewport origin in screen coordinates. outerHeight - innerHeight is
        # the browser's own chrome (tab strip + omnibox), which sits above the
        # page and must be added or every click lands that far too high.
        origin = await page.evaluate(
            "() => ({x: window.screenX, y: window.screenY,"
            " chrome: window.outerHeight - window.innerHeight})")
    except Exception:  # noqa: BLE001
        pointer = pointer or Pointer(page)
        await pointer.click(locator)
        return False

    # Jittered aim — never the centre. See behavior.AIM_JITTER.
    px, py = behavior.jitter(box)
    sx = origin["x"] + px
    sy = origin["y"] + origin["chrome"] + py

    _travel(desktop, sx, sy)
    await behavior.beat(behavior.PRE_CLICK_S)
    desktop.click()
    await behavior.beat(behavior.POST_CLICK_S)
    return True


def _travel(desktop, x: float, y: float) -> None:
    """Move the OS cursor along an eased path rather than teleporting.

    pyautogui's own `duration` interpolates linearly; the tween argument is what
    makes it decelerate into the target the way a hand does.
    """
    try:
        desktop.moveTo(x, y,
                       duration=behavior.pick(behavior.POINTER_TRAVEL_S),
                       tween=desktop.easeOutQuad)
    except Exception:  # noqa: BLE001 — a failed move must not kill the run
        try:
            desktop.moveTo(x, y)
        except Exception:  # noqa: BLE001
            pass


# ── Browser chrome (omnibox) ──────────────────────────────────────


def focus_address_bar() -> bool:
    """Ctrl+L. Needed because the omnibox is not in the page."""
    desktop = _get_desktop()
    if desktop is None:
        return False
    try:
        desktop.hotkey("ctrl", "l")
        return True
    except Exception as exc:  # noqa: BLE001
        activity.log("browser", f"Address bar focus failed: {type(exc).__name__}", "warn")
        return False


async def click_address_bar(page) -> bool:
    """Click into the omnibox at a random point in its LEFT THIRD.

    Aiming at the start of the text is where someone about to replace a URL
    actually clicks; a computed midpoint is not. Falls back to Ctrl+L when the
    window geometry can't be read.
    """
    desktop = _get_desktop()
    if desktop is None:
        return False
    try:
        origin = await page.evaluate(
            "() => ({x: window.screenX, y: window.screenY,"
            " w: window.outerWidth, chrome: window.outerHeight - window.innerHeight})")
    except Exception:  # noqa: BLE001
        return focus_address_bar()

    # The omnibox sits in the lower part of the browser chrome, past the tabs.
    box_top = origin["y"] + origin["chrome"] * 0.55
    box_height = max(18.0, origin["chrome"] * 0.30)
    x = origin["x"] + origin["w"] * behavior.pick(behavior.OMNIBOX_X_FRACTION)
    y = box_top + random.uniform(0.25, 0.75) * box_height

    _travel(desktop, x, y)
    await behavior.beat(behavior.PRE_CLICK_S)
    desktop.click()
    await asyncio.sleep(random.uniform(0.08, 0.2))
    desktop.hotkey("ctrl", "a")          # select whatever URL is there
    return True


async def type_desktop(text: str) -> bool:
    """Type into whatever has OS focus, with per-key human timing.

    pyautogui's `interval` is a fixed gap between keys — a metronome, and the
    exact thing this module exists to avoid. Each key is sent separately with a
    sampled delay instead.
    """
    desktop = _get_desktop()
    if desktop is None:
        return False
    try:
        for step in behavior.plan_typing(text, typos=False):
            if step.kind == "type":
                desktop.typewrite(step.text)
            elif step.kind == "back":
                for _ in range(step.count):
                    desktop.press("backspace")
            await asyncio.sleep(step.delay_s)
        return True
    except Exception as exc:  # noqa: BLE001
        activity.log("browser", f"Desktop typing failed: {type(exc).__name__}", "warn")
        return False


def press_desktop(*keys: str) -> bool:
    desktop = _get_desktop()
    if desktop is None:
        return False
    try:
        if len(keys) == 1:
            desktop.press(keys[0])
        else:
            desktop.hotkey(*keys)
        return True
    except Exception as exc:  # noqa: BLE001
        activity.log("browser", f"Desktop key failed: {type(exc).__name__}", "warn")
        return False


async def navigate_via_omnibox(page, url: str) -> bool:
    """Type a URL into Chrome's real address bar and press Enter.

    A navigation with no `page.goto()` signature: the omnibox is browser chrome,
    so nothing about this reaches the page as an automation event. Slower and
    Windows-only, which is why goto() remains the default everywhere else — this
    is for the handful of navigations worth spending the time on.

    Returns False if there is no desktop, so the caller can fall back to goto().
    """
    if not await click_address_bar(page):
        return False
    if not await type_desktop(url):
        return False
    await behavior.beat(behavior.PRE_ENTER_S)
    if not press_desktop("enter"):
        return False
    activity.log("browser", f"Typed {url} into the address bar", "ok")
    return True
