"""The orchestration above meetup.py: log in, then reply in the first chat.

campaign.reply_to_first_chat is the layer that knows the ORDER — log in, open a
page on that account's profile, hand off to meetup.py — so that order and its
refusals are what these cover. The DOM walk itself is tested in
test_first_chat.py against a fake page.
"""
from __future__ import annotations

import asyncio

import pytest

from backend import accounts, sessions
from backend.automation import campaign


@pytest.fixture
def wiring(monkeypatch):
    """Stub the login, the browser and the DOM walk; record the call order."""
    calls: dict = {"login": [], "pages": [], "sent": []}
    outcome = {"login": {"ok": True, "detail": "Logged in"},
               "send": {"ok": True, "detail": "Message sent", "name": "Ada"}}

    async def _login(account_id):
        calls["login"].append(account_id)
        return outcome["login"]

    async def _open_page(*, profile=None, url=None):
        calls["pages"].append(profile)
        return object()

    async def _message(page, text, image=None):
        calls["sent"].append({"text": text, "image": image})
        return outcome["send"]

    monkeypatch.setattr(accounts, "login_account", _login)
    monkeypatch.setattr(campaign.browser, "open_page", _open_page)
    monkeypatch.setattr(campaign.meetup, "message_first_chat", _message)
    return calls, outcome


def test_logs_in_then_sends(account, wiring):
    calls, _ = wiring
    result = asyncio.run(campaign.reply_to_first_chat(
        account_id=account["id"], text="hello there"))

    assert result["ok"] is True
    assert calls["login"] == [account["id"]]
    assert calls["sent"] == [{"text": "hello there", "image": None}]


def test_opens_the_page_on_that_accounts_own_profile(account, wiring):
    """The wrong profile is the wrong inbox — and, since the profile directory
    *is* the session, the wrong account entirely."""
    calls, _ = wiring
    asyncio.run(campaign.reply_to_first_chat(account_id=account["id"], text="hi"))
    assert calls["pages"] == [sessions.profile_dir(account["id"])]


def test_a_reused_session_still_sends(account, wiring):
    """login_account returns `skipped` when the stored session was still good —
    that is the outcome we want, not a reason to stop."""
    calls, outcome = wiring
    outcome["login"] = {"skipped": True, "detail": "Already logged in"}

    result = asyncio.run(campaign.reply_to_first_chat(
        account_id=account["id"], text="hi"))

    assert result["ok"] is True
    assert len(calls["sent"]) == 1


def test_a_failed_login_stops_before_the_inbox(account, wiring):
    calls, outcome = wiring
    outcome["login"] = {"ok": False, "detail": "Meetup asked for a code"}

    result = asyncio.run(campaign.reply_to_first_chat(
        account_id=account["id"], text="hi"))

    assert result["ok"] is False
    assert result["stage"] == "login"
    assert "Meetup asked for a code" in result["detail"]
    assert calls["sent"] == []          # nothing was sent on a half-open session
    assert calls["pages"] == []


def test_an_empty_message_never_logs_in(account, wiring):
    """Refused before anything happens: opening a browser and signing an account
    in is real, observable traffic, and an empty message is a typo."""
    calls, _ = wiring
    result = asyncio.run(campaign.reply_to_first_chat(
        account_id=account["id"], text="   "))

    assert result["ok"] is False
    assert calls["login"] == []
    assert calls["pages"] == []


def test_login_can_be_skipped_for_an_already_open_session(account, wiring):
    calls, _ = wiring
    result = asyncio.run(campaign.reply_to_first_chat(
        account_id=account["id"], text="hi", login=False))

    assert result["ok"] is True
    assert calls["login"] == []
    assert len(calls["sent"]) == 1


def test_the_default_profile_needs_no_account(wiring):
    calls, _ = wiring
    result = asyncio.run(campaign.reply_to_first_chat(text="hi"))

    assert result["ok"] is True
    assert calls["login"] == []
    assert calls["pages"] == [None]


def test_an_image_is_passed_through(account, wiring):
    calls, _ = wiring
    asyncio.run(campaign.reply_to_first_chat(
        account_id=account["id"], text="hi", image="flyer.png"))
    assert calls["sent"][0]["image"] == "flyer.png"


def test_a_crash_mid_send_is_reported_not_raised(account, monkeypatch, wiring):
    """A run that dies here leaves the operator with a traceback and no idea
    whether the message went — the panel needs a result either way."""
    async def _boom(page, text, image=None):
        raise RuntimeError("page closed")

    monkeypatch.setattr(campaign.meetup, "message_first_chat", _boom)

    result = asyncio.run(campaign.reply_to_first_chat(
        account_id=account["id"], text="hi"))

    assert result["ok"] is False
    assert "RuntimeError: page closed" in result["detail"]


# ── The HTTP route ───────────────────────────────────────────────────────

def test_route_sends_and_returns_the_detail(account, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.main import app

    async def _reply(*, account_id, text, image, login):
        return {"ok": True, "detail": f"Message sent to Ada ({text})",
                "name": "Ada", "stage": "send"}

    monkeypatch.setattr(campaign, "reply_to_first_chat", _reply)

    with TestClient(app) as client:
        res = client.post("/api/automation/first-chat",
                          json={"account_id": account["id"], "text": "hello"})

    assert res.status_code == 200
    assert res.json()["detail"] == "Message sent to Ada (hello)"


def test_route_reports_a_refusal_as_a_conflict_not_a_success(account, monkeypatch):
    """A 200 with ok:false would show as a green "sent" in the panel for a
    message that never left."""
    from fastapi.testclient import TestClient

    from backend.main import app

    async def _reply(**kwargs):
        return {"ok": False, "detail": "No conversations in the inbox"}

    monkeypatch.setattr(campaign, "reply_to_first_chat", _reply)

    with TestClient(app) as client:
        res = client.post("/api/automation/first-chat",
                          json={"account_id": account["id"], "text": "hello"})

    assert res.status_code == 409
    assert res.json()["detail"] == "No conversations in the inbox"
