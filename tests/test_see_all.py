""""See all" on an event page, and why the first events kept missing it.

The event page is a React app and `goto` only waits for domcontentloaded, so the
attendee section — and the "See all" beneath it — is not in the DOM when the
navigation returns. open_attendees looked for the link ONCE, at that moment,
found nothing, and silently typed the /attendees/ URL instead. Cold page loads
are the slowest, which is exactly why it was the first events of a run that
missed the click and later ones sometimes made it.

So: poll for the link and click it the moment it renders, with one scroll partway
through for the case where the section is mounted on scroll rather than on load.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.automation import meetup
from tests.test_first_chat import FakePage

EVENT = "https://www.meetup.com/a-group/events/315749481/"


class LateLink(FakePage):
    """An event page whose "See all" arrives after N looks, not on load.

    `looks` is how many times the link is asked for before it exists — the fake
    equivalent of React finishing its render a beat after the navigation.
    """

    def __init__(self, looks: int = 3, on_scroll: bool = False, **kwargs):
        super().__init__(matches={}, url=EVENT, **kwargs)
        self.left = looks
        self.on_scroll = on_scroll     # only appears once the page is scrolled
        self.scrolled = 0

    def locator(self, selector):
        if self._key_for(selector) == "attendees_link":
            if self.left > 0:
                self.left -= 1
            if self.left == 0 and not (self.on_scroll and not self.scrolled):
                self.matches["attendees_link"] = [{
                    "visible": True, "href": EVENT + "attendees/",
                    "on_click_url": EVENT + "attendees/"}]
        return super().locator(selector)


@pytest.fixture
def stubs(monkeypatch):
    """A pointer that follows a link, and the pacing stripped out."""
    clicks: list[str] = []
    gotos: list[str] = []

    async def _instant(*args, **kwargs):
        return 0.0

    async def _goto(page, url, **kwargs):
        gotos.append(url)
        page.url = url
        return True

    async def _scroll(page, *args, **kwargs):
        page.scrolled += 1

    async def _dismiss(page, selectors):
        return 0

    class _Pointer:
        def __init__(self, page):
            self.page = page

        async def click(self, locator, *, swift=False):
            element = locator._elements[0] if locator._elements else {}
            clicks.append("see-all")
            if element.get("on_click_url"):
                self.page.url = element["on_click_url"]
            return True

    monkeypatch.setattr(meetup.asyncio, "sleep", _instant)
    monkeypatch.setattr(meetup.visit, "goto", _goto)
    monkeypatch.setattr(meetup.visit, "scroll_step", _scroll)
    monkeypatch.setattr(meetup.visit, "dismiss_overlays", _dismiss)
    monkeypatch.setattr(meetup, "Pointer", _Pointer)
    return {"clicks": clicks, "gotos": gotos}


# ── THE bug ──────────────────────────────────────────────────────────────


def test_a_link_that_renders_after_the_load_is_still_clicked(stubs):
    """The whole report: the first events of a run never got the click."""
    page = LateLink(looks=4)

    assert asyncio.run(meetup.open_attendees(page, EVENT)) is True
    assert stubs["clicks"] == ["see-all"]
    assert stubs["gotos"] == []          # the route was never needed


def test_a_link_that_is_there_at_once_is_clicked_at_once(stubs):
    """No wait paid on a page that had already rendered."""
    page = LateLink(looks=1)

    assert asyncio.run(meetup.open_attendees(page, EVENT)) is True
    assert stubs["clicks"] == ["see-all"]


def test_a_section_mounted_on_scroll_is_reached(stubs):
    """The attendee row is well down the event page. A section that mounts on
    scroll never arrives to something that only waits."""
    page = LateLink(looks=1, on_scroll=True)

    assert asyncio.run(meetup.open_attendees(page, EVENT)) is True
    assert page.scrolled >= 1
    assert stubs["clicks"] == ["see-all"]


# ── What must not change ─────────────────────────────────────────────────


def test_an_event_with_no_link_still_falls_back_to_the_route(stubs):
    """Some events hide their attendee list. The wait is spent, then the route
    is taken — it must not become a failure."""
    page = FakePage(matches={}, url=EVENT)

    ok = asyncio.run(meetup.open_attendees(page, EVENT))

    assert stubs["clicks"] == []
    assert stubs["gotos"] == [EVENT.rstrip("/") + "/attendees/"]
    assert ok is True


def test_a_page_already_on_the_list_does_nothing(stubs):
    """No wait, no click, no navigation — it is already there."""
    page = FakePage(matches={}, url=EVENT + "attendees/")

    assert asyncio.run(meetup.open_attendees(page, EVENT)) is True
    assert stubs["clicks"] == [] and stubs["gotos"] == []


def test_a_page_that_will_not_scroll_is_not_fatal(stubs, monkeypatch):
    """One nudge is best-effort. A page that refuses it still gets the rest of
    the wait and then the route."""
    async def _boom(*args, **kwargs):
        raise RuntimeError("no scrolling here")

    monkeypatch.setattr(meetup.visit, "scroll_step", _boom)
    page = FakePage(matches={}, url=EVENT)

    assert asyncio.run(meetup.open_attendees(page, EVENT)) is True
    assert stubs["gotos"] == [EVENT.rstrip("/") + "/attendees/"]


def test_the_href_shape_is_tried_before_the_text(stubs):
    """`a[href*='/attendees']` is a Meetup ROUTE and outlives any label, so it
    has to win over the `See all` text match — which a comma-joined selector
    would not guarantee."""
    candidates = meetup.SEL["attendees_link"]
    assert candidates[0] == "a[href*='/attendees']"
    assert any("has-text" in c for c in candidates[1:])
