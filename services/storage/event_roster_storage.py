"""One text file per event: who was on it, and their member ids.

The other stores in this package are tables of everything ever seen — every
member, every verdict — keyed by handle and merged across runs. This one is
deliberately the opposite shape: an EVENT's guest list, kept whole, under the
event's own name, because that is the unit the operator works in. "Who was at
the Writers Meetup on the 14th" is a question the members table cannot answer
without a join nobody wants to do by hand.

    rosters/finding-joy-in-everyday-life.txt

        event                          username    name
        Finding Joy in Everyday Life   141880022   Colleen Canney
        Finding Joy in Everyday Life   385029033   Anthony Morin

Tab-separated like the rest, so it opens in a spreadsheet, and the event name
repeats down the file so a `cat rosters/*.txt` is still a valid table.

Only members WITH an id are written. A row with no id is a name this app cannot
message, store against, or look up again — and a roster file half full of them
would read as a record of people it has, which it is not.

    save(event, members)   write/replace one event's roster
    listing()              every roster in the bucket
    load(name)             one roster back, as {event, members}
    remove(name)           delete one
"""
from __future__ import annotations

import re

from . import minio_client as mc
from . import tsv

# Under a prefix in the members bucket rather than a bucket of their own: they
# are members data, and a bucket per shape means another thing to create,
# migrate and get wrong on a fresh install.
PREFIX = "rosters/"
SUFFIX = ".txt"

# `category` is the Find tab the event was scraped under. Added after the first
# builds, and safe to add: tsv.loads maps by the FILE'S OWN header, so a roster
# written before this reads back with the column simply absent rather than
# shifting everything one place along.
COLUMNS = ("event", "category", "username", "name")

log = mc.log


def safe_name(event: str) -> str:
    """An event title as an object name — lowercase, dashes, no punctuation.

    Two events with the same title collapse onto one object ON PURPOSE: the
    same meetup runs weekly under the same name, and one file per occurrence
    would bury the operator in near-identical rosters.
    """
    text = re.sub(r"[^a-z0-9]+", "-", str(event or "").strip().lower())
    return (text.strip("-") or "event")[:80]


def object_name(event: str) -> str:
    return f"{PREFIX}{safe_name(event)}{SUFFIX}"


def _rows(event: str, members, category: str = "") -> list[dict]:
    """The lines to write: everyone who HAS an id, deduped, in list order."""
    seen, out = set(), []
    for member in members or []:
        handle = str((member or {}).get("username") or "").strip()
        if not handle or handle in seen:
            continue
        seen.add(handle)
        out.append({"event": event, "category": category, "username": handle,
                    "name": str((member or {}).get("name") or "").strip()})
    return out


def save(event: str, members, category: str = "") -> dict:
    """Write one event's roster. Returns {ok, object, event, members}.

    Replaces rather than merges: the file is "who this event has", and an event
    whose attendee list shrank should not keep the people who dropped out.

    `category` is the Find tab it was scraped under. On every line rather than in
    a header of its own, because the file is a flat table meant to be grepped and
    dropped into a spreadsheet — and a column you can sort by is worth more there
    than one fact hidden at the top.
    """
    title = str(event or "").strip() or "event"
    rows = _rows(title, members, str(category or "").strip())
    if not rows:
        return {"ok": False, "object": "", "event": title, "members": 0,
                "detail": "No members with an id — nothing to write"}

    name = object_name(title)
    mc.put_bytes(mc.BUCKET_MEMBERS, name, tsv.dumps(rows, COLUMNS),
                 content_type="text/tab-separated-values; charset=utf-8")
    log.info("roster %s: %d member(s)", name, len(rows))
    return {"ok": True, "object": name, "event": title, "members": len(rows),
            "detail": f"{len(rows)} member(s) written to {name}"}


def listing(with_members: bool = False) -> list[dict]:
    """Every stored roster: object name, key, size, when it was written.

    `with_members` opens each file and attaches its event title and members —
    which is a round-trip per event, and worth it only because the panel that
    asks for it shows the names in the table itself. Without the flag this is
    one list call, whatever the bucket holds.
    """
    out = []
    for item in mc.list_objects(mc.BUCKET_MEMBERS, PREFIX):
        name = item.get("name") or ""
        if not name.endswith(SUFFIX):
            continue
        row = {
            "object": name,
            "key": name[len(PREFIX):-len(SUFFIX)],
            "size": item.get("size") or 0,
            "at": item.get("at") or 0,
        }
        if with_members:
            found = load(name)
            # The title as it was typed, off the file; the key is the slug it
            # was filed under and is what a bad title would read as.
            row["event"] = found["event"] or row["key"]
            row["category"] = found.get("category") or ""
            row["members"] = found["members"]
            row["count"] = len(found["members"])
        out.append(row)
    return sorted(out, key=lambda row: row["at"], reverse=True)


def load(key: str) -> dict:
    """One roster back, as {event, category, members: [{username, name}]}.

    `category` is empty for a roster written before the column existed — those
    files are still perfectly readable, they just do not know the answer.
    """
    name = key if key.startswith(PREFIX) else f"{PREFIX}{key}{SUFFIX}"
    raw = mc.get_bytes(mc.BUCKET_MEMBERS, name)
    if raw is None:
        return {"event": "", "category": "", "members": [], "object": name}
    rows = tsv.loads(raw, COLUMNS)
    event = next((r.get("event") for r in rows if r.get("event")), "")
    category = next((r.get("category") for r in rows if r.get("category")), "")
    members = [{"username": r.get("username", ""), "name": r.get("name", "")}
               for r in rows if r.get("username")]
    return {"event": event, "category": category, "members": members,
            "object": name}


def raw(key: str) -> bytes | None:
    """The file exactly as stored, for a download."""
    name = key if key.startswith(PREFIX) else f"{PREFIX}{key}{SUFFIX}"
    return mc.get_bytes(mc.BUCKET_MEMBERS, name)


def remove(key: str) -> dict:
    name = key if key.startswith(PREFIX) else f"{PREFIX}{key}{SUFFIX}"
    mc.remove_object(mc.BUCKET_MEMBERS, name)
    log.info("roster %s removed", name)
    return {"ok": True, "object": name}


def count() -> int:
    return len(listing())
