"""Tiny JSON-file database.

Loaded once into memory, written through to disk on every mutation. Fine for a
single-process agent; swap for SQLite if you ever need concurrent writers.

Two kinds of state live behind this module and they are NOT the same thing:

  * Local, private, per-install — accounts, runs, settings, panel prefs. These
    really are in store.json.
  * The scraped member/category tables — these live in MinIO and store.json
    holds none of them. The functions below are thin pass-throughs so callers
    never import the storage modules directly and the source of truth can move
    without touching a route.
"""
import copy
import json
import threading
from datetime import datetime, timezone
from typing import Any

from backend import config

_LOCK = threading.Lock()
_EMPTY: dict[str, Any] = {"records": [], "runs": [], "accounts": [], "settings": {},
                          "message_drafts": [], "seq": 0}
_cache: dict[str, Any] | None = None

MAX_RUNS = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    # deepcopy, not dict()/**: _EMPTY's values are mutable lists, and a shallow
    # copy hands the store the module constant's own list. Every append would
    # then land in _EMPTY — so a store.json missing one of these keys inherits
    # whatever the last one put there.
    try:
        with config.DB_FILE.open("r", encoding="utf-8") as handle:
            _cache = {**copy.deepcopy(_EMPTY), **json.load(handle)}
    except (FileNotFoundError, json.JSONDecodeError):
        _cache = copy.deepcopy(_EMPTY)
        _save()
    return _cache


def _save() -> None:
    config.DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.DB_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(_cache, handle, indent=2, default=str)
    tmp.replace(config.DB_FILE)  # atomic — never leaves a half-written store


def insert(data: dict) -> dict:
    with _LOCK:
        store = _load()
        store["seq"] += 1
        record = {"id": store["seq"], "created_at": _now(), **data}
        store["records"].append(record)
        _save()
        return record


# ─── Members (MinIO-backed) ──────────────────────────────────────────────
# A "member" is one Meetup profile the scraper has seen: a handle (Meetup's
# member id or vanity slug), whatever bio text their profile carried, and the
# groups they were found in.


def _as_member_rows(names) -> list[dict]:
    """Accept either bare handles or {username, name} rows, and yield rows.

    Both shapes are real callers. A route posting a list of usernames has only
    the handle; a scrape that walked an attendee list read the NAME off every
    card and had been throwing it away here — which is why the Users tab showed
    a column of bare ids. Anything else on the row (a bio, a location) rides
    along too; the store keeps the columns it knows and drops the rest.
    """
    rows = []
    for entry in (names or []):
        if isinstance(entry, dict):
            handle = str(entry.get("username") or entry.get("handle") or "").strip()
            if handle:
                rows.append({**entry, "username": handle})
            continue
        handle = str(entry or "").strip()
        if handle:
            rows.append({"username": handle})
    return rows


def add_unique_members(names) -> dict:
    """Store members in MinIO (the source of truth), skipping duplicates.

    Takes handles or {username, name} rows. Returns {"added": [names],
    "skipped": [names]} — the actual new and already-present handles,
    case-insensitive.

    There is no bare-handle object: the bucket holds two tables, and a handle
    with no bio yet is simply a member-store row carrying the "unknown"
    sentinel. So this adds to the member store and reports which handles were new.
    """
    from services.storage import meetup_member_storage as members

    result = members.add(_as_member_rows(names))
    return {"added": result["added"], "skipped": result["updated"]}


def record_scraped_members(names) -> dict:
    """Persist a scrape's collected members to the member store.

    Every scrape from the Scraping panel lands its handles here. A handle
    collected without a bio is stored with bio "unknown"; a real bio already on
    the row is never downgraded (so a fast roster-only pass over a group can't
    wipe a bio an earlier profile visit read). The display name is protected the
    same way — passing a row with no name never blanks one already stored.

    This store keeps EVERY handle ever scraped. The two
    tables overlap by design: this one is the scrape's record, the other is the
    verdict on it.
    """
    return add_unique_members(names)


