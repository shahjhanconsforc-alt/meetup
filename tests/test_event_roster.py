"""One text file per event: its members' names and ids.

The members table answers "who have we ever seen"; a roster answers "who was on
this event", which is the unit the operator works in and the one thing a table
keyed by handle cannot give back.

MinIO is not running in a test, so the storage module's put/get/list are stubbed
and what is under test is the shape it writes and the wiring that calls it.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.automation import campaign
from services.storage import event_roster_storage as rosters
from services.storage import tsv


@pytest.fixture
def bucket(monkeypatch):
    """A MinIO stand-in: one dict of object name → bytes."""
    store: dict[str, bytes] = {}

    def _put(bucket, name, data, content_type=None):
        store[name] = data
        return {"ok": True}

    def _get(bucket, name):
        return store.get(name)

    def _list(bucket, prefix=""):
        return [{"name": n, "size": len(v), "at": 1}
                for n, v in store.items() if n.startswith(prefix)]

    def _remove(bucket, name):
        store.pop(name, None)

    monkeypatch.setattr(rosters.mc, "put_bytes", _put)
    monkeypatch.setattr(rosters.mc, "get_bytes", _get)
    monkeypatch.setattr(rosters.mc, "list_objects", _list)
    monkeypatch.setattr(rosters.mc, "remove_object", _remove)
    return store


# ── The file itself ──────────────────────────────────────────────────────

def test_the_file_is_named_after_the_event(bucket):
    rosters.save("Finding Joy in Everyday Life",
                 [{"username": "141880022", "name": "Colleen Canney"}])
    assert "rosters/finding-joy-in-everyday-life.txt" in bucket


def test_punctuation_and_emoji_do_not_reach_the_object_name():
    """An object name is a path. A title is whatever the host typed."""
    assert rosters.safe_name("SUAW + Coffee ☕️, East Bay (summer)") == \
        "suaw-coffee-east-bay-summer"


def test_it_holds_the_event_the_name_and_the_id(bucket):
    rosters.save("Book Club", [{"username": "10935445", "name": "Simi"},
                               {"username": "415067002", "name": "Vandana"}])
    rows = tsv.loads(bucket["rosters/book-club.txt"], rosters.COLUMNS)

    assert [r["name"] for r in rows] == ["Simi", "Vandana"]
    assert [r["username"] for r in rows] == ["10935445", "415067002"]
    assert {r["event"] for r in rows} == {"Book Club"}


def test_a_member_with_no_id_is_not_written(bucket):
    """A name with no id is somebody this app cannot message, store against or
    look up again — a file full of them would read as a record of people it
    has, which it is not."""
    rosters.save("Book Club", [{"username": "", "name": "Ghost"},
                               {"username": "10935445", "name": "Simi"}])
    rows = tsv.loads(bucket["rosters/book-club.txt"], rosters.COLUMNS)
    assert [r["name"] for r in rows] == ["Simi"]


def test_a_roster_with_nobody_identified_writes_nothing(bucket):
    result = rosters.save("Book Club", [{"username": "", "name": "Ghost"}])
    assert result["ok"] is False
    assert bucket == {}


def test_the_same_member_twice_is_written_once(bucket):
    rosters.save("Book Club", [{"username": "1", "name": "Simi"},
                               {"username": "1", "name": "Simi"}])
    rows = tsv.loads(bucket["rosters/book-club.txt"], rosters.COLUMNS)
    assert len(rows) == 1


def test_saving_again_replaces_rather_than_appends(bucket):
    """The file is "who this event has". An event whose list shrank must not
    keep the people who dropped out."""
    rosters.save("Book Club", [{"username": "1", "name": "Simi"},
                               {"username": "2", "name": "Vandana"}])
    rosters.save("Book Club", [{"username": "1", "name": "Simi"}])

    rows = tsv.loads(bucket["rosters/book-club.txt"], rosters.COLUMNS)
    assert [r["username"] for r in rows] == ["1"]


def test_a_name_with_a_tab_in_it_does_not_break_the_table(bucket):
    """Tab is the column separator, so a name carrying one would split a record
    across columns — the codec escapes it, and this is the proof."""
    rosters.save("Book Club", [{"username": "1", "name": "Simi\tR"}])
    rows = tsv.loads(bucket["rosters/book-club.txt"], rosters.COLUMNS)
    assert rows[0]["name"] == "Simi\tR"


# ── Reading them back ────────────────────────────────────────────────────

def test_the_listing_names_every_file(bucket):
    rosters.save("Book Club", [{"username": "1", "name": "Simi"}])
    rosters.save("Coffee Hour", [{"username": "2", "name": "Vandana"}])

    keys = {row["key"] for row in rosters.listing()}
    assert keys == {"book-club", "coffee-hour"}


def test_one_file_reads_back_as_its_event_and_members(bucket):
    rosters.save("Book Club", [{"username": "1", "name": "Simi"}])
    found = rosters.load("book-club")

    assert found["event"] == "Book Club"
    assert found["members"] == [{"username": "1", "name": "Simi"}]


def test_an_unknown_file_reads_back_empty(bucket):
    assert rosters.load("nothing-here")["members"] == []


def test_a_file_can_be_removed(bucket):
    rosters.save("Book Club", [{"username": "1", "name": "Simi"}])
    rosters.remove("book-club")
    assert bucket == {}


# ── The scraping side writing them ───────────────────────────────────────

def test_collecting_an_event_writes_its_roster(bucket, monkeypatch):
    """It is written where the members are collected, not only after a click
    pass — a roster whose cards DID carry ids never goes through one."""
    written = {}

    def _save(event, members, category=""):
        written["event"] = event
        written["category"] = category
        written["members"] = list(members)
        return {"ok": True, "detail": f"{len(members)} member(s) written"}

    monkeypatch.setattr(rosters, "save", _save)
    result = campaign.save_event_roster(
        "Finding Joy", [{"username": "1", "name": "Colleen"},
                        {"username": "", "name": "Ghost"}],
        category="Social Activities")

    assert result["ok"] is True
    assert written["event"] == "Finding Joy"
    assert written["category"] == "Social Activities"
    assert [m["name"] for m in written["members"]] == ["Colleen"]


def test_a_dead_minio_does_not_fail_the_run(bucket, monkeypatch):
    """The file is a convenience copy of what is already in the member store and
    the event — losing it must not lose the run that collected them."""
    def _boom(event, members):
        raise RuntimeError("MinIO is down")

    monkeypatch.setattr(rosters, "save", _boom)
    result = campaign.save_event_roster("Finding Joy",
                                        [{"username": "1", "name": "Colleen"}])
    assert result["ok"] is False


def test_nothing_is_written_for_a_roster_with_no_ids(bucket, monkeypatch):
    called = {"n": 0}

    def _save(event, members):
        called["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(rosters, "save", _save)
    campaign.save_event_roster("Finding Joy", [{"username": "", "name": "Ghost"}])
    assert called["n"] == 0


# ── The Data panel reading them ──────────────────────────────────────────

def test_the_panel_lists_and_opens_the_files(bucket):
    """What the Data panel draws: the list, then one file's members."""
    from fastapi.testclient import TestClient

    from backend.main import app

    rosters.save("Finding Joy", [{"username": "141880022", "name": "Colleen"}])

    with TestClient(app) as client:
        listed = client.get("/api/data/rosters").json()["rosters"]
        opened = client.get("/api/data/rosters/finding-joy").json()

    assert [row["key"] for row in listed] == ["finding-joy"]
    assert opened["event"] == "Finding Joy"
    assert opened["members"] == [{"username": "141880022", "name": "Colleen"}]
    assert opened["count"] == 1


