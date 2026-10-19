"""The inbox roster: listing who's there, and messaging the first from a session.

The chat list, the click on the first, the write and the send are exercised
against a fake page (test_first_chat.py). Here the focus is the roster —
naming the people in the inbox and showing it alongside the send status — and
the stored-session orchestration that makes it work for an SSO account.
"""
from __future__ import annotations

import asyncio

import pytest

from backend import accounts, config, sessions
from backend.automation import campaign, meetup
from tests.test_first_chat import FakePage, inbox_page


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def _instant(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(meetup.asyncio, "sleep", _instant)
    monkeypatch.setattr(meetup.human, "think", _instant)
    monkeypatch.setattr(meetup.human, "pause", _instant)
    monkeypatch.setattr(meetup.human, "before_submit", _instant)
    monkeypatch.setattr(meetup, "_INBOX_WAIT_S", 0.2)


# ── list_conversations ───────────────────────────────────────────────────

def test_lists_every_conversation_top_first():
    page = inbox_page(rows=3, name="Person")
    roster = asyncio.run(meetup.list_conversations(page))
    assert [c["name"] for c in roster] == ["Person 0", "Person 1", "Person 2"]


def test_an_empty_inbox_lists_nothing():
    assert asyncio.run(meetup.list_conversations(inbox_page(rows=0))) == []


def test_hidden_rows_are_skipped():
    page = inbox_page(rows=3, name="P")
    page.matches["chat_row"][1]["visible"] = False
    roster = asyncio.run(meetup.list_conversations(page))
    assert [c["name"] for c in roster] == ["P 0", "P 2"]


def test_the_li_and_a_pair_for_one_conversation_is_collapsed():
    """chat_row's fallbacks match both the <li> and its <a> — the same person,
    same name, back to back. Only consecutive repeats collapse."""
    page = FakePage({"chat_row": [
        {"name": "li", "visible": True,
         "children": {meetup._sel("chat_name"): [{"text": "Faizan"}]}},
        {"name": "a", "visible": True,
         "children": {meetup._sel("chat_name"): [{"text": "Faizan"}]}},
        {"name": "li2", "visible": True,
         "children": {meetup._sel("chat_name"): [{"text": "Ada"}]}},
    ]})
    roster = asyncio.run(meetup.list_conversations(page))
    assert [c["name"] for c in roster] == ["Faizan", "Ada"]


def test_the_limit_caps_the_roster():
    page = inbox_page(rows=10, name="P")
    roster = asyncio.run(meetup.list_conversations(page, limit=4))
    assert len(roster) == 4


def test_the_name_selector_covers_the_live_span():
    """Off the rendered inbox: the name is a <span class="ds2-m14 …">, distinct
    from the preview (.ds2-r14) and the timestamp (.ds2-r12)."""
    assert "span.ds2-m14" in meetup.SEL["chat_name"]


def test_the_composer_placeholder_selector_is_covered():
    assert any("Send a message" in s for s in meetup.SEL["message_box"])


# ── message_first_chat carries the roster ────────────────────────────────

@pytest.fixture
def wired_page(monkeypatch):
    """A fake inbox whose composer clears on send, with the browser bits stubbed."""
    clicks: list = []

    async def _dismiss(page, selectors):
        return 0

    async def _goto(page, url, **kwargs):
        page.visited.append(url)
        page.url = url
        return True

    async def _type(page, selector, text, clear=False):
        page.typed.append((selector, text))
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
            if element.get("href"):
                self.page.url = element["href"]
            return True

    monkeypatch.setattr(meetup.visit, "dismiss_overlays", _dismiss)
    monkeypatch.setattr(meetup.visit, "goto", _goto)
    monkeypatch.setattr(meetup.human, "type_or_fill", _type)
    monkeypatch.setattr(meetup, "Pointer", _Pointer)
    return clicks


def test_the_flow_returns_the_roster_it_messaged_into(wired_page):
    page = inbox_page(rows=3, name="Guest")
    page.url = "https://www.meetup.com/messages/"
    page.matches["message_send"][0]["on_click"] = (
        lambda p: p.composer.__setitem__("value", ""))

    result = asyncio.run(meetup.message_first_chat(page, "hello"))

    assert result["ok"] is True
    assert result["conversations"] == ["Guest 0", "Guest 1", "Guest 2"]
    assert result["name"] == "Guest 0"          # the one at the top got it


def test_the_roster_is_reported_even_when_the_send_fails(wired_page):
    """Opened the inbox, saw who was there, but the composer never cleared — the
    roster is still the useful half of the answer."""
    page = inbox_page(rows=2, name="Guest")
    page.url = "https://www.meetup.com/messages/"
    # No on_click that clears the box, so the send-verification fails.

    result = asyncio.run(meetup.message_first_chat(page, "hello"))

    assert result["ok"] is False
    assert result["conversations"] == ["Guest 0", "Guest 1"]


def test_a_logged_out_inbox_returns_an_empty_roster(wired_page):
    page = FakePage({}, url="https://www.meetup.com/")
    result = asyncio.run(meetup.message_first_chat(page, "hi"))
    assert result["ok"] is False
    assert result["stage"] == "inbox"
    assert result["conversations"] == []


# ── accounts.send_first_chat (stored session) ────────────────────────────

@pytest.fixture
def session_stub(monkeypatch):
    """Stub open_session and the DOM send; record what got called."""
    calls = {"opened": [], "sent": []}
    outcome = {
        "session": {"ok": True, "session": {"logged_in": True}, "detail": "opened"},
        "send": {"ok": True, "detail": "Message sent to Ada", "name": "Ada",
                 "conversations": ["Ada", "Ben"], "stage": "send"},
    }

    async def _open_session(account_id):
        calls["opened"].append(account_id)
        return outcome["session"]

    async def _current(profile=None):
        return object()

    async def _message(page, text, image=None):
        calls["sent"].append({"text": text, "image": image})
        return outcome["send"]

    monkeypatch.setattr(accounts, "open_session", _open_session)
    monkeypatch.setattr(accounts.browser, "current_page", _current)
    monkeypatch.setattr(accounts.meetup, "message_first_chat", _message)
    return calls, outcome


def test_send_first_chat_uses_the_stored_session_not_a_login(account, session_stub):
    """The account this exists for signed in via Google — a password login could
    never authenticate it, but its cookies are in the profile."""
    calls, _ = session_stub
    result = asyncio.run(accounts.send_first_chat(account["id"], "hello"))

    assert result["ok"] is True
    assert calls["opened"] == [account["id"]]
    assert calls["sent"] == [{"text": "hello", "image": None}]
    assert result["conversations"] == ["Ada", "Ben"]


def test_send_first_chat_refuses_without_a_stored_session(account, session_stub):
    calls, outcome = session_stub
    outcome["session"] = {"ok": False, "detail": "No stored session for this account"}

    result = asyncio.run(accounts.send_first_chat(account["id"], "hi"))

    assert result["ok"] is False
    assert result["conversations"] == []
    assert calls["sent"] == []              # never reached the inbox


def test_send_first_chat_needs_a_message(account, session_stub):
    calls, _ = session_stub
    result = asyncio.run(accounts.send_first_chat(account["id"], "   "))

    assert result["ok"] is False
    assert "Write the message first" in result["detail"]
    assert calls["opened"] == []           # doesn't even open on an empty message


def test_send_first_chat_reports_a_crash_rather_than_raising(account, session_stub,
                                                             monkeypatch):
    async def _boom(page, text, image=None):
        raise RuntimeError("page closed")

    monkeypatch.setattr(accounts.meetup, "message_first_chat", _boom)

    result = asyncio.run(accounts.send_first_chat(account["id"], "hi"))

    assert result["ok"] is False
    assert "RuntimeError: page closed" in result["detail"]
    assert result["conversations"] == []


def test_send_first_chat_on_unknown_account_raises(session_stub, monkeypatch):
    async def _open_session(account_id):
        raise KeyError(f"No account {account_id}")

    monkeypatch.setattr(accounts, "open_session", _open_session)
    with pytest.raises(KeyError):
        asyncio.run(accounts.send_first_chat(9999, "hi"))


# ── The HTTP route ───────────────────────────────────────────────────────

def test_route_returns_200_with_the_roster_even_on_a_failed_send(account, monkeypatch):
    """A failed send is a status to show next to the roster, not a 4xx that
    throws the roster away."""
    from fastapi.testclient import TestClient

    from backend.main import app

    async def _send(account_id, text, image=None):
        return {"ok": False, "detail": "Send did not go through",
                "conversations": ["Ada", "Ben"], "name": "Ada"}

    monkeypatch.setattr(accounts, "send_first_chat", _send)

    with TestClient(app) as client:
        res = client.post(f"/api/accounts/{account['id']}/message-first",
                          json={"text": "hello"})

    assert res.status_code == 200
    assert res.json()["conversations"] == ["Ada", "Ben"]
    assert res.json()["ok"] is False


def test_route_404s_on_an_unknown_account(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.main import app

    async def _send(account_id, text, image=None):
        raise KeyError(f"No account {account_id}")

    monkeypatch.setattr(accounts, "send_first_chat", _send)

    with TestClient(app) as client:
        res = client.post("/api/accounts/9999/message-first", json={"text": "hi"})

    assert res.status_code == 404
