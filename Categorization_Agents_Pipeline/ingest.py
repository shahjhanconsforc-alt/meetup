"""Get bios INTO the pipeline from somewhere other than a scrape.

The normal path is: the scraper writes the member store, the pipeline reads it.
This module is for the other cases — a CSV someone exported, a hand-written list,
a re-import of a backup from _minio_backup/ — so the pipeline can be run over
data the browser never touched.

Everything here writes to the MEMBER store, not the category store. Ingestion
adds people to be classified; it never adds verdicts. Keeping that line sharp is
what stops a bad import silently becoming a set of categories nobody derived.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from services.storage import meetup_member_storage as members
from services.storage import tsv


def from_rows(rows: list[dict]) -> dict:
    """Upsert already-parsed records. Returns {"added", "updated", "total"}."""
    cleaned = []
    for row in rows or []:
        handle = str(row.get("username") or row.get("handle") or row.get("member")
                     or "").strip().lstrip("@")
        if not handle:
            continue
        cleaned.append({
            "username": handle,
            "name": str(row.get("name") or row.get("display_name") or "").strip(),
            "bio": str(row.get("bio") or row.get("description")
                       or row.get("intro") or "").strip(),
            "location": str(row.get("location") or row.get("city") or "").strip(),
            "groups": str(row.get("groups") or row.get("group") or "").strip(),
        })
    if not cleaned:
        return {"added": [], "updated": [], "total": members.count()}
    return members.add(cleaned)


def from_csv(text: str) -> dict:
    """A CSV or TSV blob. The delimiter is sniffed rather than assumed — these
    files come from spreadsheets and exports, and guessing wrong turns every row
    into one column with no error to show for it."""
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel_tab if "\t" in sample else csv.excel
    return from_rows(list(csv.DictReader(io.StringIO(text), dialect=dialect)))


def from_names(names: list[str]) -> dict:
    """Bare handles, one per line — the lowest-effort import.

    They land with the store's "unknown" bio, so the loader will skip them until
    a scrape enriches them. That is correct: a handle with no text is nothing to
    classify, and pretending otherwise would fill the store with empty verdicts.
    """
    return from_rows([{"username": n} for n in names or [] if str(n).strip()])


def from_backup(path: str | Path) -> dict:
    """Restore a table dumped to _minio_backup/.

    Those files are the TSV the stores themselves write, so they round-trip
    exactly — including bios containing newlines, which is the whole reason the
    format escapes its fields.
    """
    raw = Path(path).read_bytes()
    if tsv.looks_like_json(raw):
        import json

        data = json.loads(raw.decode("utf-8"))
        rows = data if isinstance(data, list) else data.get("records") or []
    else:
        rows = tsv.loads(raw, members._FIELDS)  # noqa: SLF001
    return from_rows(rows)


def preview(rows: list[dict], limit: int = 10) -> dict:
    """What an import WOULD do, without doing it.

    Worth using before a large import: `from_rows` upserts, so a file with a
    wrongly-mapped column overwrites real bios with whatever was in that column,
    and there is no undo short of the backups in _minio_backup/.
    """
    parsed = []
    for row in (rows or [])[:limit]:
        handle = str(row.get("username") or row.get("handle") or "").strip().lstrip("@")
        if handle:
            parsed.append({"username": handle,
                           "bio": str(row.get("bio") or "")[:120]})
    return {"sample": parsed, "would_import": len([
        r for r in (rows or [])
        if str(r.get("username") or r.get("handle") or "").strip()
    ])}
