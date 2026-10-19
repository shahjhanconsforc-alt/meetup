"""Storing what a category browse found.

Events live in store.json beside groups, not MinIO: they are a record of what
THIS install saw while browsing, they are small, and they must keep working with
MinIO down — which is exactly when you would be browsing to find targets.
"""
from __future__ import annotations

import asyncio

import pytest

from backend import db
from backend.automation import campaign


def row(event_id="315749481", urlname="pakistan-agile-education", **extra):
    return {"event_id": event_id, "urlname": urlname,
            "title": "Run Better Workshops with Miro",
            "group": "Pakistan Agile Education", "when": "2026-08-21T16:00:00+05:00",
            "attendees": 38, "rating": "4.6", "online": False, "going": True,
            "url": f"https://www.meetup.com/{urlname}/events/{event_id}/", **extra}


# ── The store ────────────────────────────────────────────────────────────

def test_events_are_stored_and_listed():
    result = db.add_events([row(), row(event_id="315992342", urlname="htb-lahore")])
    assert result["added"] == 2
    assert {e["event_id"] for e in db.list_events()} == {"315749481", "315992342"}


def test_the_same_event_seen_twice_is_updated_not_duplicated():
    """One event shows up under several categories — an AI meetup is both
    Technology and Career & Business — and a browse run twice must not double
    its own record."""
    db.add_events([row()])
    result = db.add_events([row(attendees=44)])

    assert result["added"] == 0
    assert result["updated"] == 1
    assert len(db.list_events()) == 1
    assert db.list_events()[0]["attendees"] == 44


def test_an_update_does_not_blank_a_field_the_new_row_lacks():
    """A card rendered without its rating must not erase the rating an earlier
    sighting captured."""
    db.add_events([row()])
    db.add_events([row(rating="", attendees=0)])

    stored = db.list_events()[0]
    assert stored["rating"] == "4.6"
    assert stored["attendees"] == 38


def test_a_row_without_an_event_id_is_kept_under_its_url():
    """`data-eventref` is missing on some cards and event_ref only parses ids out
    of /<group>/events/<id>/ URLs, so keying on the id alone dropped those rows
    on the floor — browsed, counted in the run's own total, then never stored.
    The URL identifies the event just as well."""
    card = row(event_id="", url="https://www.meetup.com/htb-lahore/events/315992342/")
    assert db.add_events([card])["added"] == 1
    assert db.list_events()[0]["url"].endswith("/events/315992342/")


def test_a_row_with_neither_an_id_nor_a_url_is_skipped():
    assert db.add_events([row(event_id="", url="")])["added"] == 0
    assert db.list_events() == []


def test_a_row_without_an_id_is_not_stored_twice():
    """Same event, same URL, no id either time: still one record."""
    db.add_events([row(event_id="")])
    result = db.add_events([row(event_id="", attendees=44)])

    assert result["added"] == 0
    assert len(db.list_events()) == 1


def test_an_event_keeps_every_category_it_was_found_under():
    """An AI meetup is both Technology and Career & Business. The single field
    was overwritten by whichever category was swept last, so the panel dropped
    it out of every earlier heading it belonged to."""
    db.add_events([row(category="Technology", category_key="652")])
    db.add_events([row(category="Career & Business", category_key="405")])

    stored = db.list_events()[0]
    assert stored["categories"] == ["Technology", "Career & Business"]
    assert stored["category"] == "Technology"          # the one it was found under first


def test_a_later_sighting_without_a_category_does_not_blank_the_one_stored():
    db.add_events([row(category="Technology")])
    db.add_events([row(category="")])

    stored = db.list_events()[0]
    assert stored["category"] == "Technology"
    assert stored["categories"] == ["Technology"]


def test_events_are_listed_newest_first():
    db.add_events([row(event_id="1"), row(event_id="2")])
    assert [e["event_id"] for e in db.list_events()] == ["2", "1"]


def test_the_listing_can_be_limited():
    db.add_events([row(event_id=str(n)) for n in range(10)])
    assert len(db.list_events(limit=3)) == 3
    assert len(db.list_events()) == 10


def test_events_can_be_cleared():
    db.add_events([row()])
    assert db.clear_events() == 1
    assert db.list_events() == []


def test_clearing_events_keeps_the_accounts(account):
    db.add_events([row()])
    db.clear_events()
    assert len(db.list_accounts()) == 1


# ── The browse stores what it saw ────────────────────────────────────────

