"""Inbox -> the top conversation -> type -> send.

Driven against a fake page rather than Chrome: what is under test is the order
of the steps and what each failure reports, and a real browser would make that
slow, flaky and dependent on Meetup being up.

The fake models the parts of Playwright's Locator API the flow actually touches
— count/nth/is_visible/inner_text/input_value — so a selector that matches
nothing behaves the way it would on a live page.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.automation import meetup


# ── A fake page ──────────────────────────────────────────────────────────

class FakeLocator:
    def __init__(self, elements):
        self._elements = list(elements)

    async def count(self):
        return len(self._elements)

    def nth(self, index):
        return FakeLocator(self._elements[index:index + 1])

    @property
    def first(self):
        return FakeLocator(self._elements[:1])

    def locator(self, selector):
        """Search inside the first element — how the row's name is read."""
        if not self._elements:
            return FakeLocator([])
        return FakeLocator(self._elements[0].get("children", {}).get(selector, []))

    async def is_visible(self):
        return bool(self._elements) and self._elements[0].get("visible", True)

    async def inner_text(self):
        return self._elements[0].get("text", "")

    async def input_value(self):
        return self._elements[0].get("value", "")

    async def get_attribute(self, name):
        return self._elements[0].get(name) if self._elements else None

    async def scroll_into_view_if_needed(self, **kwargs):
        if self._elements:
            self._elements[0]["scrolled"] = True

    async def click(self, **kwargs):
        """Playwright's own click — the fallback when the pointer misses."""
        if not self._elements:
            raise RuntimeError("no element to click")
        element = self._elements[0]
        element["direct_clicks"] = element.get("direct_clicks", 0) + 1
        hook = element.get("on_direct_click")
        if hook:
            hook()

    async def focus(self):
        if self._elements:
            self._elements[0]["focused"] = True

    async def bounding_box(self):
        """The element's box, when it was given one.

        None otherwise, which is what Playwright answers for something with no
        layout — and what the code has to cope with before it can aim a pointer
        at anything.
        """
        return self._elements[0].get("box") if self._elements else None

    async def press(self, key):
        """A key sent to one element — how a slider is asked for its maximum."""
        if not self._elements:
            raise RuntimeError("no element to press")
        element = self._elements[0]
        element.setdefault("keys", []).append(key)
        hook = element.get("on_press")
        if hook:
            hook(key)

    async def is_disabled(self):
        return bool(self._elements and self._elements[0].get("disabled", False))

    async def is_enabled(self):
        return not await self.is_disabled()


