from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import control, db, sessions, tasks
from backend.automation import campaign, meetup
from backend.automation import playwright_runner as browser

router = APIRouter(prefix="/api/automation", tags=["automation"])


class OpenBody(BaseModel):
    account_id: int | None = None
    url: str | None = None


class ScrapeBody(BaseModel):
    account_id: int | None = None
    groups: list[str] = []
    events: list[str] = []
    technique: str = "group"
    target: int = 0


class SearchBody(BaseModel):
    query: str
    location: str = ""
    limit: int = 40


class GroupsBody(BaseModel):
    urlnames: list[str] = []


class CategoriesBody(BaseModel):
    account_id: int | None = None
    keys: list[str] = []      # empty = every tab on the strip
    limit: int = 0            # 0 = no cap on categories
    # How many cards to OPEN per category. Every card is a page load, so twenty
    # categories times an uncapped grid is hundreds of them; 0 harvests the
    # listing without opening anything.
    visit_cards: int = 3
    # Attendees to take per opened event. An 800-RSVP event would otherwise
    # scroll for minutes inside what is meant to be a browse.
    attendee_target: int = 40
    # A browse starts from an empty store. Set this to add to what is already
    # there instead — deliberately, rather than by forgetting to clear.
    keep_existing: bool = False


class FirstChatBody(BaseModel):
    account_id: int | None = None
    text: str = ""
    image: str | None = None
    login: bool = True


# ── The Find pipeline: category → event → members → chat ──────────────


def _require_idle_browser(*allowed: str) -> None:
    """Refuse a pipeline step while another run is driving the browser.

    THE reason a category pick looked like it skipped the event choice: a browse
    started earlier is still walking the strip, opening the first cards of each
    category and reading their attendees. It and the pipeline share one browser
    context, so they take turns on the same page — and the operator sees their
    own click apparently open an event they never picked.

    A 409 with the run's name is the honest answer: stop that run, then pick.
    """
    busy = [kind for kind in control.busy_kinds() if kind not in allowed]
    if busy:
        raise HTTPException(409, f"A {busy[0]} run is using the browser — stop it "
                                 f"in the run box first, then try again.")


class PickCategoryBody(BaseModel):
    account_id: int | None = None
    key: str


class AutoPipelineBody(BaseModel):
    account_id: int | None = None
    # 0 = the whole roster, which is what "list all its members" asks for.
    target: int = 0
    keep_existing: bool = False
    # Which Find tab a SWEEP works down. "all" is All events, the first tab on
    # /find/; a numeric id is one of the saved categories. Empty means the
    # default, so an older caller that sends nothing still sweeps All events.
    key: str = ""
    # Several accounts sweeping the same tab at once, taking events off one
    # shared queue. `account_id` is the one-account form and still works.
    account_ids: list[int] = []


class EventMembersBody(BaseModel):
    account_id: int | None = None
    url: str
    # 0 = the whole roster. Unlike a browse, this is one event the operator
    # chose, so there is no twenty-categories budget to protect.
    target: int = 0
    # Open EVERY attendee's profile, not only the ones whose card carried no id.
    # A page load per person either way, so it is a choice rather than a default.
    click_all: bool = False
    # Open the profiles AS the list is walked — the default, and what makes the
    # step hand back people the app can actually message. Off collects names.
    open_profiles: bool = True
    tabs: int = 5             # profiles opened at once while walking


class MemberTabsBody(BaseModel):
    account_id: int | None = None
    # The five (or fewer) people in one batch. Capped in campaign.py.
    # An empty string is a member whose card carried no id — open-all clicks
    # those on the attendee list, which is the only place their id exists.
    handles: list[str] = []
    batch: int = 0            # which batch this is, for the log line only
    tabs: int = 5             # open-all only: how many profiles at once
    # open-all: the roster as {username, name}. Names are what a clicked-through
    # id is matched back onto, so the panel can update the right row.
    members: list[dict] = []
    event_url: str = ""       # open-all: where to file the ids it recovers


