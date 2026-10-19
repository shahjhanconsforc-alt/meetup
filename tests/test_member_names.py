"""The display name, from the attendee card to the Users tab.

The member store has always had a `name` column, and the scraper has always read
names off the attendee cards — but the write path in between reduced a scrape's
rows to bare handles, so every name was dropped on the floor and the Campaign →
Users table showed a column of ids with nothing to read beside them.

Two halves are defended here: names surviving the trip INTO the store, and the
backfill that recovers the ones an older build lost — the event rosters kept
them all along, which is why re-scraping is not the only way back.

Storage is the in-memory bucket from conftest, so none of this touches the
operator's data.
"""
from __future__ import annotations

import pytest

from backend import db
from backend.automation import campaign
from services.storage import event_roster_storage as rosters


def stored() -> dict[str, str]:
    """handle -> display name, as the Users tab reads it."""
    return {row["username"]: row.get("name") or ""
            for row in db.list_scraped_members()}


# ── Into the store ───────────────────────────────────────────────────────


def test_a_scraped_member_keeps_their_name():
    db.record_scraped_members([{"username": "42380392", "name": "Tom"}])

    assert stored() == {"42380392": "Tom"}


def test_bare_handles_are_still_accepted():
    """The routes post a list of usernames — an operator pasting handles has no
    names to give, and that has to keep working."""
    db.record_scraped_members(["42380392", "464892004"])

    assert sorted(stored()) == ["42380392", "464892004"]


def test_a_later_nameless_sighting_does_not_blank_the_name():
    """A roster-only pass over someone already identified must not undo it —
    the same rule the store already keeps for a bio."""
    db.record_scraped_members([{"username": "2822366", "name": "Nick w"}])
    db.record_scraped_members(["2822366"])

    assert stored()["2822366"] == "Nick w"


def test_a_name_arriving_later_fills_a_blank_row():
    db.record_scraped_members(["2822366"])
    db.record_scraped_members([{"username": "2822366", "name": "Nick w"}])

    assert stored()["2822366"] == "Nick w"


# ── Deduping a run's sightings ───────────────────────────────────────────


def test_the_sighting_with_a_name_wins():
    """One member on two events — named on one card, bare on the other. Which
    one lands must not come down to card order."""
    rows = campaign._named([{"username": "alice"},
                            {"username": "alice", "name": "Alice"}])

    assert rows == [{"username": "alice", "name": "Alice"}]


def test_a_member_with_no_handle_is_dropped():
    """A co-host's card carries no profile link. There is nobody to store."""
    assert campaign._named([{"name": "Nobody"}, {"username": "", "name": "X"}]) == []


def test_the_at_sign_does_not_reach_the_store():
    assert campaign._named([{"username": "@tom", "name": "Tom"}]) == \
        [{"username": "tom", "name": "Tom"}]


# ── The backfill ─────────────────────────────────────────────────────────


@pytest.fixture
def roster():
    rosters.save("MITRE ATT&CK", [
        {"username": "42380392", "name": "Tom"},
        {"username": "464892004", "name": "Nick w"},
    ])


def test_the_rosters_name_the_members_that_have_none(roster):
    """The names were in MinIO all along — a store filled by an older build is
    a read away from being readable, not a re-scrape away."""
    db.record_scraped_members(["42380392", "464892004"])

    out = db.backfill_member_names()

    assert out["named"] == 2
    assert stored() == {"42380392": "Tom", "464892004": "Nick w"}


def test_a_name_already_on_a_row_is_never_overwritten(roster):
    """Including one the operator corrected by hand."""
    db.record_scraped_members([{"username": "42380392", "name": "Thomas R"}])

    db.backfill_member_names()

    assert stored()["42380392"] == "Thomas R"


def test_a_member_no_roster_knows_is_left_alone(roster):
    db.record_scraped_members(["220166034"])

    out = db.backfill_member_names()

    assert out["named"] == 0
    assert stored()["220166034"] == ""


def test_with_no_rosters_it_says_so_rather_than_failing():
    db.record_scraped_members(["220166034"])

    out = db.backfill_member_names()

    assert out == {"named": 0, "checked": 1, "rosters": 0}


def test_a_store_that_needs_nothing_reads_no_rosters(roster):
    """The early return: every row already has a name, so there is nothing to
    open a roster file for."""
    db.record_scraped_members([{"username": "42380392", "name": "Tom"}])

    assert db.backfill_member_names() == {"named": 0, "checked": 1, "rosters": 0}


def test_boot_fills_the_names_in(roster):
    """Not a button the operator has to know about. This is the whole reason the
    Data panel could show a name the Users tab could not — Data reads the roster
    files, Users reads the member store — and it should be closed by starting
    the app, not by finding the right control."""
    from fastapi.testclient import TestClient

    from backend.main import app

    db.record_scraped_members(["42380392", "464892004"])
    assert set(stored().values()) == {""}         # nameless to begin with

    with TestClient(app):                         # entering runs the lifespan
        pass

    assert stored() == {"42380392": "Tom", "464892004": "Nick w"}


# ── The category a member was scraped under ──────────────────────────────
# The Recipients table shows it and filters on it, so a campaign can be aimed at
# one category's people. Joined from the ROSTER files: the member store is keyed
# by handle and has no such column, and a member is not "in" a category anyway —
# they were on an event that sat under one.


def test_a_members_category_comes_off_the_roster(roster):
    db.record_scraped_members(["42380392"])
    rosters.save("MITRE ATT&CK", [{"username": "42380392", "name": "Tom"}],
                 category="Tech")

    assert db.member_categories()["42380392"] == ["Tech"]


def test_a_member_on_events_under_two_categories_has_both():
    """The same person turns up on events filed under different tabs, and
    "everyone under Tech" has to find them from either."""
    rosters.save("One", [{"username": "1", "name": "A"}], category="Tech")
    rosters.save("Two", [{"username": "1", "name": "A"}], category="Health")

    assert db.member_categories()["1"] == ["Health", "Tech"]   # sorted


def test_the_same_category_twice_is_listed_once():
    rosters.save("One", [{"username": "1", "name": "A"}], category="Tech")
    rosters.save("Two", [{"username": "1", "name": "A"}], category="Tech")

    assert db.member_categories()["1"] == ["Tech"]


def test_a_roster_with_no_category_contributes_nothing():
    """Written before the column existed. Its members are still members — they
    just have no answer to which category, and an empty string is not one."""
    rosters.save("Old", [{"username": "1", "name": "A"}])

    assert db.member_categories() == {}


def test_a_member_no_roster_covers_has_no_category(roster):
    """A handle uploaded by hand. Not an error, and not a blank category —
    simply absent from the map."""
    db.record_scraped_members(["220166034"])

    assert "220166034" not in db.member_categories()


def test_the_lookup_is_case_insensitive_on_the_handle():
    """Meetup hands the same member back as a numeric id in some places and a
    vanity slug in others, and the store dedupes case-insensitively."""
    rosters.save("One", [{"username": "AbC", "name": "A"}], category="Tech")

    assert db.member_categories()["abc"] == ["Tech"]


def test_a_storage_failure_answers_nothing_known(monkeypatch):
    """A Recipients table without the category column is still a usable
    Recipients table — losing the join must not empty the list."""
    def _boom(**kwargs):
        raise RuntimeError("MinIO unreachable")

    monkeypatch.setattr(rosters, "listing", _boom)

    assert db.member_categories() == {}
