"""Store Meetup member records in MinIO.

The bucket holds members.txt: the scrape's
record of every member it has ever seen — handle, display name, profile bio, home
location, the groups they were found in, plus a `used_count` column — as one
tab-separated text object, one record per line, deduped by handle
(case-insensitive). A member seen again UPSERTS: their bio is refreshed in place
rather than duplicated.

Every row has the same shape: `bio` is always present — a member collected off a
group roster without opening their profile is stored with bio "unknown"
(UNKNOWN_BIO) — and a real bio is never later overwritten with "unknown".

    add(records)             merge/refresh, dedup by handle
    load_all()               read the list back
    bump_uses(names)         +1 to used_count
    remove(name)             delete one member
    count()                  how many are stored

Every operation logs to the terminal via the minio logger.
"""
from __future__ import annotations

import json
import threading

from . import minio_client as mc
from . import tsv

OBJECT_NAME = "members.txt"
_LEGACY_JSON = "members.json"    # older format, read once then migrated to .txt

# Read-modify-write (load → merge → put) has to be atomic within this process, or
# two group scrapes finishing together could both load, both miss, and one's
# records get overwritten. Single process, so a lock is enough.
_lock = threading.Lock()

log = mc.log

# How many times a campaign has staged/sent to this member. A COLUMN, not a value
# a scrape supplies: every row carries it (0 until first used), bumped by
# bump_uses(). It has to be listed in _FIELDS or _clean() would strip it back off.
USED_FIELD = "used_count"

# Only these fields are stored; everything else on an incoming (or legacy) record
# is dropped so the object holds nothing more.
#   groups — the group urlnames this member was found in, comma-joined. Kept as
#   one text column rather than its own table because it is only ever read back
#   whole, and a join table for it would be a second MinIO round-trip per scrape.
_FIELDS = ("username", "name", "bio", "location", "groups", USED_FIELD)

# The bio a record carries when the scrape never opened the profile — every
# scrape lands a row, so a handle collected off a group roster or an event
# attendee list is stored with this rather than a missing/blank bio. Kept literal
# so the Data panel can tell "no bio yet" from a real one.
UNKNOWN_BIO = "unknown"


def _is_unknown(bio) -> bool:
    """True when `bio` is the no-bio sentinel (or empty) — the value a real bio
    must never be overwritten with."""
    return not (bio and str(bio).strip()) or str(bio).strip().lower() == UNKNOWN_BIO


def _merge_groups(old, new) -> str:
    """Union of two comma-joined group lists, order preserved.

    A member turns up in several groups over a campaign's life and each sighting
    is real evidence about them, so a later scrape ADDS its group rather than
    replacing what the row already knew.
    """
    seen: dict[str, str] = {}
    for blob in (old, new):
        for part in str(blob or "").split(","):
            slug = part.strip()
            if slug and slug.lower() not in seen:
                seen[slug.lower()] = slug
    return ",".join(seen.values())


def _clean(record: dict) -> dict | None:
    """Reduce a record to the stored columns; drop one with no usable handle.
    `bio` is always present — a record with no readable bio gets "unknown" — so
    every stored row has the same shape."""
    if not isinstance(record, dict):
        return None
    name = str(record.get("username") or record.get("handle") or "").strip().lstrip("@")
    if not name:
        return None
    bio = record.get("bio")
    out = {
        "username": name,
        "name": str(record.get("name") or "").strip(),
        "bio": bio if (bio and str(bio).strip()) else UNKNOWN_BIO,
        "location": str(record.get("location") or "").strip(),
        "groups": _merge_groups("", record.get("groups")),
    }
    if record.get(USED_FIELD) is not None and USED_FIELD in record:
        try:
            out[USED_FIELD] = max(0, int(record[USED_FIELD]))
        except (TypeError, ValueError):
            pass
    return out


