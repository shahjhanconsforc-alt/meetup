"""Generic page moves — everything that isn't Meetup-specific.

Navigation, scrolling a lazy-loading list, pulling links out of a page. Kept
separate from meetup.py so the site-specific selectors stay in one file: when
Meetup's markup changes, nothing in here should need touching.

The scroll helpers are where most of the "does this look like a person" work
happens, because Meetup's member and event lists are infinite-scroll and
collecting a roster means a long, visible session of nothing but scrolling:

  * **Glide, don't jump.** `window.scrollTo` moves the viewport in one frame and
    fires no wheel event. Scrolling is animated over many frames instead.
  * **Never a whole screen.** A step is 80–95% of the viewport, so content a
    reader would have passed actually passes, and offsets aren't round numbers.
  * **Go back sometimes.** People re-read. A session that only ever advances is
    not one.
  * **Drip-feed.** Take two to four rows off the visible list per pass rather
    than grabbing a screenful and burst-opening every one.
  * **Poll for readiness, don't sleep.** Fixed sleeps make timing track a clock;
    waiting for the page makes it track the page.
"""
from __future__ import annotations

import asyncio
import random
from urllib.parse import urljoin, urlparse

from backend import activity, settings as app_settings
from backend.automation import behavior
from backend.automation.cursor import Pointer

# How many scrolls in a row may add nothing before the list is called finished.
# One is too few — a slow fetch looks identical to the end of the list.
_IDLE_ROUNDS = 3

# Animate a scroll over many frames instead of jumping. requestAnimationFrame
# rather than a timer, so the motion is tied to the compositor the way a real
# wheel or trackpad gesture is.
_GLIDE_JS = """
([distance, ms]) => new Promise(resolve => {
  const start = window.scrollY;
  const t0 = performance.now();
  function step(now) {
    const t = Math.min(1, (now - t0) / ms);
    // easeOutQuad — a flick decelerates, it does not travel at constant speed.
    window.scrollTo(0, start + distance * (1 - (1 - t) * (1 - t)));
    if (t < 1) requestAnimationFrame(step); else resolve(window.scrollY);
  }
  requestAnimationFrame(step);
})
"""


async def goto(page, url: str, *, wait: str = "domcontentloaded") -> bool:
    """Navigate, returning False rather than raising on a timeout.

    "networkidle" is deliberately not the default: Meetup keeps analytics and
    websocket traffic going indefinitely, so waiting for idle waits for the full
    timeout on a page that was usable seconds earlier.
    """
    timeout = int(app_settings.current()["page_timeout_s"] * 1000)
    try:
        await page.goto(url, wait_until=wait, timeout=timeout)
        return True
    except Exception as exc:  # noqa: BLE001
        activity.log("browser", f"Could not load {url}: {type(exc).__name__}", "warn")
        return False


async def viewport_height(page) -> float:
    try:
        return float(await page.evaluate("() => window.innerHeight")) or 800.0
    except Exception:  # noqa: BLE001
        return 800.0


async def glide(page, distance: float) -> None:
    """Scroll `distance` pixels over an animated, eased path.

    A wheel event is also sent so the page sees the input a trackpad would
    produce; the animation is what the *scroll position* does, and pages watch
    both.
    """
    ms = int(behavior.pick((260, 620)))
    try:
        await page.mouse.wheel(0, distance * 0.35)
    except Exception:  # noqa: BLE001 — some contexts refuse synthetic wheel
        pass
    try:
        await page.evaluate(_GLIDE_JS, [distance, ms])
    except Exception:  # noqa: BLE001 — navigated away mid-scroll
        pass


async def scroll_step(page, pointer: Pointer | None = None, *,
                      multiplier: float = 1.0, swift: bool = False) -> None:
    """One scroll step, sized and paced like a real one.

    `swift` is the skimming pace — a bigger step and a shorter pause, for the
    read-only lists (a category's cards, an event's attendees) where the careful
    pacing bought nothing and cost most of the wall-clock of a roll.
    """
    height = await viewport_height(page)
    span = (behavior.SWIFT_SCROLL_STEP_FRACTION if swift
            else behavior.SCROLL_STEP_FRACTION)
    distance = height * behavior.pick(span) * multiplier
    await glide(page, distance)
    # The idle drift is a nice touch on a slow read and pure latency on a skim.
    if pointer is not None and not swift and random.random() < 0.25:
        await pointer.drift()
    await behavior.beat(behavior.SWIFT_SCROLL_PAUSE_S if swift
                        else behavior.SCROLL_PAUSE_S)


async def scroll_back(page, steps: int | None = None) -> None:
    """Flick back up a step or two — re-reading, without reloading anything."""
    height = await viewport_height(page)
    steps = steps or behavior.pick_int(behavior.BACK_SCROLL_STEPS)
    await glide(page, -height * behavior.pick((0.5, 0.9)) * steps)
    await behavior.beat(behavior.SCROLL_PAUSE_S)


