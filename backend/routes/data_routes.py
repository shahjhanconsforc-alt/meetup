import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend import activity, db, tasks

router = APIRouter(prefix="/api/data", tags=["data"])


class NamesBody(BaseModel):
    usernames: list[str] = []


# ─── Members ─────────────────────────────────────────────────────────────


@router.get("/members")
async def members(categories: bool = True):
    """Every stored member, as the Recipients table renders them.

    `categories` folds in the Find categories each member was scraped under —
    joined from the roster files, since the member store has no such column.
    It costs a read per roster, so a caller that only wants the handles can turn
    it off; the panel wants it, because "send to everyone under Tech" is the
    question the column exists to answer.
    """
    try:
        rows = db.list_scraped_members()
    except Exception as exc:  # noqa: BLE001 — MinIO down
        raise HTTPException(503, f"Member store unavailable: {exc}") from exc

    known: dict[str, list[str]] = {}
    if categories:
        known = db.member_categories()
        for row in rows:
            row["categories"] = known.get(row["username"].lower(), [])
    # The categories that actually have somebody in them, for the panel's
    # picker. Off the joined rows rather than the roster listing, so a category
    # whose members have all been deleted does not sit in the filter offering
    # nobody.
    present = sorted({name for names in known.values() for name in names})
    return {"members": rows, "count": len(rows), "categories": present}


@router.post("/members")
async def add_members(body: NamesBody):
    """Upload handles by hand — for a list gathered outside the scraper."""
    if not body.usernames:
        raise HTTPException(400, "No usernames given.")
    try:
        return db.record_scraped_members(body.usernames)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"Could not save: {exc}") from exc


@router.post("/members/backfill-names")
async def backfill_names():
    """Give nameless stored members the name their event roster already holds.

    For a store filled before names were kept on the way in — the rosters had
    them all along. Never overwrites a name that is already on a row.
    """
    try:
        return db.backfill_member_names()
    except Exception as exc:  # noqa: BLE001 — MinIO down
        raise HTTPException(503, f"Could not read the rosters: {exc}") from exc


@router.delete("/members/{username}")
async def remove_member(username: str):
    try:
        return db.remove_member(username)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"Could not remove: {exc}") from exc


@router.get("/export")
async def export(what: str = "members"):
    """The member table as a TSV download.

    The store is already tab-separated text for exactly this reason — the file
    is meant to be openable in a spreadsheet without a conversion step.
    """
    from services.storage import meetup_member_storage as mem
    from services.storage import tsv

    try:
        rows, columns, name = mem.load_all(), mem._FIELDS, "members.txt"  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"Store unavailable: {exc}") from exc

    body = tsv.dumps([{k: _flat(r.get(k)) for k in columns} for r in rows], columns)
    return StreamingResponse(
        iter([body]),
        media_type="text/tab-separated-values; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _flat(value):
    return ",".join(str(v) for v in value) if isinstance(value, list) else value


# ─── Event rosters ───────────────────────────────────────────────────────
# One text file per event: its members' names and ids. The members table says
# who has ever been seen; these say who was on a given event, which is the unit
# the operator works in and the one thing a table keyed by handle cannot answer.


@router.get("/rosters")
async def rosters(full: bool = False):
    """Every stored roster. `full=1` opens each one and includes its members —
    which is what the Data panel's table draws."""
    from services.storage import event_roster_storage as store

    try:
        return {"rosters": store.listing(with_members=full)}
    except Exception as exc:  # noqa: BLE001 — MinIO down
        raise HTTPException(503, f"Roster store unavailable: {exc}") from exc


@router.get("/rosters/{key}")
async def roster(key: str):
    """One roster's members, as the panel lists them."""
    from services.storage import event_roster_storage as store

    try:
        found = store.load(key)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"Roster store unavailable: {exc}") from exc
    if not found["members"]:
        raise HTTPException(404, f"No roster {key}")
    return {**found, "count": len(found["members"])}


@router.get("/rosters/{key}/download")
async def roster_download(key: str):
    """The file exactly as stored — it is already a spreadsheet-shaped table."""
    from services.storage import event_roster_storage as store

    try:
        body = store.raw(key)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"Roster store unavailable: {exc}") from exc
    if body is None:
        raise HTTPException(404, f"No roster {key}")
    return StreamingResponse(
        iter([body]),
        media_type="text/tab-separated-values; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{key}.txt"'},
    )


@router.delete("/rosters/{key}")
async def roster_remove(key: str):
    from services.storage import event_roster_storage as store

    try:
        return store.remove(key)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"Roster store unavailable: {exc}") from exc


# ─── Records / runs ──────────────────────────────────────────────────────


@router.get("/records")
async def records(limit: int = 0):
    return {"records": db.get_all(limit), "count": db.count()}


@router.delete("/records/{record_id}")
async def remove_record(record_id: int):
    if not db.remove(record_id):
        raise HTTPException(404, f"No record {record_id}")
    return {"deleted": record_id}


@router.post("/clear")
async def clear():
    """Wipes records and runs. Accounts and the MinIO stores are untouched —
    "Clear all" must not silently destroy credentials or a scrape."""
    return {"cleared": db.clear()}


@router.get("/status")
async def status():
    return db.status()


# ─── Activity feed ───────────────────────────────────────────────────────


@router.get("/activity")
async def activity_history(since: int = 0, limit: int = 300):
    return {"events": activity.history(since, limit), "stats": activity.stats()}


@router.delete("/activity")
async def activity_clear():
    return {"cleared": activity.clear()}


@router.get("/activity/stream")
async def activity_stream(request: Request):
    """Server-sent events — the live feed.

    SSE rather than a websocket: the stream is one-directional, and SSE
    reconnects on its own when the server restarts, which during development is
    constantly.
    """
    queue = activity.subscribe()

    async def _events():
        # The stream has to end on ITS OWN when the server is going down. A
        # disconnect check only catches the client leaving, and uvicorn waits
        # for in-flight requests before it will exit — so with a browser tab
        # open this loop is what made Ctrl+C hang.
        closing = activity.closing()
        try:
            for event in activity.history(limit=50):
                yield f"data: {json.dumps(event)}\n\n"
            while not closing.is_set():
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20.0)
                    if event.get("closing"):
                        break
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # A comment frame keeps proxies from closing an idle stream.
                    yield ": keep-alive\n\n"
        finally:
            activity.unsubscribe(queue)

    return StreamingResponse(_events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})
