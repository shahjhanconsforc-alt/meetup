"""Store the categorization pipeline's verdicts in MinIO.

The second of the members bucket's two tables: members_categories.txt, one row
per member the pipeline has reached — the category it settled on, the full
ranked list, the structured tags, and the entities it pulled out of the bio.

Written by Categorization_Agents_Pipeline (via its Writer agent) and read by the
app. The app never classifies; it only joins this onto the member store to show a
category beside a handle, so the two stay cleanly separated: scraping decides who
exists, the pipeline decides what they are.

Multi-valued columns (categories, tags, specifics, locations) are stored
comma-joined in a single text column rather than as JSON, so the file stays
greppable and spreadsheet-openable — the whole reason the stores are TSV. They
are split back into lists on read, so callers only ever see Python lists.

    add(rows)                merge/refresh, dedup by handle
    load_all()               read the list back
    categorized()            just the handles, lower-cased, as a set
    bump_uses(names)         +1 to used_count on handles already here
    remove(name)             delete one verdict
"""
from __future__ import annotations

import json
import threading

from . import minio_client as mc
from . import tsv

OBJECT_NAME = "members_categories.txt"
_LEGACY_JSON = "members_categories.json"

_lock = threading.Lock()

log = mc.log

USED_FIELD = "used_count"
_FIELDS = ("username", "bio", "category", "categories", "tags",
           "specifics", "locations", USED_FIELD)

# Columns holding a list, stored comma-joined.
_LIST_FIELDS = ("categories", "tags", "specifics", "locations")

# What a member with no confident category is filed as. The pipeline only writes
# a row when it concluded something, so this is mostly for rows migrated in from
# an older build — but the Data panel still needs a value it can group by.
UNKNOWN_CATEGORY = "Unknown"