def backfill_member_names() -> dict:
    """Give nameless stored members the name their event roster already knows.

    Every roster file in MinIO holds {event, username, name} — the names were
    read off the attendee cards all along. What dropped them was the member
    store's own write path, which reduced a scrape's rows to bare handles, so a
    store filled before that fix is a column of ids with nothing to read beside
    them.

    Reads only the rosters, and only writes members that are missing a name — a
    name already on a row is never touched, including one the operator has
    corrected by hand. Returns {"named", "checked", "rosters"}.
    """
    from services.storage import event_roster_storage as rosters
    from services.storage import meetup_member_storage as members

    stored = members.load_all()
    missing = {r["username"].lower() for r in stored
               if r.get("username") and not str(r.get("name") or "").strip()}
    if not missing:
        return {"named": 0, "checked": len(stored), "rosters": 0}

    found: dict[str, str] = {}
    files = rosters.listing()
    for row in files:
        for member in rosters.load(row["object"])["members"]:
            handle = str(member.get("username") or "").strip().lstrip("@")
            name = str(member.get("name") or "").strip()
            key = handle.lower()
            if handle and name and key in missing and key not in found:
                found[key] = name

    if not found:
        return {"named": 0, "checked": len(stored), "rosters": len(files)}

    # Written back through the ordinary upsert, so every rule the store already
    # enforces — never downgrade a bio, merge the groups — still applies.
    by_key = {r["username"].lower(): r["username"] for r in stored
              if r.get("username")}
    members.add([{"username": by_key[key], "name": name}
                 for key, name in found.items() if key in by_key])
    return {"named": len(found), "checked": len(stored), "rosters": len(files)}


def list_members() -> list[str]:
    """Every stored handle, from the member store — the scrape's record of what
    it has seen."""
    from services.storage import meetup_member_storage as members

    return [r["username"] for r in members.load_all() if r.get("username")]


def remove_member(name: str) -> dict:
    """Delete one handle from the member store."""
    from services.storage import meetup_member_storage as members

    return members.remove(name)


def add_member_bios(records: list[dict]) -> dict:
    """Upsert {username, bio, groups, location} rows by username. Returns
    {"added", "updated", "total"}."""
    from services.storage import meetup_member_storage as members

    return members.add(records)


def list_member_bios() -> list[dict]:
    """Every stored member row, from MinIO's members.txt."""
    from services.storage import meetup_member_storage as members

    return members.load_all()


def member_categories() -> dict[str, list[str]]:
    """handle (lowercased) -> the Find categories that member was scraped under.

    Joined from the ROSTER files, because the member store has no such column
    and should not grow one: a member is not "in" a category, they were on an
    event that sat under one — and the same person turns up on events under
    several. The list is what makes "everyone under Tech" answerable.

    Reads every roster, so it is its own call rather than folded into
    list_scraped_members(): a campaign picking its targets does not need this,
    and paying a bucket walk on every send would be a poor trade for a column.

    A storage failure answers "nothing known", because a Recipients table
    without the category column is still a usable Recipients table.
    """
    from services.storage import event_roster_storage as rosters

    out: dict[str, list[str]] = {}
    try:
        for roster in rosters.listing(with_members=True):
            category = str(roster.get("category") or "").strip()
            if not category:
                continue
            for member in roster.get("members") or []:
                handle = str(member.get("username") or "").strip().lstrip("@")
                if not handle:
                    continue
                seen = out.setdefault(handle.lower(), [])
                if category not in seen:
                    seen.append(category)
    except Exception as exc:  # noqa: BLE001 — MinIO down must not empty the table
        from backend import activity

        activity.log("data", f"Could not read the categories: "
                             f"{type(exc).__name__}", "warn")
        return {}
    return {handle: sorted(names) for handle, names in out.items()}


def list_scraped_members() -> list[dict]:
    """Every member this bot scraped off Meetup, with whatever is known of them.

    One table now: the member store is the whole answer. It used to be joined
    against a verdict table written by a categorisation pipeline; that feature
    is gone, and with it the join, the second MinIO read on every listing, and
    the columns nothing rendered any more.
    """
    from services.storage import meetup_member_storage as members

    out: list[dict] = []
    for row in members.load_all():
        name = str(row.get("username") or "").strip().lstrip("@")
        if not name:
            continue
        try:
            used = max(0, int(row.get("used_count") or 0))
        except (TypeError, ValueError):
            used = 0
        out.append({
            "username": name,
            "name": row.get("name") or "",
            "bio": row.get("bio") or "",
            "location": row.get("location") or "",
            "groups": row.get("groups") or "",
            "used_count": used,
        })
    return out