def test_an_unknown_file_is_a_404(bucket):
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        assert client.get("/api/data/rosters/nothing-here").status_code == 404


def test_the_file_downloads_exactly_as_stored(bucket):
    """It is already a spreadsheet-shaped table — there is nothing to convert."""
    from fastapi.testclient import TestClient

    from backend.main import app

    rosters.save("Finding Joy", [{"username": "1", "name": "Colleen"}])

    with TestClient(app) as client:
        got = client.get("/api/data/rosters/finding-joy/download")

    assert got.content == bucket["rosters/finding-joy.txt"]
    assert "finding-joy.txt" in got.headers["content-disposition"]


def test_the_listing_can_carry_the_members_for_the_table(bucket):
    """The Data panel draws the names in the row itself, so the listing it asks
    for opens each file — a round-trip per event, and worth it only because
    that is what the table shows."""
    rosters.save("Finding Joy", [{"username": "1", "name": "Colleen"},
                                 {"username": "2", "name": "Aliya"}])

    row = rosters.listing(with_members=True)[0]

    assert row["event"] == "Finding Joy"
    assert row["count"] == 2
    assert [m["name"] for m in row["members"]] == ["Colleen", "Aliya"]


def test_the_plain_listing_opens_nothing(bucket, monkeypatch):
    """Anything that only needs to know the files exist must not pay for their
    contents."""
    rosters.save("Finding Joy", [{"username": "1", "name": "Colleen"}])
    opened = {"n": 0}
    original = rosters.mc.get_bytes

    def _counted(bucket_name, name):
        opened["n"] += 1
        return original(bucket_name, name)

    monkeypatch.setattr(rosters.mc, "get_bytes", _counted)
    rows = rosters.listing()

    assert [r["key"] for r in rows] == ["finding-joy"]
    assert opened["n"] == 0
    assert "members" not in rows[0]


