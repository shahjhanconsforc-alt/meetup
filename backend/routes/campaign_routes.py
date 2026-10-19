from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from backend import control, db, tasks, uploads
from backend.automation import campaign, media

router = APIRouter(prefix="/api/campaign", tags=["campaign"])


class Draft(BaseModel):
    label: str = ""
    text: str = ""
    # Turned off by the operator: still stored, still on the strip, never sent.
    # Declared here or the field is dropped on the way in — a draft would come
    # back enabled the moment the panel reloaded it.
    disabled: bool = False


class DraftsBody(BaseModel):
    drafts: list[Draft] = []


class TargetsBody(BaseModel):
    limit: int = 50
    min_bio: int = 0
    skip_used: bool | None = None


class OutreachBody(BaseModel):
    account_id: int | None = None
    # Several accounts run the campaign together: the recipients are dealt
    # between them and each gets its own browser window and its own tab in the
    # live panel. `account_id` is the one-account form and still works.
    account_ids: list[int] = []
    usernames: list[str] = []
    limit: int = 0
    # `image` is the single-image form kept for older callers; `images` is what
    # the wizard sends, and the sender picks one per recipient from the pool so
    # that every message in a run doesn't carry an identical attachment.
    image: str | None = None
    images: list[str] = []
    # The operator skipped the Messages step: send the picture with no text.
    # Explicit rather than inferred from an empty draft set, because a bucket
    # with nothing in it looks identical and must not send the same thing.
    no_text: bool = False
    dry_run: bool = False


class ImportMember(BaseModel):
    username: str = ""
    name: str = ""


class ImportBody(BaseModel):
    members: list[ImportMember] = []
    """A previewed upload being committed — the handles the operator looked at
    and kept, not the file they came from."""
    usernames: list[str] = []


class RsvpBody(BaseModel):
    account_id: int | None = None
    event_urls: list[str] = []


# ─── Drafts ──────────────────────────────────────────────────────────────


@router.get("/drafts")
async def get_drafts():
    try:
        return {"drafts": db.get_message_drafts(),
                "placeholders": list(_placeholders())}
    except Exception as exc:  # noqa: BLE001 — MinIO down, with no cached copy yet
        raise HTTPException(503, f"Drafts unavailable: {exc}") from exc


def _placeholders():
    from services.storage import message_storage

    return message_storage.PLACEHOLDERS


@router.post("/drafts")
async def save_drafts(body: DraftsBody):
    drafts = [d.model_dump() for d in body.drafts]
    return {"drafts": db.save_message_drafts(drafts)}


# ─── Targeting ───────────────────────────────────────────────────────────


@router.post("/targets")
async def targets(body: TargetsBody):
    """Preview who an outreach run would message, without starting one."""
    try:
        rows = campaign.pick_targets(limit=body.limit, skip_used=body.skip_used,
                                     min_bio=body.min_bio)
    except Exception as exc:  # noqa: BLE001 — MinIO down
        raise HTTPException(503, f"Targets unavailable: {exc}") from exc
    return {"targets": rows, "count": len(rows)}


@router.post("/upload/users")
async def upload_users(file: UploadFile = File(...), store: bool = False):
    """Parse handles out of a .csv/.tsv/.txt/.xlsx upload.

    Parses only — nothing is written unless `store=true`. An upload is a thing
    an operator does to LOOK at a list; silently merging it into the member
    store on sight would make a mis-picked column permanent before the preview
    that would have caught it is even on screen.
    """
    data = await file.read()
    result = uploads.parse_handles(file.filename or "upload", data)
    if not result["count"]:
        raise HTTPException(400, result["detail"] or "No handles found in that file.")

    if store:
        try:
            rows = result.get("members") or [{"username": u}
                                             for u in result["usernames"]]
            saved = db.add_member_bios(rows)
            result["stored"] = len(saved.get("added", []))
        except Exception as exc:  # noqa: BLE001 — parsing still succeeded
            result["stored"] = 0
            result["detail"] += f" (not saved: {exc})"
    return result