def bump_member_uses(names: list[str]) -> dict:
    """+1 to each handle's `used_count` — called when a campaign stages/sends."""
    from services.storage import meetup_member_storage as members

    rest = members.bump_uses(names)
    return {
        "bumped": rest.get("bumped", 0),
        "added": rest.get("added", 0),
        "total": rest.get("total", 0),
    }


# ─── Groups (local) ──────────────────────────────────────────────────────
# The Meetup groups a scrape targets. Small, install-specific and not worth a
# MinIO round-trip, so unlike members these really do live in store.json.


# ─── The Find page's category strip ──────────────────────────────────────
# Saved so a roll does not have to re-read the page to know what it may roll.
# The strip is the same twenty tabs every time; reading it per roll is a page
# load and a wait for a list that has not changed since the last one.


def save_find_categories(rows: list[dict]) -> dict:
    """Replace the saved strip with what was just read off /find/.

    Replaced rather than merged: this is a snapshot of a page, so a tab Meetup
    has dropped should disappear here too rather than linger as a key that no
    longer clicks anything.
    """
    with _LOCK:
        store = _load()
        clean = []
        seen = set()
        for row in rows or []:
            key = str((row or {}).get("key") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            clean.append({"key": key,
                          "name": str(row.get("name") or key).strip()})
        store["find_categories"] = clean
        store["find_categories_at"] = _now()
        _save()
        return {"saved": len(clean)}


def list_find_categories() -> list[dict]:
    return list(_load().get("find_categories") or [])


def list_groups() -> list[dict]:
    return list(_load().get("groups") or [])


def add_groups(urlnames: list[str]) -> dict:
    """Add group urlnames (the slug in meetup.com/<urlname>/), deduped."""
    with _LOCK:
        store = _load()
        groups = list(store.get("groups") or [])
        have = {g["urlname"].lower() for g in groups}
        added = []
        for raw in urlnames or []:
            slug = _group_slug(raw)
            if not slug or slug.lower() in have:
                continue
            have.add(slug.lower())
            groups.append({"urlname": slug, "added_at": _now(), "scraped": 0})
            added.append(slug)
        store["groups"] = groups
        _save()
        return {"added": added, "total": len(groups)}


def _group_slug(raw: str) -> str:
    """Accept a bare urlname or a full group URL and return the urlname.

    Operators paste whichever they have in front of them; making the API take
    only one of the two forms just moves the parsing into the browser.
    """
    text = str(raw or "").strip().strip("/")
    if not text:
        return ""
    if "meetup.com" in text.lower():
        parts = [p for p in text.split("/") if p]
        for i, part in enumerate(parts):
            if "meetup.com" in part.lower() and i + 1 < len(parts):
                return parts[i + 1]
        return ""
    return text.split("/")[0]


# ─── Events discovered on the Find page ──────────────────────────────────
# What a category browse saw. Local like groups, not MinIO: they are a record of
# what THIS install found while browsing, they are small, and they must keep
# working with MinIO down — which is exactly when you would be browsing to find
# targets in the first place.

MAX_EVENTS = 2000


def _event_key(row: dict) -> str:
    """What identifies one event across sightings.

    The id when the card carries one, the URL when it does not. The URL fallback
    is not a nicety: `data-eventref` is missing on some cards and event_ref only
    parses ids out of /<group>/events/<id>/ URLs, so keying on the id alone
    dropped every one of those rows on the floor — browsed, counted in the run's
    own total, and then never stored.
    """
    key = str(row.get("event_id") or "").strip()
    if key:
        return key
    url = str(row.get("url") or "").split("?")[0].strip().rstrip("/")
    return url


def _merge_categories(entry: dict, row: dict) -> list[str]:
    """Every category name this event has been found under, in the order it was
    found under them.

    A list, not a single name, because an event genuinely belongs to several (an
    AI meetup is both Technology and Career & Business) and the old single field
    was simply overwritten by whichever category was swept last — so the panel
    grouped by that one and every earlier heading it should have appeared under
    lost it.
    """
    names = list(entry.get("categories") or [])
    for name in [entry.get("category"), row.get("category")]:
        name = str(name or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def add_events(rows: list[dict]) -> dict:
    """Upsert event cards, keyed by event id. Returns {added, updated, total}.

    Upsert rather than append: the same event shows up under several categories,
    and a browse run twice should not double its own record.
    """
    with _LOCK:
        store = _load()
        events = list(store.get("events") or [])
        by_id = {}
        for event in events:
            key = _event_key(event)
            if key:
                by_id[key] = event
        added = updated = 0
        for row in rows or []:
            key = _event_key(row)
            if not key:
                continue
            if key in by_id:
                entry = by_id[key]
                names = _merge_categories(entry, row)
                entry.update({k: v for k, v in row.items() if v not in ("", 0)})
                # After the update, so a later sighting cannot narrow an event
                # back down to the one category it was last seen under.
                if names:
                    entry["categories"] = names
                    entry["category"] = names[0]
                entry["seen_at"] = _now()
                updated += 1
            else:
                entry = {**row, "event_id": str(row.get("event_id") or key),
                         "found_at": _now(), "seen_at": _now()}
                entry["categories"] = _merge_categories({}, row)
                events.append(entry)
                by_id[key] = entry
                added += 1
        # Newest kept. A browse is a snapshot of what is on soon, so the oldest
        # rows are the least useful ones to hold on to.
        store["events"] = events[-MAX_EVENTS:]
        _save()
        return {"added": added, "updated": updated, "total": len(store["events"])}


def list_events(limit: int = 0) -> list[dict]:
    """Stored events, newest first. `limit <= 0` returns every one."""
    events = list(reversed(_load().get("events") or []))
    return events[:limit] if limit and limit > 0 else events


def clear_events() -> int:
    with _LOCK:
        store = _load()
        count = len(store.get("events") or [])
        store["events"] = []
        _save()
        return count


def mark_group_scraped(urlname: str, found: int) -> None:
    with _LOCK:
        store = _load()
        for group in store.get("groups") or []:
            if group["urlname"].lower() == str(urlname).lower():
                group["scraped"] = int(found)
                group["last_scraped"] = _now()
                _save()
                return


def remove_group(urlname: str) -> bool:
    with _LOCK:
        store = _load()
        groups = list(store.get("groups") or [])
        kept = [g for g in groups if g["urlname"].lower() != str(urlname).lower()]
        changed = len(kept) != len(groups)
        if changed:
            store["groups"] = kept
            _save()
        return changed


# ─── Records / runs ──────────────────────────────────────────────────────


def get_all(limit: int = 100) -> list[dict]:
    """Newest-first records. `limit <= 0` (or None) returns every record — the
    Data panel needs the true total, not a capped window that pins the visible
    count at the limit no matter how many are really stored."""
    records = _load()["records"]
    if not limit or limit <= 0:
        return list(reversed(records))
    return list(reversed(records[-limit:]))


def get_by_id(record_id: int) -> dict | None:
    return next((r for r in _load()["records"] if r["id"] == int(record_id)), None)


def remove(record_id: int) -> bool:
    with _LOCK:
        store = _load()
        before = len(store["records"])
        store["records"] = [r for r in store["records"] if r["id"] != int(record_id)]
        changed = len(store["records"]) != before
        if changed:
            _save()
        return changed


def clear() -> bool:
    """Wipes records and runs. Accounts are deliberately left alone — they hold
    credentials the user typed in, and "Clear all" on the Data panel must not
    silently destroy them."""
    with _LOCK:
        store = _load()
        store["records"] = []
        store["runs"] = []
        _save()
        return True


def save_run(run: dict) -> dict:
    with _LOCK:
        store = _load()
        store["seq"] += 1
        entry = {"id": store["seq"], "created_at": _now(), **run}
        store["runs"].append(entry)
        store["runs"] = store["runs"][-MAX_RUNS:]
        _save()
        return entry


def get_runs(limit: int = 25) -> list[dict]:
    return list(reversed(_load()["runs"][-limit:]))


# ─── Settings ────────────────────────────────────────────────────────────


def get_settings() -> dict:
    return dict(_load().get("settings") or {})


def save_settings(values: dict) -> dict:
    with _LOCK:
        store = _load()
        store["settings"] = dict(values)
        _save()
        return dict(values)


# ─── Scraping panel prefs ────────────────────────────────────────────────
# The Scraping panel's own selections that aren't global settings: the 🎯 target
# count and each account's chosen technique. Kept in their own top-level key so
# they persist to the file (survive restart/refresh) without riding the settings
# blob, which settings.update() rewrites wholesale.

TECHNIQUES = ("group", "event", "search", "all")


def get_scrape_prefs() -> dict:
    prefs = dict(_load().get("scrape_prefs") or {})
    prefs.setdefault("target", 0)      # 0 = unlimited: scrape until the source dries up
    prefs.setdefault("techniques", {})
    return prefs


def save_scrape_prefs(changes: dict) -> dict:
    """Merge a partial update into the stored scrape prefs and persist it."""
    with _LOCK:
        store = _load()
        cur = dict(store.get("scrape_prefs") or {})
        if "target" in changes and changes["target"] is not None:
            try:
                # 0 = unlimited, and no upper bound: a cap here silently truncated
                # long scrapes, which is exactly what the target is meant to leave
                # to the operator (or to the source drying up).
                cur["target"] = max(0, int(changes["target"]))
            except (TypeError, ValueError):
                pass
        if isinstance(changes.get("techniques"), dict):
            techs = dict(cur.get("techniques") or {})
            for key, val in changes["techniques"].items():
                if val in TECHNIQUES:
                    techs[str(key)] = val
                elif val is None:
                    techs.pop(str(key), None)
            cur["techniques"] = techs
        cur.setdefault("target", 0)
        cur.setdefault("techniques", {})
        store["scrape_prefs"] = cur
        _save()
        return dict(cur)


# ─── Campaign message drafts ─────────────────────────────────────────────
# The drafts behind the campaign's msg1/msg2/… tabs. Kept in MinIO (the messages
# bucket is their source of truth) behind these two functions, so every caller is
# unchanged; a local in-memory copy answers reads when MinIO is briefly
# unreachable, and any drafts still in an old store.json are migrated up on first
# read.
_draft_cache: list | None = None


def get_message_drafts() -> list:
    global _draft_cache
    from services.storage import message_storage

    try:
        drafts = message_storage.load()
    except message_storage.mc.MinioUnavailable:
        if _draft_cache is not None:
            return list(_draft_cache)          # serve the last known copy
        raise
    # One-time migration: if MinIO has none but a legacy store.json still does,
    # push those up so nothing is lost on the switch to MinIO.
    if not drafts:
        legacy = _load().get("message_drafts") or []
        if legacy:
            message_storage.push(legacy)
            drafts = list(legacy)
    _draft_cache = list(drafts)
    return list(drafts)


def save_message_drafts(drafts: list) -> list:
    global _draft_cache
    from services.storage import message_storage

    message_storage.push(list(drafts))       # MinIO is the source of truth
    _draft_cache = list(drafts)
    # Drop any legacy copy from the local store so it can't shadow MinIO later.
    with _LOCK:
        store = _load()
        if store.get("message_drafts"):
            store["message_drafts"] = []
            _save()
    return list(drafts)


# ─── Accounts ────────────────────────────────────────────────────────────
# NOTE: passwords are stored in plaintext in this JSON file. It is gitignored
# and local-only. See README — this is a local convenience store, not a vault.


def add_account(data: dict) -> dict:
    with _LOCK:
        store = _load()
        store["seq"] += 1
        account = {
            "id": store["seq"],
            "created_at": _now(),
            "label": (data.get("label") or "").strip(),
            "login": (data.get("login") or "").strip(),
            "password": data.get("password") or "",
            "logged_in": False,
            "last_login": None,
            "last_result": None,
        }
        store["accounts"].append(account)
        _save()
        return account


def list_accounts() -> list[dict]:
    return list(_load()["accounts"])


def get_account(account_id: int) -> dict | None:
    return next((a for a in _load()["accounts"] if a["id"] == int(account_id)), None)


def update_account(account_id: int, changes: dict) -> dict | None:
    with _LOCK:
        store = _load()
        for account in store["accounts"]:
            if account["id"] == int(account_id):
                account.update(changes)
                _save()
                return account
        return None


def remove_account(account_id: int) -> bool:
    with _LOCK:
        store = _load()
        before = len(store["accounts"])
        store["accounts"] = [a for a in store["accounts"] if a["id"] != int(account_id)]
        changed = len(store["accounts"]) != before
        if changed:
            _save()
        return changed


def count() -> int:
    return len(_load()["records"])


def status() -> dict:
    store = _load()
    return {
        "driver": "json-file",
        "file": str(config.DB_FILE.relative_to(config.ROOT_DIR)),
        "records": len(store["records"]),
        "runs": len(store["runs"]),
        "groups": len(store.get("groups") or []),
        "size_bytes": config.DB_FILE.stat().st_size if config.DB_FILE.exists() else 0,
    }
