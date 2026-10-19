"""Type like a person, not like a clipboard.

`page.fill()` sets an input's value in one event. Every keystroke-timing check
sees a 400-character message appear in zero milliseconds with no keydown events,
which is about as clear a bot signal as exists. This sends key by key with
pauses that vary the way real typing does — including deliberate typos that get
backspaced and retyped, because a long message entered with *zero* corrections
is itself an anomaly.

The timing model lives in behavior.py (`plan_typing`), not here. This module
only executes a plan against a page, which is what lets the pacing be tested
without a browser: the plan is data, and replaying it must always end with the
intended text.
"""
from __future__ import annotations

import asyncio

from backend import settings as app_settings
from backend.automation import behavior


async def _run_plan(page, plan, ctl=None) -> int:
    """Execute a typing plan against whatever currently has focus.

    `ctl` makes the typing itself pausable and stoppable, which it has to be: a
    2000-character message typed honestly takes over a minute, and a Pause that
    only lands between RECIPIENTS is a button that does nothing for the length of
    a message. Checked between steps rather than between keys — a step is one
    burst of a few characters, so the granularity is already sub-second and this
    costs one flag read per burst.

    Raises control.RunStopped when the run was ended mid-message, so the caller
    never goes on to press Send on a half-written one.
    """
    typed = 0
    for step in plan:
        # Blocks here while paused — the composer keeps what is already in it and
        # the rest of the message goes in on Continue.
        if ctl is not None:
            await ctl.guard()
        if step.kind == "type":
            await page.keyboard.type(step.text)
            typed += len(step.text)
        elif step.kind == "back":
            for _ in range(step.count):
                await page.keyboard.press("Backspace")
        await asyncio.sleep(step.delay_s)
    return typed


def _target(page, selector):
    """The field to type into, from either a selector string or a locator.

    Passing a LOCATOR is what a caller does when it has already found the right
    element — and on a page with several matches that is the only way to be sure
    the text lands in it. `page.locator(sel).first` is first in DOM ORDER, so a
    caller that searched for the first VISIBLE match and then handed the selector
    back here was silently typing into a different element: Meetup's inbox keeps
    a textarea per conversation pane, and the one at the top of the document is
    not the open thread's.
    """
    return selector if hasattr(selector, "click") else page.locator(selector).first


async def type_text(page, selector, text: str, *, clear: bool = False,
                    speed_ms: int | None = None, jitter_ms: int | None = None,
                    typos: bool = True, ctl=None) -> int:
    """Type `text` into `selector` key by key. Returns characters sent.

    `selector` is a CSS string or an already-resolved locator — see _target.
    `ctl` makes the typing pausable and stoppable — see _run_plan.

    The field is focused by clicking it rather than by `focus()`: a focus event
    with no preceding pointer event is another free tell, and the click is free.
    """
    values = app_settings.current()
    element = _target(page, selector)
    await element.click()

    if clear:
        # Select-all + delete, as a person would — not fill(""), which fires no
        # key events at all.
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        await behavior.beat((0.1, 0.3))

    plan = behavior.plan_typing(
        text,
        speed_ms=speed_ms if speed_ms is not None else values["type_speed_ms"],
        jitter_ms=jitter_ms if jitter_ms is not None else values["type_jitter_ms"],
        typos=typos and values["human_typing"],
    )
    return await _run_plan(page, plan, ctl)


async def type_or_fill(page, selector, text: str, *, clear: bool = False,
                       ctl=None) -> int:
    """type_text when human typing is on, a plain fill when it isn't.

    The setting exists because a 2000-character campaign message takes over a
    minute to type honestly, and an operator running against their own test
    group shouldn't have to wait for that.

    Takes a selector string or a locator, like type_text — the fast path has to
    land in the same box the slow one would, or turning human typing off would
    quietly change WHERE the message goes.
    """
    if app_settings.current()["human_typing"]:
        return await type_text(page, selector, text, clear=clear, ctl=ctl)
    # The fill path is one event and cannot be interrupted halfway — but it also
    # takes no time, so there is nothing to interrupt. It still checks first, so
    # a stopped run does not write one last message on its way out.
    if ctl is not None:
        await ctl.guard()
    await _target(page, selector).fill(text or "")
    return len(text or "")


async def type_search(page, selector: str, seed: str) -> int:
    """Type a search seed — faster than prose, and never with a typo.

    You type a two-character seed quickly and you type it correctly; applying
    the prose model here would produce a stream of corrected search queries,
    which is its own strange pattern.
    """
    low, high = behavior.SEARCH_TYPE_MS
    return await type_text(page, selector, seed, clear=True,
                           speed_ms=int(low), jitter_ms=int(high - low),
                           typos=False)


async def pause(reason: str = "") -> float:
    """The configured between-actions delay, randomised across its range.

    Uniform, not fixed: a constant gap between actions is a metronome, and a
    metronome is the easiest possible thing to spot in a request log.
    """
    values = app_settings.current()
    return await behavior.beat((values["action_delay_min_s"],
                                values["action_delay_max_s"]))


async def think() -> float:
    """A short "reading it" pause — before replying, after a page loads."""
    return await behavior.beat((0.8, 2.6))


async def field_gap() -> float:
    """Between two fields of a form. Moving from a username box to a password
    box takes a person a moment; submitting both in the same tick does not."""
    return await behavior.beat(behavior.FIELD_GAP_S)


async def before_submit() -> float:
    """The beat before pressing a submit button — the glance back over what was
    typed. Slightly longer than a field gap, for the same reason."""
    return await behavior.beat(behavior.PRE_SUBMIT_S)