def _split(value) -> list[str]:
    """A comma-joined column back into a list, blanks dropped."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def _join(value) -> str:
    """A list into its stored comma-joined form.

    Commas inside a single value would be ambiguous on the way back, so they are
    dropped here rather than escaped — no category, tag or place name this
    pipeline produces contains one, and a silently-split value would be worse
    than a slightly altered one.
    """
    return ",".join(v.replace(",", " ").strip() for v in _split(value))


def _clean(row: dict) -> dict | None:
    """Reduce a verdict to the stored columns; drop one with no usable handle."""
    if not isinstance(row, dict):
        return None
    name = str(row.get("username") or "").strip().lstrip("@")
    if not name:
        return None
    categories = _split(row.get("categories"))
    primary = str(row.get("category") or "").strip() or (
        categories[0] if categories else UNKNOWN_CATEGORY)
    # The single `category` column is only ever the head of `categories`, so keep
    # the two from drifting: whichever was supplied, both end up consistent.
    if primary not in categories:
        categories = [primary] + categories
    out = {
        "username": name,
        "bio": str(row.get("bio") or "").strip(),
        "category": primary,
        "categories": categories,
        "tags": _split(row.get("tags")),
        "specifics": _split(row.get("specifics")),
        "locations": _split(row.get("locations")),
    }
    if USED_FIELD in row and row.get(USED_FIELD) is not None:
        try:
            out[USED_FIELD] = max(0, int(row[USED_FIELD]))
        except (TypeError, ValueError):
            pass
    return out


def _row_out(row: dict) -> dict:
    """One cleaned verdict flattened for storage — lists comma-joined."""
    out = dict(row)
    for field in _LIST_FIELDS:
        out[field] = _join(row.get(field))
    out.setdefault(USED_FIELD, 0)
    return out


def _row_in(row: dict) -> dict:
    """One parsed row with its lists and int restored."""
    out = dict(row)
    out["username"] = str(row.get("username") or "").strip().lstrip("@")
    for field in _LIST_FIELDS:
        out[field] = _split(row.get(field))
    try:
        out[USED_FIELD] = max(0, int(row.get(USED_FIELD) or 0))
    except (TypeError, ValueError):
        out[USED_FIELD] = 0
    return out


def _parse_legacy_json(raw: bytes) -> list[dict]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log.warning("%s is not valid JSON — treating as empty", _LEGACY_JSON)
        return []
    if isinstance(data, dict):
        data = data.get("records") or data.get("categories") or []
    return [r for r in (data if isinstance(data, list) else []) if isinstance(r, dict)]


def load_all() -> list[dict]:
    """Every stored verdict, lists already split. Raises MinioUnavailable if the
    server is down — an empty list would be a lie that could wipe the table on
    the next write."""
    raw = mc.get_bytes(mc.BUCKET, OBJECT_NAME)
    if raw is None:
        legacy = mc.get_bytes(mc.BUCKET, _LEGACY_JSON)
        if legacy is None:
            return []
        log.info("%s not found — reading legacy %s", OBJECT_NAME, _LEGACY_JSON)
        return [_row_in(r) for r in _parse_legacy_json(legacy)]
    if tsv.looks_like_json(raw):
        return [_row_in(r) for r in _parse_legacy_json(raw)]
    return [_row_in(r) for r in tsv.loads(raw, _FIELDS)]


def _put(rows: list[dict]) -> int:
    mc.put_bytes(mc.BUCKET, OBJECT_NAME,
                 tsv.dumps([_row_out(r) for r in rows], _FIELDS),
                 content_type="text/tab-separated-values; charset=utf-8")
    return len(rows)


def add(rows) -> dict:
    """Merge verdicts in, UPSERTING by handle. A re-run of the pipeline replaces
    a member's verdict rather than appending a second one. Returns
    {"added", "updated", "total"}."""
    with _lock:
        incoming = [c for c in (_clean(r) for r in (rows or [])) if c]
        if not incoming:
            return {"added": 0, "updated": 0, "total": _count_unsafe()}
        by_key: dict[str, dict] = {}
        for row in load_all():               # raises MinioUnavailable if down
            cleaned = _clean(row)
            if cleaned:
                by_key[cleaned["username"].lower()] = cleaned
        added = updated = 0
        for rec in incoming:
            key = rec["username"].lower()
            row = by_key.get(key)
            if row is None:
                by_key[key] = rec
                added += 1
            else:
                # used_count belongs to the campaign, not the pipeline — a re-run
                # must not reset how many times this member has been contacted.
                rec.pop(USED_FIELD, None)
                row.update(rec)
                updated += 1
        merged = list(by_key.values())
        _put(merged)
        log.info("category store: +%d new, %d re-classified (total %d)",
                 added, updated, len(merged))
        return {"added": added, "updated": updated, "total": len(merged)}


def categorized() -> set[str]:
    """Lower-cased handles carrying a real category — what the Data panel
    partitions its two views on."""
    return {
        r["username"].lower()
        for r in load_all()
        if r.get("username") and str(r.get("category") or "").strip()
        and str(r["category"]).strip() != UNKNOWN_CATEGORY
    }


def bump_uses(names) -> dict:
    """+1 to `used_count`, but ONLY for handles this table already holds.

    Unlike the member store this one never invents a row: a verdict is something
    the pipeline concluded, and a campaign send is not evidence of a category.
    """
    wanted = {str(n or "").strip().lstrip("@").lower() for n in (names or [])}
    wanted.discard("")
    with _lock:
        if not wanted:
            return {"bumped": 0, "total": _count_unsafe()}
        rows = load_all()
        bumped = 0
        for row in rows:
            if str(row.get("username") or "").lower() in wanted:
                row[USED_FIELD] = int(row.get(USED_FIELD) or 0) + 1
                bumped += 1
        if bumped:
            _put(rows)
        return {"bumped": bumped, "total": len(rows)}


def remove(name) -> dict:
    """Delete one member's verdict (case-insensitive)."""
    target = str(name or "").strip().lstrip("@").lower()
    with _lock:
        if not target:
            return {"removed": False, "total": _count_unsafe()}
        rows = load_all()
        kept = [r for r in rows if str(r.get("username") or "").lower() != target]
        removed = len(kept) != len(rows)
        if removed:
            _put(kept)
        return {"removed": removed, "total": len(kept)}


def _count_unsafe() -> int:
    try:
        return len(load_all())
    except mc.MinioUnavailable:
        return 0


def count() -> int:
    return len(load_all())
