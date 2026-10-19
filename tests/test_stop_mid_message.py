"""Pause and End have to reach the TYPING, not just the gaps between recipients.

A campaign spends most of its wall-clock inside one message: 2000 characters at
a real typist's rate is over a minute. Checked only at `ctl.checkpoint()` between
recipients, Pause was a button that did nothing for the length of a message and
End could leave a half-written one sitting in a composer with Send about to be
pressed on it.

So the typing plan itself watches the run: it holds while paused and abandons the
message when the run is ended — and a stopped run never reaches the Send click.
"""
from __future__ import annotations

import asyncio

import pytest

from backend import control
from backend.automation import typing as human


class Keyboard:
    """A field that keeps what was typed into it.

    Backspace is honoured, because the typing model deliberately makes typos and
    corrects them — a fake that swallowed the corrections would leave the wrong
    characters in and make "the message arrived intact" impossible to assert.
    """

    def __init__(self):
        self.text = ""

    async def type(self, chunk):
        self.text += chunk

    async def press(self, key):
        if key == "Backspace":
            self.text = self.text[:-1]


class Box:
    async def click(self, **kwargs):
        pass

    async def fill(self, text):
        self.filled = text


class Page:
    def __init__(self):
        self.keyboard = Keyboard()

    def locator(self, selector):
        class _One:
            first = Box()
        return _One()


@pytest.fixture(autouse=True)
def typing_on(monkeypatch):
    """Human typing on, at the fastest the settings allow.

    The per-key sleeps are left REAL rather than stubbed out. `human.asyncio` is
    the asyncio module itself, so patching its sleep also silences the one these
    tests use to drive the loop — and a driver that cannot yield can never see
    the typing task pause. 1ms a key is fast enough for a test and still a real
    suspension point, which is the whole thing being measured.
    """
    monkeypatch.setattr(human.app_settings, "current",
                        lambda: {**human.app_settings.defaults(),
                                 "human_typing": True, "type_speed_ms": 1,
                                 "type_jitter_ms": 0})


# ── End, mid-message ─────────────────────────────────────────────────────


def test_ending_the_run_abandons_the_message():
    """Raised, not returned: the caller must not go on to press Send on a
    half-written composer, and an exception is the only answer no intermediate
    step can accidentally ignore."""
    ctl = control.Run("outreach")
    ctl.stop()
    page = Page()

    with pytest.raises(control.RunStopped):
        asyncio.run(human.type_text(page, Box(), "a long message", clear=True,
                                    ctl=ctl))

    assert page.keyboard.text == ""          # not one character


def test_a_stop_partway_through_stops_partway_through():
    """The realistic case: End pressed while the message is going in."""
    ctl = control.Run("outreach")
    page = Page()
    real_type = page.keyboard.type

    async def _type(chunk):
        await real_type(chunk)
        if len(page.keyboard.text) >= 5:
            ctl.stop()

    page.keyboard.type = _type

    with pytest.raises(control.RunStopped):
        asyncio.run(human.type_text(page, Box(), "x" * 400, clear=True, ctl=ctl))

    # It stopped near where End was pressed rather than finishing the message.
    assert 5 <= len(page.keyboard.text) < 400


def test_the_fast_path_checks_before_it_writes(monkeypatch):
    """Human typing off is one fill() with nothing to interrupt — but a stopped
    run must still not write one last message on its way out."""
    monkeypatch.setattr(human.app_settings, "current",
                        lambda: {**human.app_settings.defaults(),
                                 "human_typing": False})
    ctl = control.Run("outreach")
    ctl.stop()
    box = Box()

    with pytest.raises(control.RunStopped):
        asyncio.run(human.type_or_fill(Page(), box, "hello", ctl=ctl))

    assert not hasattr(box, "filled")


def test_no_control_means_no_interruption():
    """Every other caller passes nothing and types straight through."""
    page = Page()
    asyncio.run(human.type_text(page, Box(), "hello", clear=True))

    assert page.keyboard.text == "hello"


# ── Pause, mid-message ───────────────────────────────────────────────────


def test_pausing_holds_the_typing_and_continuing_finishes_it():
    """Not a stop: the composer keeps what is already in it and the rest of the
    message goes in on Continue."""
    ctl = control.Run("outreach")
    page = Page()
    real_type = page.keyboard.type
    # ONCE. Re-pausing on every step after the fifth character would hold the
    # typing again the instant the test resumed it, and the run would never end.
    pauses = []

    async def _type(chunk):
        await real_type(chunk)
        if len(page.keyboard.text) >= 5 and not pauses:
            pauses.append(ctl.pause())

    page.keyboard.type = _type

    async def _drive():
        run = asyncio.ensure_future(
            human.type_text(page, Box(), "x" * 60, clear=True, ctl=ctl))
        # Polled, not a fixed wait: a 1ms sleep on Windows is really ~15ms, so
        # how long the first few keys take is a property of the clock rather
        # than of anything under test.
        await _until(lambda: ctl.status == "paused")
        held = len(page.keyboard.text)
        assert held < 60                       # stopped mid-message
        await asyncio.sleep(0.15)
        assert len(page.keyboard.text) == held  # and stayed there
        ctl.resume()
        return await run

    asyncio.run(_drive())

    assert page.keyboard.text == "x" * 60      # finished on Continue


async def _until(cond, seconds: float = 5.0):
    """Wait for `cond`, or fail the test rather than hang the suite."""
    left = seconds
    while left > 0:
        if cond():
            return
        await asyncio.sleep(0.01)
        left -= 0.01
    raise AssertionError("condition never became true")