def test_the_full_listing_route_is_what_the_panel_asks_for(bucket):
    from fastapi.testclient import TestClient

    from backend.main import app

    rosters.save("Finding Joy", [{"username": "141880022", "name": "Colleen"}])

    with TestClient(app) as client:
        full = client.get("/api/data/rosters?full=1").json()["rosters"]
        plain = client.get("/api/data/rosters").json()["rosters"]

    assert full[0]["members"] == [{"username": "141880022", "name": "Colleen"}]
    assert full[0]["count"] == 1
    assert "members" not in plain[0]


# ── The category the event was scraped under ─────────────────────────────
# Added after the first builds. The Data panel says where a roster came from,
# and the event store cannot answer it — that is cleared at every boot, while
# the roster file outlives everything.


def test_the_category_is_written_onto_every_line(bucket):
    """A column, not a header. The file is a flat table meant to be grepped and
    dropped into a spreadsheet, and a column you can sort by is worth more there
    than one fact hidden at the top."""
    rosters.save("MITRE ATT&CK", [{"username": "1", "name": "Colleen"},
                                  {"username": "2", "name": "Aliya"}],
                 category="Social Activities")

    rows = tsv.loads(bucket[rosters.object_name("MITRE ATT&CK")], rosters.COLUMNS)

    assert [r["category"] for r in rows] == ["Social Activities"] * 2


def test_the_category_comes_back_with_the_roster(bucket):
    rosters.save("MITRE ATT&CK", [{"username": "1", "name": "Colleen"}],
                 category="Social Activities")

    key = rosters.safe_name("MITRE ATT&CK")
    assert rosters.load(key)["category"] == "Social Activities"


def test_a_roster_written_before_the_column_still_loads(bucket):
    """tsv maps by the FILE'S OWN header, so an old three-column file reads back
    with the category simply absent — not with every field shifted one place
    along, which is what a positional format would have done."""
    old = tsv.dumps([{"event": "Old event", "username": "1", "name": "Colleen"}],
                    ("event", "username", "name"))
    bucket[rosters.object_name("Old event")] = old

    found = rosters.load("old-event")

    assert found["event"] == "Old event"
    assert found["category"] == ""                 # unknown, not wrong
    assert [m["name"] for m in found["members"]] == ["Colleen"]


def test_a_roster_with_no_category_writes_the_column_empty(bucket):
    """Every row keeps the same shape, so the file is still a rectangle."""
    rosters.save("No tab", [{"username": "1", "name": "Colleen"}])

    rows = tsv.loads(bucket[rosters.object_name("No tab")], rosters.COLUMNS)

    assert rows[0]["category"] == ""


def test_the_listing_carries_the_category(bucket):
    """It is what the Data panel draws under each event's name."""
    rosters.save("MITRE ATT&CK", [{"username": "1", "name": "Colleen"}],
                 category="Tech")

    row = rosters.listing(with_members=True)[0]

    assert row["category"] == "Tech"