def _parse_legacy_json(raw: bytes) -> list[dict]:
    """The old members.json body, read so it can be rewritten as text."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log.warning("%s is not valid JSON — treating as empty", _LEGACY_JSON)
        return []
    if isinstance(data, dict):                       # tolerate a wrapped payload
        data = data.get("members") or data.get("records") or []
    return [r for r in (data if isinstance(data, list) else []) if isinstance(r, dict)]


def _row_in(record: dict) -> dict:
    """One parsed row with its types restored.

    Text columns come back as strings, so `used_count` has to be made an int
    again here — callers (and the Data panel) treat it as a number, and a bare
    "0" is truthy in a way 0 is not.
    """
    out = dict(record)
    out["username"] = str(record.get("username") or "").strip().lstrip("@")
    try:
        out[USED_FIELD] = max(0, int(record.get(USED_FIELD) or 0))
    except (TypeError, ValueError):
        out[USED_FIELD] = 0
    return out


def load_all() -> list[dict]:
    """Every stored member record. Empty list if nothing's there yet.

    Reads members.txt; if that isn't there yet but a legacy members.json is, the
    JSON is read instead so no data is lost before the next write converts it.
    Raises MinioUnavailable if the server is down — an empty list would be a lie
    that could wipe the whole set on the next write."""
    raw = mc.get_bytes(mc.BUCKET, OBJECT_NAME)
    if raw is None:
        legacy = mc.get_bytes(mc.BUCKET, _LEGACY_JSON)
        if legacy is None:
            return []
        log.info("%s not found — reading legacy %s", OBJECT_NAME, _LEGACY_JSON)
        return [_row_in(r) for r in _parse_legacy_json(legacy)]
    if tsv.looks_like_json(raw):                     # .txt written by an older build
        return [_row_in(r) for r in _parse_legacy_json(raw)]
    return [_row_in(r) for r in tsv.loads(raw, _FIELDS)]


def _put(records: list[dict]) -> int:
    """Write the whole list as members.txt. Overwrites. Returns the count.

    Every record leaves through here, so this is where the column defaults are
    guaranteed: a row with no used_count yet gets 0 rather than a missing key —
    keeping the object one consistent table shape.
    """
    for record in records:
        record.setdefault(USED_FIELD, 0)
    mc.put_bytes(mc.BUCKET, OBJECT_NAME, tsv.dumps(records, _FIELDS),
                 content_type="text/tab-separated-values; charset=utf-8")
    return len(records)


def _index(rows) -> dict[str, dict]:
    """Existing rows keyed by lower-cased handle, cleaned on the way in so any
    legacy extra columns are stripped by the write that follows."""
    by_key: dict[str, dict] = {}
    for record in rows:
        cleaned = _clean(record)
        if cleaned:
            by_key[cleaned["username"].lower()] = cleaned
    return by_key


def add(records) -> dict:
    """Merge `records` into the stored set, UPSERTING by handle (case-insensitive)
    — a repeat sighting refreshes the row instead of adding a duplicate.
    Returns {"added": [handles], "updated": [handles], "total": n}."""
    with _lock:
        incoming = [c for c in (_clean(r) for r in (records or [])) if c]
        if not incoming:
            return {"added": [], "updated": [], "total": _count_unsafe()}
        by_key = _index(load_all())          # raises MinioUnavailable if down
        added: list[str] = []
        updated: list[str] = []
        for rec in incoming:
            key = rec["username"].lower()
            row = by_key.get(key)
            if row is None:
                by_key[key] = rec
                added.append(rec["username"])
                continue
            # Never downgrade a real bio back to "unknown": a later roster-only
            # scrape of an already-enriched member must keep the bio it has.
            if _is_unknown(rec.get("bio")) and not _is_unknown(row.get("bio")):
                rec = {k: v for k, v in rec.items() if k != "bio"}
            # Same for the other enriched columns — a roster row carries neither
            # a location nor a display name, and a blank must not erase one.
            rec = {k: v for k, v in rec.items()
                   if v not in ("", None) or k not in ("name", "location")}
            rec["groups"] = _merge_groups(row.get("groups"), rec.get("groups"))
            row.update(rec)
            updated.append(row["username"])
        merged = list(by_key.values())
        if added or updated:
            _put(merged)
        log.info("member store: +%d new, %d refreshed (total %d)",
                 len(added), len(updated), len(merged))
        return {"added": added, "updated": updated, "total": len(merged)}


def bump_uses(names) -> dict:
    """+1 to `used_count` for each name in `names` (once per occurrence) — how many
    times a campaign has staged/sent to that member. A handle not yet in the store
    is added (bio "unknown") with a count of 1, so campaign usage is tracked even
    for handles that were uploaded rather than scraped. A bio already on a row is
    left untouched. Returns {"bumped", "added", "total"}."""
    cleaned_names = [str(n or "").strip().lstrip("@") for n in (names or [])]
    cleaned_names = [n for n in cleaned_names if n]
    with _lock:
        if not cleaned_names:
            return {"bumped": 0, "added": 0, "total": _count_unsafe()}
        by_key = _index(load_all())          # raises MinioUnavailable if down
        bumped = added = 0
        for name in cleaned_names:
            key = name.lower()
            row = by_key.get(key)
            if row is None:
                by_key[key] = {"username": name, "name": "", "bio": UNKNOWN_BIO,
                               "location": "", "groups": "", USED_FIELD: 1}
                added += 1
            else:
                row[USED_FIELD] = int(row.get(USED_FIELD) or 0) + 1
                bumped += 1
        merged = list(by_key.values())
        _put(merged)
        log.info("member store: bumped used_count on %d member(s) (+%d new) — total %d",
                 bumped, added, len(merged))
        return {"bumped": bumped, "added": added, "total": len(merged)}


def clean() -> dict:
    """Rewrite members.txt keeping ONLY the known columns on every record —
    strips any legacy extras from records already stored, without needing a fresh
    scrape. Returns {"total", "stripped", "removed_fields"}."""
    with _lock:
        existing = load_all()                # raises MinioUnavailable if down
        cleaned: dict[str, dict] = {}
        stripped = 0
        removed_fields: set[str] = set()
        for record in existing:
            extra = (set(record) - set(_FIELDS)) if isinstance(record, dict) else set()
            keep = _clean(record)
            if not keep:
                continue
            if extra:
                stripped += 1
                removed_fields |= extra
            cleaned[keep["username"].lower()] = keep
        merged = list(cleaned.values())
        _put(merged)
        log.info("member store cleaned: %d record(s), %d had extra columns stripped",
                 len(merged), stripped)
        return {"total": len(merged), "stripped": stripped,
                "removed_fields": sorted(removed_fields)}


def remove(name) -> dict:
    """Delete one member's row (case-insensitive).

    Rewrites the object without that handle. Returns {"removed": bool,
    "total": remaining}. A miss is not an error — removed is just False."""
    target = str(name or "").strip().lstrip("@").lower()
    with _lock:
        if not target:
            return {"removed": False, "total": _count_unsafe()}
        existing = [c for c in (_clean(r) for r in load_all()) if c]
        kept = [c for c in existing if c["username"].lower() != target]
        removed = len(kept) != len(existing)
        if removed:
            _put(kept)
        log.info("member store: remove @%s — %s (total %d)",
                 target, "removed" if removed else "not found", len(kept))
        return {"removed": removed, "total": len(kept)}


def _count_unsafe() -> int:
    """len(load_all()) without taking the lock — for callers already holding it."""
    try:
        return len(load_all())
    except mc.MinioUnavailable:
        return 0


def count() -> int:
    """How many member records are stored."""
    return len(load_all())