async def wait_ready(page, selector: str, *, timeout_s: float = 8.0,
                     minimum: int = 1) -> bool:
    """Poll until at least `minimum` matches exist, or give up.

    Readiness polling instead of a fixed sleep, so timing tracks the page rather
    than a clock — on a fast response the run moves on immediately, and on a slow
    one it doesn't act against a half-rendered list.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            if await page.locator(selector).count() >= minimum:
                return True
        except Exception:  # noqa: BLE001 — mid-navigation
            pass
        await asyncio.sleep(behavior.pick((0.12, 0.3)))
    return False


async def scroll_to_end(page, *, collect, ctl=None, target: int = 0,
                        max_rounds: int = 400, bio_mode: bool = False,
                        on_item=None, swift: bool = False) -> list:
    """Scroll a lazy list, collecting as it goes.

    `collect(page)` returns whatever is currently on screen; results accumulate,
    deduped by username. `on_item(item)` — when given — is awaited for each newly
    released row, which is what "bio mode" uses to open a profile.

    Stops on: the target being reached, `max_rounds`, the run being stopped, or
    `_IDLE_ROUNDS` consecutive scrolls that added nothing.

    Returns the accumulated list in the order first seen — which for Meetup is
    roster order, and that is worth preserving.
    """
    seen: dict[str, object] = {}
    pending: list = []
    idle = 0
    steps = 0
    pointer = Pointer(page)

    multiplier = behavior.BIO_STEP_MULTIPLIER if bio_mode else 1.0
    back_every = (behavior.pick_int(behavior.BIO_BACK_SCROLL_EVERY) if bio_mode
                  else behavior.BACK_SCROLL_EVERY)

    for _ in range(max_rounds):
        if ctl is not None and not await ctl.checkpoint(f"Collected {len(seen)}"):
            break

        before = len(seen)
        for item in await collect(page):
            key = item if isinstance(item, str) else str(item.get("username") or item)
            if key and key not in seen:
                seen[key] = item
                pending.append(item)

        # Drip-feed: release a few at a time instead of burst-opening a whole
        # screenful, which is the shape a person's reading simply never has.
        if on_item is not None and pending:
            release = behavior.pick_int(behavior.RESULTS_RELEASE_PER_PASS)
            if bio_mode:
                release = 1                     # one profile per step, no more
            for item in pending[:release]:
                if ctl is not None and not await ctl.checkpoint():
                    break
                await on_item(item)
                if bio_mode:
                    await behavior.beat(behavior.BIO_DWELL_S)
            del pending[:release]

        if target and len(seen) >= target:
            break

        idle = idle + 1 if len(seen) == before else 0
        if idle >= _IDLE_ROUNDS:
            break

        await scroll_step(page, pointer, multiplier=multiplier, swift=swift)
        steps += 1
        # The re-read flick is what a reader does; a skim does not double back,
        # and on a long roster it was a second and a half every five steps.
        if not swift and steps % back_every == 0:
            await scroll_back(page)

    return list(seen.values())


async def dwell_menu(page=None) -> float:
    """How long a menu stays open before the next move. A menu that opens and
    closes in 60ms was never read."""
    return await behavior.beat(behavior.MENU_DWELL_S)


async def dwell_action(page=None) -> float:
    """The beat after an action surface appears — long enough to have looked."""
    return await behavior.beat(behavior.ACTION_DWELL_S)


async def links_on_page(page, *, pattern: str = "") -> list[str]:
    """Every href on the page, absolute, optionally filtered by substring."""
    hrefs = await page.eval_on_selector_all(
        "a[href]", "els => els.map(e => e.getAttribute('href'))")
    base = page.url
    out, seen = [], set()
    for href in hrefs or []:
        if not href:
            continue
        absolute = urljoin(base, href)
        if pattern and pattern not in absolute:
            continue
        clean = absolute.split("?")[0].split("#")[0]
        if clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def path_segment(url: str, index: int) -> str:
    """The nth path segment of a URL, or "" — how a slug is pulled out of a
    Meetup link without a regex per URL shape."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[index] if 0 <= index < len(parts) else ""


async def text_of(page, selector: str, default: str = "") -> str:
    """The trimmed text of the first match, or `default` if it isn't there.

    Meetup renders a lot of these conditionally — a profile with no bio simply
    has no bio element — so "missing" is the normal case, not an error.
    """
    try:
        element = page.locator(selector).first
        if await element.count() == 0:
            return default
        return ((await element.inner_text()) or "").strip() or default
    except Exception:  # noqa: BLE001 — detached between query and read
        return default


async def attr_of(page, selector: str, name: str, default: str = "") -> str:
    try:
        element = page.locator(selector).first
        if await element.count() == 0:
            return default
        return (await element.get_attribute(name)) or default
    except Exception:  # noqa: BLE001
        return default


async def dismiss_overlays(page, selectors: list[str]) -> int:
    """Click away cookie banners and modals that sit over the content.

    Meetup shows a cookie consent banner and, once logged in, an intermittent
    "get the app" interstitial. Both intercept clicks meant for the page beneath,
    so a run that ignores them fails on a selector that is genuinely there.
    """
    dismissed = 0
    for selector in selectors:
        try:
            element = page.locator(selector).first
            if await element.count() and await element.is_visible():
                await element.click(timeout=3_000)
                dismissed += 1
                await behavior.beat((0.3, 0.8))
        except Exception:  # noqa: BLE001 — gone on its own, or never there
            continue
    return dismissed