@router.post("/upload/messages")
async def upload_messages(file: UploadFile = File(...), replace: bool = False):
    """Parse message drafts out of an upload.

    A .txt is split on blank lines so multi-line messages survive; a spreadsheet
    takes one draft per row. Nothing is saved unless `replace=true`.
    """
    data = await file.read()
    result = uploads.parse_messages(file.filename or "upload", data)
    if not result["count"]:
        raise HTTPException(400, result["detail"] or "No messages found in that file.")

    if replace:
        try:
            db.save_message_drafts(result["drafts"])
            result["saved"] = result["count"]
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"Parsed, but could not save: {exc}") from exc
    return result


@router.post("/import/users")
async def import_users(body: ImportBody):
    """Commit a previewed list of rows to the member store."""
    rows = [member.model_dump() for member in body.members
            if member.username.strip()]
    if not rows and not body.usernames:
        raise HTTPException(400, "No usernames given.")
    try:
        if rows:
            saved = db.add_member_bios(rows)
            return {"added": saved.get("added", []),
                    "skipped": saved.get("updated", []),
                    "total": saved.get("total", 0)}
        return db.record_scraped_members(body.usernames)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"Could not save: {exc}") from exc


# ─── Media ───────────────────────────────────────────────────────────────


@router.get("/media")
async def media_list():
    """Images a campaign can attach."""
    try:
        return {"media": media.listing()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"Media unavailable: {exc}") from exc


@router.post("/media")
async def media_upload(files: list[UploadFile] = File(...)):
    """Upload one or more images.

    Takes a list so a multi-select in the picker is one request rather than N —
    and so a partial failure reports which file failed instead of leaving the
    caller to reconcile several responses.
    """
    stored, failed = [], []
    for item in files:
        data = await item.read()
        if not data:
            failed.append(f"{item.filename}: empty")
            continue
        try:
            stored.append(media.media_storage.put(
                item.filename or "upload", data, content_type=item.content_type))
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{item.filename}: {exc}")
    if not stored:
        raise HTTPException(503, "; ".join(failed) or "Nothing uploaded.")
    return {"uploaded": stored, "failed": failed, "count": len(stored)}


# ─── Running ─────────────────────────────────────────────────────────────


@router.post("/outreach")
async def outreach(body: OutreachBody):
    """Start an outreach run. Returns a task id — poll /api/automation/tasks/{id}.

    Run it with dry_run first. There is no unsend on Meetup, and a mistake here is
    a mistake in someone else's inbox.
    """
    targets_list = None
    if body.usernames:
        by_name = {r["username"].lower(): r for r in db.list_scraped_members()}
        targets_list = [by_name.get(u.lower().lstrip("@"), {"username": u.lstrip("@")})
                        for u in body.usernames]

    try:
        ctl = control.start("outreach", meta={"dry_run": body.dry_run})
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc

    # Messages skipped: an empty draft list, which the run reads as picture-only
    # and refuses if there is no picture either.
    task_id = tasks.spawn("outreach", campaign.outreach(
        ctl, account_id=body.account_id, account_ids=body.account_ids,
        targets=targets_list,
        drafts=[] if body.no_text else None,
        image=body.image, images=body.images,
        limit=body.limit, dry_run=body.dry_run), ctl=ctl)
    return {"task": task_id, "run": ctl.snapshot()}


@router.post("/rsvp")
async def rsvp(body: RsvpBody):
    """RSVP to the given events. Always explicit — never inferred from a scrape."""
    if not body.event_urls:
        raise HTTPException(400, "Give at least one event URL.")
    try:
        ctl = control.start("rsvp")
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc

    task_id = tasks.spawn("rsvp", campaign.rsvp_events(
        ctl, account_id=body.account_id, event_urls=body.event_urls), ctl=ctl)
    return {"task": task_id, "run": ctl.snapshot()}


@router.get("/runs")
async def runs():
    return {"runs": db.get_runs(limit=25)}