class ChatMembersBody(BaseModel):
    account_id: int | None = None
    handles: list[str] = []
    text: str = ""
    # How many member tabs to keep open at once. Capped in campaign.py.
    tabs: int = 5
    image: str | None = None
    event_url: str = ""


# ─── Browser ─────────────────────────────────────────────────────────────


@router.get("/status")
async def status():
    return {"browser": await browser.status(), "runs": control.all_runs()}


@router.post("/open")
async def open_browser(body: OpenBody):
    profile = sessions.profile_dir(body.account_id) if body.account_id else None
    page = await browser.open_page(profile=profile, url=body.url)
    return {"opened": True, "url": page.url}


@router.post("/close")
async def close_browser(account_id: int | None = None):
    """Close one account's window, or every open one when none is named."""
    profile = sessions.profile_dir(account_id) if account_id else None
    return await browser.close_browser(profile=profile)


@router.post("/screenshot")
async def screenshot():
    path = await browser.screenshot("manual")
    if not path:
        raise HTTPException(409, "No page is open")
    return {"shot": path}


@router.get("/probe")
async def probe():
    """Which Meetup selectors match on the open page.

    The first thing to run when a scrape starts returning nothing — it separates
    "Meetup changed their markup" from "we're logged out" in one call, instead of
    leaving it to be guessed at from an empty result.
    """
    context = await browser.get_context()
    if not context.pages:
        raise HTTPException(409, "No page is open")
    return await meetup.probe(context.pages[0])


@router.get("/categories")
async def categories():
    """The saved strip. NO browser at all — this is what the panel draws.

    Saved rather than re-read: the strip is the same twenty tabs every time, and
    a page load to rediscover them on every panel open is a page load for
    nothing.
    """
    return {"categories": db.list_find_categories()}


@router.post("/categories/save")
async def save_categories(body: AutoPipelineBody | None = None):
    """Read the Find strip once and keep it.

    Lands on /find/ first when the browser is elsewhere — open_find leaves a
    page that is already there alone.
    """
    _require_idle_browser()
    result = await campaign.save_categories(
        account_id=(body.account_id if body else None))
    if not result["ok"]:
        raise HTTPException(409, result["detail"])
    return result


@router.get("/events")
async def events(limit: int = 0):
    """Event cards a category browse collected, newest first."""
    return {"events": db.list_events(limit), "total": len(db.list_events())}


@router.delete("/events")
async def clear_events():
    return {"cleared": db.clear_events()}


@router.post("/categories/open")
async def open_category(body: PickCategoryBody):
    """Step 1→2 of the pipeline: click one tab, hand back its events.

    Awaited rather than spawned as a task: it is one click and one read, and the
    panel has nothing to draw until the events come back.
    """
    _require_idle_browser()
    result = await campaign.open_category(account_id=body.account_id,
                                          key=body.key)
    if not result["ok"]:
        raise HTTPException(409, result["detail"])
    return result


@router.post("/pipeline/auto")
async def auto_pipeline(body: AutoPipelineBody | None = None):
    """Steps 1-3 in one press: a random category, its first event, its members.

    A task rather than an awaited call, unlike the two picks it replaces: it is
    three browser stages back to back and the last one scrolls a roster that can
    run to hundreds, so it has to be stoppable from the run box.
    """
    _require_idle_browser()
    try:
        ctl = control.start("pipeline")
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc

    task_id = tasks.spawn("pipeline", campaign.auto_pipeline(
        ctl, account_id=(body.account_id if body else None),
        target=(body.target if body else 0),
        keep_existing=(body.keep_existing if body else False)), ctl=ctl)
    return {"task": task_id, "run": ctl.snapshot()}


