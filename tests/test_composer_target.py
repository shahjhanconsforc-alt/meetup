"""Which box the message actually goes into.

Meetup's inbox keeps a <textarea> per conversation pane, and the composer for
the thread on screen is not the first one in the document. Two separate bugs
came out of that, and both looked identical from the outside — the chat opened,
and nothing was ever written in it:

  1. the flow found the first VISIBLE match, then threw it away and typed into
     `page.locator(sel).first`, which is the first match in DOM ORDER;
  2. the candidate list ends in a bare `textarea` catch-all, and comma-joining
     the candidates into one selector makes that catch-all rank equally with the
     specific ones above it.

So: type into the element that was found, and try the candidates in order.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.automation import meetup
from backend.automation import typing as human
from tests.test_first_chat import FakeLocator, FakePage


# ── typing: a locator is used as given ───────────────────────────────────


class _Box:
    """The one field, recording what reached it."""

    def __init__(self):
        self.filled = None
        self.clicks = 0

    async def click(self, **kwargs):
        self.clicks += 1

    async def fill(self, text):
        self.filled = text


class _Page:
    """A page whose `locator` answers with a DIFFERENT field — so a test fails
    loudly if the code re-resolves the selector instead of using what it was
    handed."""

    def __init__(self, wrong):
        self.wrong = wrong

    def locator(self, selector):
        class _One:
            first = self.wrong
        return _One()


@pytest.fixture
def no_human(monkeypatch):
    """Human typing off, so type_or_fill takes its fill() path — the point here
    is WHICH element is written to, not how the keys are paced."""
    monkeypatch.setattr(human.app_settings, "current",
                        lambda: {**human.app_settings.defaults(),
                                 "human_typing": False})


def test_a_locator_is_written_to_as_given(no_human):
    right, wrong = _Box(), _Box()

    asyncio.run(human.type_or_fill(_Page(wrong), right, "hello"))

    assert right.filled == "hello"
    assert wrong.filled is None


def test_a_selector_string_still_resolves_against_the_page(no_human):
    """The older shape, and still a real one — most callers have a selector and
    only one field to hit."""
    wrong = _Box()          # "wrong" only in the test above; here it is the field

    asyncio.run(human.type_or_fill(_Page(wrong), "textarea", "hello"))

    assert wrong.filled == "hello"


def test_the_typed_path_clicks_the_locator_it_was_given(monkeypatch):
    """Human typing on: the field is focused by CLICKING it, and that click has
    to land on the same element the text is meant for."""
    right, wrong = _Box(), _Box()
    monkeypatch.setattr(human.app_settings, "current",
                        lambda: {**human.app_settings.defaults(),
                                 "human_typing": True, "type_speed_ms": 10,
                                 "type_jitter_ms": 0})

    class _Keyboard:
        def __init__(self):
            self.text = ""

        async def type(self, chunk):
            self.text += chunk

        async def press(self, key):
            pass

    async def _zero(*args, **kwargs):
        return None

    page = _Page(wrong)
    page.keyboard = _Keyboard()
    monkeypatch.setattr(human.asyncio, "sleep", _zero)

    asyncio.run(human.type_text(page, right, "hi", clear=True))

    assert right.clicks == 1
    assert wrong.clicks == 0
    assert page.keyboard.text == "hi"


# ── meetup: the composer that gets found is the one that gets written ────


@pytest.fixture(autouse=True)
def instant(monkeypatch):
    async def _zero(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(meetup.asyncio, "sleep", _zero)
    monkeypatch.setattr(meetup.human, "think", _zero)
    monkeypatch.setattr(meetup.human, "pause", _zero)
    monkeypatch.setattr(meetup.human, "before_submit", _zero)


def test_the_visible_composer_is_the_one_written_to(monkeypatch):
    """A hidden pane's textarea sits ahead of the open thread's in the document.
    Writing to `.first` put the message in the hidden one, which is exactly what
    "it opens the chat but never writes" looked like."""
    handed = {}

    async def _type(page, selector, text, clear=False):
        handed["selector"] = selector
        return len(text)

    monkeypatch.setattr(meetup.human, "type_or_fill", _type)
    monkeypatch.setattr(meetup, "_clicking_send", lambda: False)

    page = FakePage(url="https://www.meetup.com/messages/", matches={
        "message_box": [{"visible": False, "pane": "another conversation"},
                        {"visible": True, "pane": "the open thread"}],
    })
    result = asyncio.run(meetup.send_in_open_chat(page, "hello"))

    assert result["ok"] is True
    # A locator was handed over, not the selector string — and it is the visible
    # pane's box, not the first in the document.
    box = handed["selector"]
    assert isinstance(box, FakeLocator)
    assert box._elements[0]["pane"] == "the open thread"


def test_no_composer_at_all_is_still_reported(monkeypatch):
    """The fix must not turn "this member cannot be messaged" into a crash."""
    monkeypatch.setattr(meetup, "_INBOX_WAIT_S", 0.05)
    page = FakePage(url="https://www.meetup.com/messages/", matches={})

    result = asyncio.run(meetup.send_in_open_chat(page, "hello"))

    assert result["ok"] is False
    assert "No composer" in result["detail"]


# ── the candidate order ──────────────────────────────────────────────────


def test_the_catch_all_textarea_is_last():
    """The list ends in a bare `textarea` on purpose, and everything ahead of it
    is anchored to something readable. If a new candidate is ever appended after
    the catch-all it can never be reached."""
    assert meetup.SEL["message_box"][-1] == "textarea"


def test_the_composer_is_pinned_by_its_placeholder():
    """Meetup's composer carries no name and no testid — every class on it is a
    design-system token. The placeholder is the identifying attribute."""
    assert any("placeholder" in candidate
               for candidate in meetup.SEL["message_box"][:-1])