@pytest.fixture
def swept(monkeypatch):
    """Stub the DOM sweep; the storing is what's under test."""
    outcome = {"visited": [
        {"key": "all", "name": "All events", "ok": True, "cards": 2,
         "events": [row(), row(event_id="315992342", urlname="htb-lahore")]},
        {"key": "652", "name": "Social Activities", "ok": True, "cards": 1,
         "events": [row(event_id="315874526", urlname="aws-sbg-lahore")]},
    ], "ok": True, "detail": "Visited 2/2 categories"}

    async def _sweep(page, **kwargs):
        return outcome

    async def _current(profile=None):
        return object()

    monkeypatch.setattr(campaign.meetup, "sweep_categories", _sweep)
    monkeypatch.setattr(campaign.browser, "current_page", _current)
    return outcome


def test_a_browse_stores_every_event_it_saw(swept):
    result = asyncio.run(campaign.browse_categories(None))

    assert result["stored"]["added"] == 3
    assert len(db.list_events()) == 3


def test_a_browse_turns_cards_into_scrapeable_groups(swept):
    """The group slug off each card is the valuable half — it is exactly what a
    scrape takes as a target, so browsing Find feeds the scraper directly
    instead of leaving a list to copy out by hand."""
    result = asyncio.run(campaign.browse_categories(None))

    assert sorted(result["groups_added"]) == [
        "aws-sbg-lahore", "htb-lahore", "pakistan-agile-education"]
    assert {g["urlname"] for g in db.list_groups()} == set(result["groups_added"])


def test_a_group_already_targeted_is_not_added_twice(swept):
    db.add_groups(["pakistan-agile-education"])
    result = asyncio.run(campaign.browse_categories(None))

    assert "pakistan-agile-education" not in result["groups_added"]
    assert len(db.list_groups()) == 3


def test_a_browse_that_found_nothing_stores_nothing(swept):
    for visit in swept["visited"]:
        visit["events"] = []

    result = asyncio.run(campaign.browse_categories(None))

    assert result["stored"]["added"] == 0
    assert db.list_events() == []
    assert db.list_groups() == []


def test_the_attendees_go_into_the_member_store(swept, monkeypatch):
    """The attendees are the point of opening events — they are the only people
    this bot can actually message, so they land where the campaign reads."""
    swept["visited"][0]["events"][0]["members"] = [
        {"username": "alice", "name": "Alice"}, {"username": "bob"}]
    swept["visited"][1]["events"][0]["members"] = [{"username": "alice"}]
    recorded = {}

    def _record(names):
        recorded["names"] = list(names)
        return {"added": list(names), "skipped": []}

    monkeypatch.setattr(campaign.db, "record_scraped_members", _record)

    result = asyncio.run(campaign.browse_categories(None))

    stored = {row["username"]: row["name"] for row in recorded["names"]}
    assert sorted(stored) == ["alice", "bob"]              # deduped across events
    # And they go in WITH their names. alice is on two events — named on one,
    # bare on the other — and the sighting that has a name has to be the one
    # that lands, or the dedup decides who is readable by card order.
    assert stored["alice"] == "Alice"
    assert result["members"] == 2


def test_a_member_store_outage_does_not_lose_the_browse(swept, monkeypatch):
    """MinIO is exactly what's down when you're browsing to find targets."""
    swept["visited"][0]["events"][0]["members"] = [{"username": "alice"}]

    def _boom(_names):
        raise RuntimeError("MinIO unreachable")

    monkeypatch.setattr(campaign.db, "record_scraped_members", _boom)

    result = asyncio.run(campaign.browse_categories(None))

    assert result["stored"]["added"] == 3          # the events still landed
    assert result["members"] == 1                  # and the count is honest


def test_each_stored_event_remembers_its_category(swept):
    """The panel groups by category, and a stored row has to know its own after
    a reload — the run's `visited` structure is long gone by then."""
    asyncio.run(campaign.browse_categories(None))

    by_id = {e["event_id"]: e for e in db.list_events()}
    assert by_id["315749481"]["category"] == "All events"
    assert by_id["315749481"]["category_key"] == "all"
    assert by_id["315874526"]["category"] == "Social Activities"


def test_the_detail_reports_what_was_kept(swept):
    result = asyncio.run(campaign.browse_categories(None))
    assert "3 new events" in result["detail"]
    assert "3 new groups" in result["detail"]


def test_a_boot_clears_the_events_the_last_session_left():
    """THE stale panel. The browse results are scratch — "what the last browse
    found" — and the last browse belonged to a session that is over. Kept across
    a restart they came back on screen as a panel full of events nobody in this
    session collected. The members they yielded live in the member store, so
    nothing of value goes with them.
    """
    from fastapi.testclient import TestClient

    from backend import db
    from backend.main import app

    db.add_events([{"event_id": "1", "url": "https://www.meetup.com/g/events/1/",
                    "title": "Left over from last time"}])
    db.add_groups(["a-group"])
    assert db.list_events()

    with TestClient(app):                 # entering the client runs the lifespan
        pass

    assert db.list_events() == []
    assert [g["urlname"] for g in db.list_groups()] == ["a-group"]   # kept