@router.post("/pipeline/sweep")
async def sweep_pipeline(body: AutoPipelineBody | None = None):
    """Every event on one tab, scraped one after another until stopped.

    The roll's opposite: it stays on the tab it started on and works down the
    list rather than opening one random category and taking a single event out
    of it. `key` picks the tab — All events by default, or one of the saved
    categories, which is what clicking one in the pool sends. Long by design, so
    it is a task with a stop rather than a request anybody waits on.
    """
    _require_idle_browser()
    try:
        ctl = control.start("pipeline")
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc

    task_id = tasks.spawn("pipeline", campaign.sweep_events(
        ctl, account_id=(body.account_id if body else None),
        account_ids=(body.account_ids if body else []),
        target=(body.target if body else 0),
        key=((body.key if body else "") or campaign.SWEEP_TAB)), ctl=ctl)
    return {"task": task_id, "run": ctl.snapshot()}


@router.post("/events/members")
async def event_members(body: EventMembersBody):
    """Step 2→3: everyone on one event's attendee list, with their ids.

    A task now, not an awaited call. It walks the list opening each attendee's
    profile for the id their card does not carry, which on a roster of forty is
    minutes of work — held open as one request it would look like a frozen
    panel, and there would be no way to stop it.
    """
    _require_idle_browser("members")
    try:
        ctl = control.start("members")
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc

    task_id = tasks.spawn("members", campaign.event_members(
        account_id=body.account_id, url=body.url, target=body.target,
        click_all=body.click_all, open_profiles=body.open_profiles,
        tabs=body.tabs, ctl=ctl), ctl=ctl)
    return {"task": task_id, "run": ctl.snapshot()}


@router.post("/members/tabs")
async def open_member_tabs(body: MemberTabsBody):
    """Open one batch of profiles as tabs on the open event screen.

    Awaited rather than spawned as a task: it is five page loads and the panel
    has nothing to draw until they are up. Nothing is sent — the operator works
    the tabs by hand, which is the whole point of the batch button.
    """
    if not body.handles:
        raise HTTPException(400, "This batch has no members.")
    _require_idle_browser()
    result = await campaign.open_member_tabs(account_id=body.account_id,
                                             handles=body.handles,
                                             batch=body.batch)
    if not result["ok"]:
        raise HTTPException(409, result["detail"])
    return result


@router.post("/members/open-all")
async def open_all_profiles(body: MemberTabsBody):
    """Open every listed member's profile, a tab-full at a time.

    A task, unlike one batch: two hundred profiles is minutes of page loads, so
    it has to be pausable and stoppable from the run box.
    """
    if not (body.members or body.handles):
        raise HTTPException(400, "No members to open.")
    _require_idle_browser("profiles")
    try:
        ctl = control.start("profiles")
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc

    task_id = tasks.spawn("profiles", campaign.open_all_profiles(
        ctl, account_id=body.account_id, handles=body.handles,
        members=body.members, event_url=body.event_url,
        tabs=body.tabs), ctl=ctl)
    return {"task": task_id, "run": ctl.snapshot()}


@router.post("/members/tabs/close")
async def close_member_tabs(body: MemberTabsBody | None = None):
    """Close the profile tabs a batch left open. The event screen stays."""
    _require_idle_browser()
    return await campaign.close_member_tabs(
        account_id=(body.account_id if body else None))


@router.post("/chat/members")
async def chat_members(body: ChatMembersBody):
    """Step 3→4: message the picked members, a tab-full at a time.

    A task, unlike the two steps above: this one sends real messages to real
    people for as long as the list is, so it has to be pausable and stoppable
    from the run box like every other long job.
    """
    if not body.handles:
        raise HTTPException(400, "Pick at least one member first.")
    if not (body.text or "").strip():
        raise HTTPException(400, "Write the message first.")
    _require_idle_browser("chat")
    try:
        ctl = control.start("chat")
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc

    task_id = tasks.spawn("chat", campaign.chat_members(
        ctl, account_id=body.account_id, handles=body.handles, text=body.text,
        tabs=body.tabs, image=body.image, event_url=body.event_url), ctl=ctl)
    return {"task": task_id, "run": ctl.snapshot()}


