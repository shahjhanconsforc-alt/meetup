"""The roster filling in on screen WHILE an event is being walked.

The Scraping panel polls the run and draws `ctl.meta["members"]`, so what that
key holds mid-walk is the whole of what the operator sees. Forty page loads with
one row on screen until the end is not something anybody can watch, and it also
hides a run that has quietly stopped finding anyone.

These check the chain the panel actually reads: harvest_attendees publishes each
member as their tab closes, event_members puts that list on the run, and a
SNAPSHOT taken mid-walk — the same shallow dict() the poll gets — carries every
member found so far.
"""
from __future__ import annotations

import asyncio

import pytest

from backend import control
from backend.automation import campaign, meetup
from tests.test_categories import ListPage, list_click_stubs  # noqa: F401


class Watcher(control.Run):
    """A real Run that records what each poll WOULD have seen.

    snapshot() is what /api/automation/tasks/{id} returns, so recording it is
    recording the panel's input — not an approximation of it.
    """

    def __init__(self):
        super().__init__("sweep")
        self.seen: list[int] = []

    async def checkpoint(self, step: str = "") -> bool:
        self.seen.append(len(self.snapshot()["meta"].get("members") or []))
        return await super().checkpoint(step)


@pytest.fixture
def walk_stubs(monkeypatch, list_click_stubs):  # noqa: F811
    """event_members' surroundings — the browser, the store, the pacing."""
    async def _zero(*args, **kwargs):
        return 0.0

    async def _current(profile=None):
        return walk_stubs.page

    async def _dismiss(page, selectors):
        return 0

    async def _open_attendees(page, url):
        return True

    monkeypatch.setattr(meetup.asyncio, "sleep", _zero)
    monkeypatch.setattr(meetup.human, "think", _zero)
    monkeypatch.setattr(meetup.human, "pause", _zero)
    monkeypatch.setattr(campaign.browser, "current_page", _current)
    monkeypatch.setattr(campaign.meetup.visit, "dismiss_overlays", _dismiss)
    monkeypatch.setattr(campaign.meetup, "open_attendees", _open_attendees)
    monkeypatch.setattr(campaign, "save_event_roster", lambda *a, **k: {"ok": True})
    return walk_stubs


EVENT = "https://www.meetup.com/a-group/events/315749481/"


def test_the_walk_publishes_every_member_as_it_goes(list_click_stubs):  # noqa: F811
    """Per member, not per screenful: the rolling window has no batches, and
    people arrive one at a time as their tab closes."""
    page = ListPage([f"P{n}" for n in range(12)])
    sizes = []

    rows = asyncio.run(meetup.harvest_attendees(
        page, width=5, on_found=lambda r: sizes.append(len(r))))

    assert len(rows) == 12
    assert sizes[0] == 1                       # the first one to land
    assert sizes[-1] == 12                     # and every one after it
    assert sizes == sorted(sizes)              # only ever grows


def test_a_poll_mid_walk_sees_everyone_found_so_far(walk_stubs, monkeypatch):
    """THE thing the panel reads. A snapshot taken while the walk is going has
    to carry the roster so far — one member on screen for the length of a
    forty-person event is the symptom this rules out."""
    walk_stubs.page = ListPage([f"P{n}" for n in range(12)])
    ctl = Watcher()

    result = asyncio.run(campaign.event_members(url=EVENT, ctl=ctl))

    assert result["count"] == 12
    # Not just "ended up with 12" — it GREW, which is what makes it watchable.
    assert max(ctl.seen) > 1
    assert ctl.seen == sorted(ctl.seen)
    assert ctl.seen[-1] > ctl.seen[0]


def test_the_published_rows_carry_names_and_ids(walk_stubs):
    """The panel draws both columns. A row with an id and no name reads as a
    bare number, which is what the Users tab looked like for months."""
    walk_stubs.page = ListPage(["Colleen", "Aliya", "Bunny"])
    ctl = Watcher()

    asyncio.run(campaign.event_members(url=EVENT, ctl=ctl))

    rows = ctl.meta["members"]
    assert [r["name"] for r in rows] == ["Colleen", "Aliya", "Bunny"]
    assert all(r["username"] for r in rows)


def test_the_rows_are_cleared_when_the_next_event_starts(walk_stubs):
    """A sweep walks event after event. The second one's people must not stack
    under the first's — the panel keys that off event_url changing."""
    walk_stubs.page = ListPage(["A", "B"])
    ctl = Watcher()
    asyncio.run(campaign.event_members(url=EVENT, ctl=ctl))
    assert len(ctl.meta["members"]) == 2

    other = "https://www.meetup.com/a-group/events/999/"
    walk_stubs.page = ListPage(["C"])
    asyncio.run(campaign.event_members(url=other, ctl=ctl))

    assert ctl.meta["event_url"] == other
    assert [r["name"] for r in ctl.meta["members"]] == ["C"]


def test_the_list_the_run_holds_is_the_one_the_snapshot_hands_out(walk_stubs):
    """snapshot() shallow-copies meta, so the published list is the live one —
    which is what makes a poll taken mid-walk current rather than stale."""
    walk_stubs.page = ListPage([f"P{n}" for n in range(6)])
    ctl = Watcher()

    asyncio.run(campaign.event_members(url=EVENT, ctl=ctl))

    assert ctl.snapshot()["meta"]["members"] is ctl.meta["members"]
