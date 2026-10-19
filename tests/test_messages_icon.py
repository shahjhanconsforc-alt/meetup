"""Reaching the inbox by clicking the header's Messages icon.

Following the link the header already offers is what a person does; a bare goto
to /messages/ is a URL nobody typed, arriving with no referrer and no preceding
click on any page that links there. The goto stays as the fallback.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.automation import meetup
from tests.test_first_chat import FakePage, inbox_page


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def _instant(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(meetup.asyncio, "sleep", _instant)
    monkeypatch.setattr(meetup.human, "think", _instant)
    monkeypatch.setattr(meetup, "_INBOX_WAIT_S", 0.2)


@pytest.fixture(autouse=True)
def stub_page_bits(monkeypatch):
    """Pointer clicks and navigations, recorded. A click on the icon moves the
    fake page's URL the way following a real link does."""
    log = {"clicks": [], "gotos": []}

    class _Pointer:
        def __init__(self, page):
            self.page = page

        async def click(self, locator):
            element = locator._elements[0] if locator._elements else {}
            log["clicks"].append(element.get("_key") or element.get("name") or "?")
            if element.get("href"):
                self.page.url = element["href"]      # a link moves the page
            hook = element.get("on_click")
            if hook:
                hook(self.page)                      # a control changes the DOM
            return True

    async def _goto(page, url, **kwargs):
        log["gotos"].append(url)
        page.url = url
        return True

    async def _dismiss(page, selectors):
        return 0

    monkeypatch.setattr(meetup, "Pointer", _Pointer)
    monkeypatch.setattr(meetup.visit, "goto", _goto)
    monkeypatch.setattr(meetup.visit, "dismiss_overlays", _dismiss)
    return log


def header_page(*, icon=True, url="https://www.meetup.com/"):
    """A signed-in Meetup page, with or without the header icon."""
    matches = {"logged_in_marker": [{"name": "avatar"}]}
    if icon:
        matches["header_messages"] = [
            {"name": "messages-links", "href": "https://www.meetup.com/messages/"}]
    return FakePage(matches, url=url)


# ── The icon itself ──────────────────────────────────────────────────────

def test_clicking_the_icon_opens_the_inbox(stub_page_bits):
    page = header_page()
    result = asyncio.run(meetup.click_messages_icon(page))

    assert result["ok"] is True
    assert stub_page_bits["clicks"] == ["header_messages"]
    assert page.url == "https://www.meetup.com/messages/"


def test_a_missing_icon_is_reported_not_raised():
    """It only exists on a signed-in header, so absent usually means logged out
    rather than a dead selector."""
    result = asyncio.run(meetup.click_messages_icon(header_page(icon=False)))
    assert result["ok"] is False
    assert result["detail"] == "No Messages icon in the header"


def test_a_click_that_goes_nowhere_is_a_failure(stub_page_bits, monkeypatch):
    """The href is the only thing that says the click landed — a decorative or
    intercepted anchor leaves the URL where it was."""
    page = header_page()
    page.matches["header_messages"][0].pop("href")

    result = asyncio.run(meetup.click_messages_icon(page))

    assert result["ok"] is False
    assert "did not open" in result["detail"]


def test_the_live_anchors_are_covered():
    """Straight off the rendered header: an id, a data-element-name and an
    aria-label, so three independent anchors survive a class rename."""
    assert "#messages-links" in meetup.SEL["header_messages"]
    assert "[data-element-name='header-messages']" in meetup.SEL["header_messages"]
    assert "a[aria-label='Messages']" in meetup.SEL["header_messages"]


# ── open_inbox prefers the click ─────────────────────────────────────────

def test_the_inbox_is_reached_by_clicking_when_the_icon_is_there(stub_page_bits):
    page = header_page()
    page.matches["chat_row"] = []

    asyncio.run(meetup.open_inbox(page))

    assert stub_page_bits["clicks"] == ["header_messages"]
    assert stub_page_bits["gotos"] == []          # no typed URL


def test_a_page_without_the_icon_falls_back_to_navigating(stub_page_bits):
    page = header_page(icon=False)
    result = asyncio.run(meetup.open_inbox(page))

    assert result["ok"] is True
    assert stub_page_bits["clicks"] == []
    assert stub_page_bits["gotos"] == [meetup.INBOX_URL]


def test_a_browser_already_on_the_inbox_is_left_alone(stub_page_bits):
    page = header_page(url="https://www.meetup.com/messages/")
    asyncio.run(meetup.open_inbox(page))

    assert stub_page_bits["clicks"] == []
    assert stub_page_bits["gotos"] == []


def test_a_logged_out_page_still_refuses(stub_page_bits):
    """Reaching /messages/ logged out lands on the login form, which has none of
    the row selectors and would otherwise read as an empty inbox."""
    page = FakePage({}, url="https://www.meetup.com/")
    result = asyncio.run(meetup.open_inbox(page))

    assert result["ok"] is False
    assert "Not logged in" in result["detail"]


def test_the_first_chat_flow_still_runs_end_to_end(stub_page_bits):
    """The click is a change of route to the same place — the flow above it must
    not notice."""
    page = inbox_page(rows=1, name="Ada")
    page.url = "https://www.meetup.com/messages/"
    page.matches["message_send"][0]["on_click"] = (
        lambda p: p.composer.__setitem__("value", ""))

    async def _type(_page, selector, text, clear=False):
        _page.typed.append((selector, text))
        for element in _page.matches.get("message_box", []):
            element["value"] = text
        return len(text)

    import backend.automation.typing as human
    original = meetup.human.type_or_fill
    meetup.human.type_or_fill = _type
    try:
        result = asyncio.run(meetup.message_first_chat(page, "hello"))
    finally:
        meetup.human.type_or_fill = original

    assert result["ok"] is True
    assert "Ada 0" in result["detail"]