class FakePage:
    """`matches` maps a SEL key to the elements its selector should find."""

    # Defaults to the home feed, NOT the inbox: "we still have to get there" is
    # the starting state these tests are about, and open_inbox leaves a page
    # that is already on /messages/ alone.
    def __init__(self, matches=None, url="https://www.meetup.com/"):
        self.url = url
        self.matches = dict(matches or {})
        self.clicked: list[str] = []
        self.typed: list[tuple[str, str]] = []
        self.visited: list[str] = []

    # Locators are looked up by the joined selector string, so map each SEL key
    # back to the key name to decide what to return.
    def _key_for(self, selector):
        for key in meetup.SEL:
            if meetup._sel(key) == selector:
                return key
        # A single candidate rather than the joined form. Code that tries a
        # key's selectors ONE AT A TIME — meetup._first_of, and everything
        # ordered by preference rather than by document order — asks for each
        # on its own, and a fake that only knows the joined string answers
        # every one of those with nothing.
        for key, candidates in meetup.SEL.items():
            if selector in candidates:
                return key
        return None

    def locator(self, selector):
        # Most lookups come through a SEL key. Some code builds a selector by
        # hand (a category tab's own data-testid), so fall back to the literal
        # string — otherwise those would silently match nothing.
        key = self._key_for(selector)
        elements = self.matches.get(key) if key else None
        if elements is None:
            elements = self.matches.get(selector, [])
        for element in elements:
            element.setdefault("_key", key or selector)
        return FakeLocator(elements)

    async def goto(self, url, **kwargs):
        self.visited.append(url)
        return None

    async def evaluate(self, script="", *args, **kwargs):
        """Two different evaluates reach this fake, and they want different
        shapes back: the scroll-height probe wants a number, the member reader
        wants a list of rows. Answering 800 to both made the reader hand an int
        to a for-loop."""
        if "username" in str(script):
            return list(self.matches.get("_members", []))
        return 800


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Strip the human pauses and the poll backoff — the flow, not the pacing."""
    async def _instant(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(meetup.asyncio, "sleep", _instant)
    monkeypatch.setattr(meetup.human, "think", _instant)
    monkeypatch.setattr(meetup.human, "pause", _instant)
    monkeypatch.setattr(meetup.human, "before_submit", _instant)
    monkeypatch.setattr(meetup, "_INBOX_WAIT_S", 0.2)


@pytest.fixture(autouse=True)
def stub_browser_bits(monkeypatch):
    """Nothing in these tests may touch a real browser or the overlay/pointer
    machinery, which both expect a live page."""
    clicks: list = []

    async def _dismiss(page, selectors):
        return 0

    async def _goto(page, url, **kwargs):
        page.visited.append(url)
        return True

    async def _type(page, selector, text, clear=False):
        page.typed.append((selector, text))
        # Typing into the composer fills it, exactly as the real one does.
        for element in page.matches.get("message_box", []):
            element["value"] = text
        return len(text)

    class _Pointer:
        def __init__(self, page):
            self.page = page

        async def click(self, locator):
            element = locator._elements[0] if locator._elements else {}
            clicks.append(element.get("_key") or element.get("name") or "?")
            hook = element.get("on_click")
            if hook:
                hook(self.page)
            return True

    monkeypatch.setattr(meetup.visit, "dismiss_overlays", _dismiss)
    monkeypatch.setattr(meetup.visit, "goto", _goto)
    monkeypatch.setattr(meetup.human, "type_or_fill", _type)
    monkeypatch.setattr(meetup, "Pointer", _Pointer)
    return clicks


def inbox_page(*, rows=1, name="Ada", composer=True, logged_in=True):
    """A page shaped like the inbox: N conversation rows and maybe a composer."""
    composer_box = {"value": "", "name": "composer"}

    def _open_thread(page):
        page.matches["message_box"] = [composer_box]
        page.matches["chat_thread"] = [{"name": "thread"}]

    matches = {
        "logged_in_marker": [{"name": "avatar"}] if logged_in else [],
        "chat_row": [
            {"name": f"row{i}", "visible": True,
             "children": {meetup._sel("chat_name"): [{"text": f"{name} {i}"}]},
             "on_click": _open_thread}
            for i in range(rows)
        ],
        "message_send": [{"name": "send"}],
        "message_box": [composer_box] if composer and rows == 0 else [],
    }
    page = FakePage(matches)
    page.composer = composer_box
    if not composer:
        # A thread that opens but never renders a composer.
        for row in matches["chat_row"]:
            row["on_click"] = lambda p: p.matches.__setitem__("chat_thread", [{}])
    return page


# ── open_inbox ───────────────────────────────────────────────────────────

def test_open_inbox_goes_to_the_messages_url():
    page = inbox_page()
    result = asyncio.run(meetup.open_inbox(page))
    assert result["ok"] is True
    assert page.visited == ["https://www.meetup.com/messages/"]


def test_open_inbox_refuses_when_logged_out():
    """The inbox is behind the login wall — a logged-out session lands on the
    login form, which has no rows and would otherwise read as an empty inbox."""
    page = inbox_page(logged_in=False)
    result = asyncio.run(meetup.open_inbox(page))
    assert result["ok"] is False
    assert "Not logged in" in result["detail"]


def test_open_inbox_reports_a_page_that_will_not_load(monkeypatch):
    async def _fail(page, url, **kwargs):
        return False

    monkeypatch.setattr(meetup.visit, "goto", _fail)
    result = asyncio.run(meetup.open_inbox(inbox_page()))
    assert result == {"ok": False, "detail": "Inbox would not load"}


# ── open_first_chat ──────────────────────────────────────────────────────

def test_opens_the_row_at_the_top_of_the_inbox(stub_browser_bits):
    page = inbox_page(rows=3, name="Ada")
    result = asyncio.run(meetup.open_first_chat(page))

    assert result["ok"] is True
    assert stub_browser_bits == ["chat_row"]     # exactly one row clicked
    assert result["name"] == "Ada 0"             # the top one, not row 1 or 2


def test_an_empty_inbox_is_reported_not_raised():
    page = inbox_page(rows=0)
    result = asyncio.run(meetup.open_first_chat(page))
    assert result["ok"] is False
    assert result["detail"] == "No conversations in the inbox"


def test_a_hidden_row_is_skipped_for_the_first_visible_one(stub_browser_bits):
    """The row selectors are comma-joined fallbacks, so the first DOM match can
    be an off-screen element from a candidate meant for another layout —
    clicking one of those silently does nothing."""
    page = inbox_page(rows=2)
    page.matches["chat_row"][0]["visible"] = False
    page.matches["chat_row"][1]["children"] = {
        meetup._sel("chat_name"): [{"text": "Visible one"}]}

    result = asyncio.run(meetup.open_first_chat(page))

    assert result["ok"] is True
    assert result["name"] == "Visible one"


def test_a_nameless_row_still_opens():
    page = inbox_page(rows=1)
    page.matches["chat_row"][0]["children"] = {}
    result = asyncio.run(meetup.open_first_chat(page))
    assert result["ok"] is True
    assert result["name"] == ""


def test_a_thread_that_never_renders_a_composer_is_a_failure():
    page = inbox_page(rows=1, composer=False)
    result = asyncio.run(meetup.open_first_chat(page))
    assert result["ok"] is False
    assert "no composer appeared" in result["detail"]


# ── send_in_open_chat ────────────────────────────────────────────────────

def test_types_the_text_and_clicks_send(stub_browser_bits):
    page = _clearing_page(rows=1)
    asyncio.run(meetup.open_first_chat(page))

    result = asyncio.run(meetup.send_in_open_chat(page, "  hello there  "))

    assert result["ok"] is True
    assert page.typed[-1][1] == "hello there"        # trimmed before sending
    assert stub_browser_bits[-1] == "message_send"


def test_click_send_off_writes_the_message_and_leaves_it(stub_browser_bits,
                                                         monkeypatch):
    """Settings → Click Send. Off, and everything happens except the last
    press: the text is typed and the composer is left for a person to read.

    Not a dry run — the message really is written, in the real composer, to the
    real member. That is the point: what this thing would say is only worth
    checking as the run would actually say it."""
    monkeypatch.setattr(meetup, "_clicking_send", lambda: False)
    page = _clearing_page(rows=1)
    asyncio.run(meetup.open_first_chat(page))

    result = asyncio.run(meetup.send_in_open_chat(page, "hello there"))

    assert page.typed[-1][1] == "hello there"     # written
    assert "message_send" not in stub_browser_bits   # but never pressed
    assert result["ok"] is True                   # not a failure
    assert result["sent"] is False                # and not a message either
    assert page.composer["value"] == "hello there"   # still sitting there


def test_click_send_on_is_the_default(stub_browser_bits):
    """The switch has to default to sending, or an upgrade quietly stops every
    campaign this app runs."""
    from backend import settings as app_settings

    assert app_settings.current()["click_send"] is True
    assert meetup._clicking_send() is True


def test_an_empty_message_is_refused_before_anything_is_typed(stub_browser_bits):
    page = inbox_page(rows=1)
    asyncio.run(meetup.open_first_chat(page))

    result = asyncio.run(meetup.send_in_open_chat(page, "   "))

    assert result["ok"] is False
    assert "empty" in result["detail"]
    assert "message_send" not in stub_browser_bits
    assert page.typed == []


def test_a_composer_that_still_holds_the_text_is_not_a_send():
    """A disabled or mis-targeted Send button swallows the click with no error.
    Meetup clears the box on success, so a box that still holds our text is the
    difference between "we clicked Send" and "the message went"."""
    page = inbox_page(rows=1)
    asyncio.run(meetup.open_first_chat(page))

    # The stub types into the box; nothing clears it, i.e. the send didn't take.
    result = asyncio.run(meetup.send_in_open_chat(page, "did not go"))

    assert result["ok"] is False
    assert "still holds the text" in result["detail"]


def test_send_waits_for_the_button_to_become_clickable(stub_browser_bits, monkeypatch):
    """The live Send starts data-disabled='true' with pointer-events off and
    flips only once React sees text. A click before the flip lands on nothing —
    the "typed but never sent" the operator hit."""
    monkeypatch.setattr(meetup, "_SUBMIT_ENABLE_S", 1.0)
    page = _clearing_page(rows=1)
    button = page.matches["message_send"][0]
    button["data-disabled"] = "true"        # disabled until typing flips it

    async def _type(_page, selector, text, clear=False):
        _page.typed.append((selector, text))
        for element in _page.matches.get("message_box", []):
            element["value"] = text
        button["data-disabled"] = "false"   # React enables Send once text lands
        return len(text)

    monkeypatch.setattr(meetup.human, "type_or_fill", _type)
    asyncio.run(meetup.open_first_chat(page))

    result = asyncio.run(meetup.send_in_open_chat(page, "hello"))

    assert result["ok"] is True
    assert stub_browser_bits[-1] == "message_send"


def test_a_send_button_stuck_disabled_is_reported(stub_browser_bits, monkeypatch):
    monkeypatch.setattr(meetup, "_SUBMIT_ENABLE_S", 0.3)
    page = inbox_page(rows=1)
    asyncio.run(meetup.open_first_chat(page))
    page.matches["message_send"][0]["data-disabled"] = "true"   # never flips

    result = asyncio.run(meetup.send_in_open_chat(page, "hello"))

    assert result["ok"] is False
    assert "never became clickable" in result["detail"]
    assert "message_send" not in stub_browser_bits


def test_the_live_send_button_anchors_are_covered():
    """From the rendered composer: aria-label='Send a message...' and a
    lucide-send-horizontal icon, on a type=button (never a bare submit)."""
    assert "button[aria-label='Send a message...']" in meetup.SEL["message_send"]
    assert any("lucide-send-horizontal" in s for s in meetup.SEL["message_send"])
    assert "button[type='submit']" not in meetup.SEL["message_send"]


def test_a_challenge_on_send_is_reported(monkeypatch):
    page = inbox_page(rows=1)
    asyncio.run(meetup.open_first_chat(page))

    async def _challenged(_page):
        return True

    monkeypatch.setattr(meetup, "hit_challenge", _challenged)
    result = asyncio.run(meetup.send_in_open_chat(page, "hello"))

    assert result["ok"] is False
    assert result["detail"] == "Challenged on send"


def test_no_composer_at_all_is_reported():
    page = FakePage({"logged_in_marker": [{"name": "avatar"}]})
    result = asyncio.run(meetup.send_in_open_chat(page, "hello"))
    assert result["ok"] is False
    assert result["detail"] == "No composer on this thread"


# ── The whole flow ───────────────────────────────────────────────────────

def _clearing_page(rows=1, name="Ada"):
    """An inbox whose composer clears on send — i.e. Meetup accepted it."""
    page = inbox_page(rows=rows, name=name)
    page.matches["message_send"][0]["on_click"] = (
        lambda p: p.composer.__setitem__("value", ""))
    return page


def test_the_flow_runs_inbox_then_chat_then_send(stub_browser_bits):
    page = _clearing_page(rows=2)
    result = asyncio.run(meetup.message_first_chat(page, "hello there"))

    assert result["ok"] is True
    assert page.visited == ["https://www.meetup.com/messages/"]
    assert stub_browser_bits == ["chat_row", "message_send"]
    assert "Ada 0" in result["detail"]


def test_the_flow_stops_at_the_inbox_when_logged_out(stub_browser_bits):
    page = inbox_page(logged_in=False)
    result = asyncio.run(meetup.message_first_chat(page, "hello"))

    assert result["ok"] is False
    assert result["stage"] == "inbox"
    assert stub_browser_bits == []              # nothing was clicked, nothing typed
    assert page.typed == []


def test_the_flow_stops_at_the_chat_when_the_inbox_is_empty(stub_browser_bits):
    page = inbox_page(rows=0)
    result = asyncio.run(meetup.message_first_chat(page, "hello"))

    assert result["ok"] is False
    assert result["stage"] == "chat"
    assert "message_send" not in stub_browser_bits


def test_the_flow_names_who_was_messaged():
    page = _clearing_page(rows=1, name="Grace")
    result = asyncio.run(meetup.message_first_chat(page, "hello"))
    assert result["detail"] == "Message sent to Grace 0"
    assert result["name"] == "Grace 0"


def test_an_empty_message_never_opens_the_inbox(stub_browser_bits):
    """Refused at the composer, after the thread is open — the inbox and chat
    steps are harmless reads, but nothing is typed and nothing is sent."""
    page = inbox_page(rows=1)
    result = asyncio.run(meetup.message_first_chat(page, ""))
    assert result["ok"] is False
    assert page.typed == []
    assert "message_send" not in stub_browser_bits