@router.post("/categories/browse")
async def browse_categories(body: CategoriesBody):
    """Click through the Find page's categories. Returns a task id — poll
    /tasks/{id}, and pause/stop it like any other run."""
    try:
        ctl = control.start("categories")
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc

    task_id = tasks.spawn("categories", campaign.browse_categories(
        ctl, account_id=body.account_id, keys=body.keys, limit=body.limit,
        visit_cards=body.visit_cards, attendee_target=body.attendee_target,
        keep_existing=body.keep_existing), ctl=ctl)
    return {"task": task_id, "run": ctl.snapshot()}


@router.post("/first-chat")
async def first_chat(body: FirstChatBody):
    """Log in, open the inbox, and reply in the conversation at the top.

    Synchronous rather than a background task: it is one message, the operator is
    watching the browser window, and the result they need is whether it sent.
    """
    result = await campaign.reply_to_first_chat(
        account_id=body.account_id, text=body.text,
        image=body.image, login=body.login)
    if not result.get("ok"):
        raise HTTPException(409, result.get("detail") or "Could not send")
    return result


# ─── Groups (scrape targets) ─────────────────────────────────────────────


@router.get("/groups")
async def list_groups():
    return {"groups": db.list_groups()}


@router.post("/groups")
async def add_groups(body: GroupsBody):
    if not body.urlnames:
        raise HTTPException(400, "Give at least one group URL or urlname.")
    return db.add_groups(body.urlnames)


@router.delete("/groups/{urlname}")
async def remove_group(urlname: str):
    if not db.remove_group(urlname):
        raise HTTPException(404, f"No group {urlname}")
    return {"deleted": urlname}


@router.post("/search")
async def search(body: SearchBody):
    """Find groups by keyword — the front of the funnel."""
    if not body.query.strip():
        raise HTTPException(400, "A search term is required.")
    page = await browser.open_page()
    return {"groups": await meetup.search_groups(page, body.query, body.location,
                                                 limit=body.limit)}


# ─── Scraping ────────────────────────────────────────────────────────────


@router.post("/scrape")
async def scrape(body: ScrapeBody):
    """Start a scrape. Returns a task id immediately — poll /tasks/{id}."""
    groups = body.groups or [g["urlname"] for g in db.list_groups()]
    if not groups and not body.events:
        raise HTTPException(400, "Add a group or an event first.")
    try:
        ctl = control.start("scrape", meta={"technique": body.technique})
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc

    task_id = tasks.spawn("scrape", campaign.scrape(
        ctl, account_id=body.account_id, groups=groups, events=body.events,
        technique=body.technique, target=body.target), ctl=ctl)
    return {"task": task_id, "run": ctl.snapshot()}


# ─── Scrape prefs ────────────────────────────────────────────────────────


@router.get("/prefs")
async def get_prefs():
    return db.get_scrape_prefs()


@router.post("/prefs")
async def save_prefs(changes: dict):
    return db.save_scrape_prefs(changes or {})


# ─── Run control ─────────────────────────────────────────────────────────


@router.get("/runs")
async def runs():
    return {"runs": control.all_runs()}


@router.post("/runs/{kind}/pause")
async def pause(kind: str):
    try:
        return control.require(kind).pause()
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/runs/{kind}/resume")
async def resume(kind: str):
    try:
        return control.require(kind).resume()
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/runs/{kind}/stop")
async def stop(kind: str):
    try:
        return control.require(kind).stop()
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/runs/stop-all")
async def stop_all():
    return {"stopped": control.stop_all()}


# ─── Tasks ───────────────────────────────────────────────────────────────


@router.get("/tasks")
async def task_list():
    return {"tasks": tasks.listing()}


@router.get("/tasks/{task_id}")
async def task_status(task_id: int):
    try:
        return tasks.status(task_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/tasks/{task_id}/cancel")
async def task_cancel(task_id: int):
    """Hard-cancel. Prefer stopping the RUN — that finishes the current action
    cleanly; this abandons it wherever it is."""
    try:
        return await tasks.cancel(task_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
