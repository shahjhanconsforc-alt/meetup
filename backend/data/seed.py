"""Seed a fresh install with something to look at.

    python -m backend.data.seed              # groups + drafts + sample members
    python -m backend.data.seed --clear      # remove what this script added

Exists because an empty install is hard to evaluate: every panel is blank and
the campaign wizard can't be walked through end to end. The sample members are obviously fake handles with
bios written to exercise the lexicon's different branches — they are NOT scraped
data, and the seed prefix makes them easy to remove again.
"""
from __future__ import annotations

import sys

from backend import activity, db

# A prefix nothing real can collide with, so --clear can find exactly what this
# script added and nothing else.
SEED_PREFIX = "sample-"

GROUPS = [
    "python-developers-london",
    "berlin-hiking-club",
    "nyc-startup-founders",
]

DRAFTS = [
    {"label": "msg1", "text": (
        "Hi {first_name} — saw you're in {group} too. I'm putting together a "
        "small meetup for people into {category} next month. Want the details?"
    )},
    {"label": "msg2", "text": (
        "Hey {first_name}! Fellow {group} member here. We're running a casual "
        "get-together in {city} soon — thought it might be your kind of thing."
    )},
    {"label": "msg3", "text": (
        "Hi {name}, I help organise events around {category} in {city}. "
        "Happy to send an invite over if you're interested."
    )},
]

# Bios chosen to hit different parts of the lexicon: an occupation, a sport, a
# location, a hashtag, an emoji-only one, and one with nothing to classify.
MEMBERS = [
    {"username": f"{SEED_PREFIX}alice", "name": "Alice Nguyen",
     "location": "London, UK", "groups": "python-developers-london",
     "bio": "Backend engineer, mostly Python and Postgres. Climbing on weekends."},
    {"username": f"{SEED_PREFIX}ben", "name": "Ben Okafor",
     "location": "Berlin, Germany", "groups": "berlin-hiking-club",
     "bio": "Hiking every weekend 🥾 #outdoors — trail runner, ex-marathoner."},
    {"username": f"{SEED_PREFIX}chi", "name": "Chi Zhang",
     "location": "New York, NY", "groups": "nyc-startup-founders",
     "bio": "Founder @ a seed-stage fintech. Previously PM at a bank. Investing, "
            "coffee, and very long walks."},
    {"username": f"{SEED_PREFIX}dee", "name": "Dee Marsh",
     "location": "London, UK", "groups": "python-developers-london",
     "bio": "Photographer and makeup artist 📷 — portraits mostly."},
    {"username": f"{SEED_PREFIX}eze", "name": "Eze Kalu",
     "location": "Berlin, Germany", "groups": "berlin-hiking-club",
     "bio": "🎧🎸"},
    {"username": f"{SEED_PREFIX}fay", "name": "Fay Ortiz",
     "location": "", "groups": "nyc-startup-founders",
     "bio": ""},
]


def seed() -> dict:
    groups = db.add_groups(GROUPS)
    drafts = db.save_message_drafts(DRAFTS)
    members = db.add_member_bios(MEMBERS)
    result = {
        "groups": len(groups.get("added", [])),
        "drafts": len(drafts),
        "members": len(members.get("added", [])) + len(members.get("updated", [])),
    }
    activity.log("data", f"Seeded {result}", "ok")
    return result


def clear() -> dict:
    removed = 0
    for member in MEMBERS:
        if db.remove_member(member["username"]).get("removed"):
            removed += 1
    for urlname in GROUPS:
        db.remove_group(urlname)
    activity.log("data", f"Removed {removed} seeded member(s)", "ok")
    return {"members_removed": removed, "groups_removed": len(GROUPS)}


if __name__ == "__main__":
    try:
        if "--clear" in sys.argv:
            print(clear())
        else:
            print(seed())
    except Exception as exc:  # noqa: BLE001
        # Almost always MinIO being down. Say which, rather than a traceback that
        # buries it — this script is the first thing a new install runs.
        print(f"Seed failed: {type(exc).__name__}: {exc}")
        print("Is MinIO running? Try: docker compose up -d minio")
        raise SystemExit(1)
