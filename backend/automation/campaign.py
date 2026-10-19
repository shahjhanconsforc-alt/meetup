"""Whole jobs — the layer that turns settings and a target list into a run.

    scrape(...)    groups/events -> members -> (optionally) their bios -> store
    outreach(...)  stored members -> a message each -> used_count bumped
    rsvp_events(...)  explicit event URLs -> RSVPs

Both long jobs are cooperative: they take a control.Run and check in between
members, so pause and stop land on a clean boundary rather than mid-action. Both
are also resumable in the only sense that matters — every member is written to
the store as it is collected, so a run stopped at member 300 of 800 keeps 300.

**Volume shaping.** This module owns the run's *shape*, which matters as much as
any per-action disguise. A thousand messages sent at an even rate down a list in
stored order is recognisable no matter how human each individual send looks. So:
the list is shuffled, cut into uneven batches with minutes between them, and
unrelated read-only browsing is interleaved every few rows. The numbers live in
behavior.py.

The layering rule: this module knows the ORDER of things, meetup.py knows the
DOM, and neither knows about HTTP.
"""
from __future__ import annotations

import asyncio
import random

from backend import activity, control, db, sessions, settings as app_settings
from backend.automation import behavior, meetup
from backend.automation import playwright_runner as browser
from backend.automation import typing as human

# Bios are written in batches rather than one at a time: the member store keeps
# the whole table in one MinIO object, so a write per member is a full download
# and upload per member. 25 keeps a stopped run from losing much while keeping
# the round-trips proportional to the run, not to its length.
_BATCH = 25


# A guard rail on how wide a campaign may run, matching the ceiling on
# settings.campaign_tabs. Deliberately generous and deliberately not the
# default: five is what the pool opens unless the operator says otherwise, and
# every step past that trades away how much the session looks like a person with
# a few tabs open.
CAMPAIGN_TABS_MAX = 20


async def _search_from_a_city(page, ctl, account=None, avoid: str = "") -> dict:
    """Point the search at ONE of the operator's cities, picked at random.

    First of the three page-level moves, and the order matters: changing the
    location re-fetches the grid and takes the category and the distance with
    it, so it has to happen before either of those is set. (The distance already
    learned this the hard way — see sweep_events.)

    Random per run on purpose. A list of cities whose first entry is used every
    time is a fixed home city with extra steps, and every run searching from the
    same place is one more thing that ties the runs together.

    `avoid` is the city a lane is trying to get AWAY from — the one it just gave
    up on. Searching the same place again is the one roll that cannot help, and
    with a short list it comes up often enough to matter.

    Best-effort: a run that cannot set it searches from wherever the session
    already is, and the line says so.
    """
    who = f"{account_label(account)}: " if account else ""
    names = app_settings.cities()
    if not names:
        ctl.meta["city"] = ""
        activity.log("scraping", f"{who}no search cities set — searching from "
                                 f"wherever the session is", "info")
        return {"ok": False, "label": "", "city": ""}

    # Unless it is the only one there is, in which case the operator has named
    # exactly one place and moving elsewhere is not on offer.
    elsewhere = [c for c in names if c.lower() != (avoid or "").lower()]
    city = random.choice(elsewhere or names)
    result = await meetup.set_location(page, city)
    # The LAST lane to set one wins the header, which is honest enough with one
    # account and meaningless with four — so the per-account city is on its own
    # line in the activity feed, where each is attributed.
    ctl.meta["city"] = result.get("label") or city
    activity.log("scraping", who + (result.get("detail") or "Location untouched"),
                 "ok" if result.get("ok") else "warn")
    # `city` as ROLLED, not as the site labelled it: a lane restarting somewhere
    # else needs the name it would roll again, and "Tokyo, JP" is not one of the
    # operator's entries.
    return {**(result or {}), "city": city}


def account_label(account_id: int | None) -> str:
    """What one account is CALLED, for the live panel's tab and the run's log.

    An id is not a name: with three windows open, "3" tells the operator nothing
    about which of their accounts is working which recipients. Falls back to the
    id when the account has gone — a run outlives a deletion, and a tab with no
    heading is worse than one headed "#3".
    """
    if not account_id:
        return "Default profile"
    try:
        from backend import accounts

        row = next((a for a in accounts.list_accounts()
                    if a.get("id") == account_id), None)
    except Exception:  # noqa: BLE001 — a label is never worth failing a run for
        row = None
    return ((row or {}).get("label") or (row or {}).get("login")
            or f"#{account_id}")


def batch_span(values: dict) -> tuple[int, int]:
    """How many messages one batch may hold — Settings, as a RANGE.

    A range rather than a number, and re-sampled for every batch: batches of
    exactly ten with exactly two minutes between them is a metronome, and the
    whole reason a campaign is broken into batches at all is to not have one.
    """
    low = int(values.get("batch_size_min") or 1)
    high = int(values.get("batch_size_max") or low)
    return (max(1, low), max(1, low, high))


def pause_span(values: dict) -> tuple[float, float]:
    """How long the run waits between two batches — Settings, as a RANGE."""
    low = float(values.get("batch_pause_min_s") or 0)
    high = float(values.get("batch_pause_max_s") or low)
    return (max(0.0, low), max(0.0, low, high))


def campaign_width() -> int:
    """How many recipients a campaign works at once. Settings → campaign_tabs.

    Clamped here as well as on save, because the value can also arrive from a
    store written by another build — and a width of 0 would mean a run that
    starts no workers and finishes instantly having sent nothing.
    """
    try:
        want = int(app_settings.current().get("campaign_tabs", 5) or 5)
    except (TypeError, ValueError):
        want = 5
    return max(1, min(want, CAMPAIGN_TABS_MAX))


def _named(members) -> list[dict]:
    """Scraped members as {username, name} rows, deduped by handle.

    Replaces the `sorted({m["username"] for m in members})` this module used to
    do at every store call. That set threw the display NAME away — the attendee
    cards carry it, and dropping it here is why the Users tab showed a column of
    bare ids with nothing to read beside them.

    Where one handle turns up twice, the sighting that HAS a name wins: a card
    read before its profile opened has an id and nothing else, and it must not
    be the version that lands.
    """
    best: dict[str, dict] = {}
    for member in (members or []):
        handle = str((member or {}).get("username") or "").strip().lstrip("@")
        if not handle:
            continue
        name = str((member or {}).get("name") or "").strip()
        seen = best.get(handle.lower())
        if seen is None or (name and not seen["name"]):
            best[handle.lower()] = {"username": handle, "name": name}
    return [best[key] for key in sorted(best)]


def _flush(rows: list[dict]) -> int:
    """Write a batch to the member store, swallowing a MinIO outage.

    A dead MinIO must not end a scrape that is otherwise working — the browser
    side is the expensive, rate-limited half, and losing it because storage
    blinked would mean re-walking every roster.
    """
    if not rows:
        return 0
    try:
        result = db.add_member_bios(rows)
        return len(result.get("added", [])) + len(result.get("updated", []))
    except Exception as exc:  # noqa: BLE001
        activity.log("scrape", f"Could not save {len(rows)} member(s): {exc}", "error")
        return 0


async def scrape(ctl, *, account_id: int | None = None,
                 groups: list[str] | None = None,
                 events: list[str] | None = None,
                 technique: str = "group",
                 target: int = 0) -> dict:
    """Collect members from groups and/or events.

    `technique` picks the source: "group" walks group rosters, "event" walks
    event attendee lists, "search" discovers groups by random seeds first, "all"
    rotates between surfaces on a clock. The distinction is about warmth — see
    meetup.scrape_event_attendees on why an attendee beats a member — and about
    footprint: a session that only ever touches one surface is narrow in a way
    real use is not.

    Profile visits (for bios) are the `visit_profiles` setting: they multiply the
    run's page loads by the number of members found, which is the difference
    between a two-minute run and an hour-long one. When on, collection runs in
    "bio mode" — a smaller scroll step, one profile per step, and a real dwell
    on each, because opening and closing a profile in 100ms reads as nothing
    human.
    """
    values = app_settings.current()
    cap = target or values["max_members_per_run"]
    profile = sessions.profile_dir(account_id) if account_id else None
    page = await browser.open_page(profile=profile)

    collected: dict[str, dict] = {}
    sources: list[tuple[str, str]] = []

    # Seed-driven discovery, when asked for it.
    if technique in ("search", "all"):
        discovered = await meetup.sweep_search(page, ctl=ctl, rounds=8)
        for row in discovered:
            db.add_groups([row["urlname"]])
        sources += [("group", row["urlname"]) for row in discovered]

    if technique in ("group", "all"):
        sources += [("group", g) for g in (groups or [])]
    if technique in ("event", "all"):
        sources += [("event", e) for e in (events or [])]

    if not sources:
        return {"members": 0, "detail": "No groups or events selected"}

    # Rotate which surface is worked, on a clock rather than at random — real
    # use has runs of one activity, not a coin flip per action.
    surface = behavior.Surface()
    backoff = behavior.Backoff()

    ctl.total = len(sources)
    for kind, source in sources:
        if not await ctl.checkpoint(f"{kind}: {source}"):
            break
        remaining = max(0, cap - len(collected))
        if not remaining:
            activity.log("scrape", f"Hit the {cap}-member cap for this run", "warn")
            break

        if kind == "group":
            found = await meetup.scrape_group_members(page, source, ctl=ctl,
                                                      target=remaining)
            db.mark_group_scraped(source, len(found))
        else:
            found = await meetup.scrape_event_attendees(page, source, ctl=ctl,
                                                        target=remaining)

        fresh = 0
        for row in found:
            handle = row.get("username")
            if handle and handle not in collected:
                collected[handle] = row
                fresh += 1

        # An empty surface is usually a rate-limit window, not an empty site.
        # Retrying hard is what turns a soft limit into a block.
        if fresh:
            backoff.reset()
        else:
            waited = await backoff.sleep()
            activity.log("scrape", f"{source} returned nothing — "
                                   f"backing off {waited:.0f}s", "warn")

        ctl.advance(1, f"{len(collected)} member(s) so far")

        # Short stop every batch of handles, then a longer beat between sources.
        await behavior.beat(behavior.SCRAPE_BATCH_PAUSE_S)
        surface.advance(behavior.pick(behavior.BETWEEN_SEARCH_S))
        await human.pause()

    # Land the handles immediately, bios or not. A member known by handle alone is
    # already a usable target, and a run stopped before the profile pass should
    # not throw away everything it walked.
    saved = _flush(list(collected.values()))

    enriched = 0
    if values["visit_profiles"]:
        batch: list[dict] = []
        handles = list(collected)
        ctl.total = len(handles)
        ctl.done = 0
        # Uneven runs with a real stop between them, rather than one continuous
        # sweep of every profile back to back.
        for group in behavior.batches(handles, behavior.SCRAPE_BATCH):
            if not await ctl.checkpoint():
                break
            for handle in group:
                if not await ctl.checkpoint(f"Reading @{handle}"):
                    break
                try:
                    row = await meetup.read_profile(page, handle)
                    # Keep the group the member was FOUND in — the profile page
                    # lists the groups they're in publicly, a different set.
                    row["groups"] = collected[handle].get("groups") or row.get("groups")
                    batch.append(row)
                    enriched += 1
                except Exception as exc:  # noqa: BLE001 — one bad profile isn't fatal
                    activity.log("scrape", f"@{handle}: {type(exc).__name__}", "warn")
                ctl.advance(1)
                if len(batch) >= _BATCH:
                    _flush(batch)
                    batch = []
                # A real dwell on the profile — see the docstring.
                await behavior.beat(behavior.BIO_DWELL_S)
            await behavior.beat(behavior.SCRAPE_BATCH_PAUSE_S)
        _flush(batch)

    detail = f"{len(collected)} member(s), {enriched} bio(s)"
    db.save_run({"kind": "scrape", "technique": technique,
                 "sources": len(sources), "members": len(collected),
                 "bios": enriched})
    activity.log("scrape", detail, "ok")
    return {"members": len(collected), "saved": saved, "bios": enriched,
            "detail": detail}


def pick_targets(*, limit: int, skip_used: bool | None = None,
                 min_bio: int = 0) -> list[dict]:
    """Choose who an outreach run will message.

    Ordering is by fewest previous contacts first — an untouched member is
    always a better target than one already messaged twice — and then SHUFFLED
    within the run by outreach() itself, so the send order isn't the store's.
    """
    values = app_settings.current()
    skip = values["skip_used_members"] if skip_used is None else skip_used

    rows = db.list_scraped_members()
    if min_bio:
        rows = [r for r in rows
                if r.get("bio") and r["bio"] != "unknown" and len(r["bio"]) >= min_bio]
    if skip:
        rows = [r for r in rows if not int(r.get("used_count") or 0)]

    rows.sort(key=lambda r: int(r.get("used_count") or 0))
    return rows[:max(0, limit)]


def render(template: str, member: dict) -> str:
    """Fill a draft's placeholders for one member.

    An unresolved placeholder is replaced with a neutral fallback rather than
    left in the text — sending a message that literally reads "Hi {name}" is
    worse than sending one that reads "Hi there".
    """
    name = (member.get("name") or "").strip()
    first = name.split()[0] if name else ""
    groups = str(member.get("groups") or "").split(",")
    values = {
        "{name}": name or "there",
        "{first_name}": first or "there",
        "{group}": (groups[0] if groups and groups[0] else "your group"),
        "{city}": (member.get("location") or "your area").strip(),
    }
    out = template or ""
    for token, value in values.items():
        out = out.replace(token, value)
    return out


def _assign(member: dict, drafts: list[dict], images: list[str],
            index: int) -> tuple[dict | None, str | None]:
    """Pick this recipient's draft and image.

    Randomised per recipient rather than rotated in lockstep. Strict rotation
    still produces a repeating pattern across a batch — recipient 1, 4 and 7 all
    getting draft 1 — which is visible to anyone comparing notes. The index
    seeds a shuffle-free fallback so a single-draft campaign still works.

    The whole DRAFT comes back, not just its text: the live table names which one
    each recipient got, and a table of message bodies is unreadable where a
    column of labels is a campaign at a glance.
    """
    # No drafts at all is a picture-only run — see outreach, which allows it
    # only when there are pictures to send.
    if not drafts:
        return None, (random.choice(images) if images else None)
    draft = random.choice(drafts) if len(drafts) > 1 else drafts[index % len(drafts)]
    image = random.choice(images) if images else None
    return draft, image


# What a draft is CALLED in the live table when the operator never named it.
# Positional, so the strip's first draft is "sms1" wherever it turns up — the
# panel's own vocabulary, and the word the operator used asking for this.
def _draft_label(draft: dict | None, drafts: list[dict]) -> str:
    if not draft:
        return ""
    label = str(draft.get("label") or "").strip()
    if label:
        return label
    try:
        return f"sms{drafts.index(draft) + 1}"
    except ValueError:
        return "sms"


# How long a stoppable wait sleeps before looking up to see whether the run is
# still wanted. Short enough that End feels like a button and long enough that a
# three-minute pause is not a few hundred wake-ups.
_WAIT_STEP_S = 0.5


async def _quiet(ctl, span) -> float:
    """behavior.beat, but it gives up the moment the run is stopped.

    The between-batch pause is one to three MINUTES. Slept in one go, an End
    pressed a second into it sat there doing nothing until the sleep ran out,
    which reads as a button that did not work — so it is slept in slices with a
    look at the run between each.

    Returns how long was actually waited, so the log says what happened rather
    than what was planned.
    """
    wanted = behavior.pick(span)
    waited = 0.0
    while waited < wanted:
        if ctl is not None and ctl.stopping:
            break
        step = min(_WAIT_STEP_S, wanted - waited)
        await asyncio.sleep(step)
        waited += step
    return waited


async def _close_tab(tab) -> None:
    """Shut one tab, swallowing a tab that is already gone."""
    try:
        await tab.close()
    except Exception:  # noqa: BLE001 — a closed tab is a closed tab
        pass


async def _send_in_tab(context, handle: str, name: str, text: str,
                       image: str | None, ctl=None) -> tuple[dict, object | None]:
    """Open ONE recipient's chat link in its own tab and write to it there.

    The link is Settings → chat_link with this member's id filled in — see
    meetup.chat_link — so which page a campaign lands on is the operator's to
    change without a new build.

    A tab per recipient rather than one page walked down the list: the composer
    is a real page with a draft in it, and navigating away from it is what the
    single-page version did to every message before the last one. Returns the
    result and the tab — the CALLER closes it, so that the tab and the row's
    status are decided in one place.

    `ctl` reaches into the typing, so Pause holds mid-message and End abandons
    one. A stop comes back as `stopped`, which is neither a send nor a failure:
    nobody was messaged and nothing went wrong.
    """
    tab = None
    try:
        tab = await context.new_page()
        result = await meetup.send_message(tab, handle, text, image=image,
                                           name=name, ctl=ctl)
    except control.RunStopped:
        return {"ok": False, "stopped": True,
                "detail": "Stopped before it was sent"}, tab
    except Exception as exc:  # noqa: BLE001 — one dead tab is not a dead run
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}, tab
    return result, tab


async def outreach(ctl, *, account_id: int | None = None,
                   account_ids: list[int] | None = None,
                   drafts: list[dict] | None = None,
                   targets: list[dict] | None = None,
                   image: str | None = None,
                   images: list[str] | None = None,
                   limit: int = 0, dry_run: bool = False) -> dict:
    """Message a set of members, with the run's shape deliberately broken up.

    Rotating drafts is not cosmetic: identical message text sent to many members
    of one group is the single most reportable pattern on Meetup, and the drafts
    exist so a campaign doesn't produce one. Neither is the batching — see the
    module docstring.

    `account_ids` runs the campaign from SEVERAL accounts at once — one browser
    window each, the recipients dealt between them, all of them going at the
    same time. That is two things at once: the run finishes in a fraction of the
    time, and no single account is the one that started every conversation.
    `account_id` is the one-account form and still works.

    Each recipient is opened in a TAB OF THEIR OWN, at Settings → chat_link with
    their id filled in, and the tab closes once that recipient is done.

    `dry_run` renders and counts everything without sending, which is what an
    operator should run first, because there is no unsend.
    """
    values = app_settings.current()
    cap = limit or values["max_messages_per_run"]

    # A disabled draft is one the operator turned off, not one they deleted —
    # the rotation must skip it, and "all of them are off" is a different thing
    # to say than "you have not written any".
    # `is not None`, NOT `or`: an empty list is the caller saying "no text at
    # all" — the panel's skipped Messages step — and falling back to the stored
    # drafts there would send the very thing that was skipped.
    given = drafts if drafts is not None else db.get_message_drafts()
    written = [d for d in given if (d.get("text") or "").strip()]
    drafts = [d for d in written if not d.get("disabled")]
    pool = list(images or ([image] if image else []))
    # No text is allowed, but only as a PICTURE-ONLY run: the panel lets the
    # operator skip messages or media, never both, and a send carrying neither
    # is an empty message to every recipient.
    if not drafts and not pool:
        return {"sent": 0,
                "detail": f"All {len(written)} draft(s) are disabled" if written
                          else "Nothing to send — no message drafts and no image"}
    if not drafts:
        activity.log("campaign", "No message text — sending the image alone",
                     "info")

    members = targets if targets is not None else pick_targets(limit=cap)
    if not members:
        return {"sent": 0, "detail": "No members matched the filters"}

    # Shuffle before sending: the stored order is roster order, i.e. the order
    # members appear on one group's page, and sending down it preserves that
    # correlation for anyone on the receiving end to notice.
    members = behavior.shuffled(members)

    # THE table the Campaign live panel draws, decided BEFORE a single message
    # goes out. Who gets which draft and which picture is a plan, and a plan is
    # worth seeing while the rows still say Pending rather than being pieced
    # together afterwards from what was sent.
    #
    # Mutated in place as the run walks it — ctl.meta is shallow-copied into
    # every snapshot, so a status written onto a row is on screen at the next
    # poll. Recipients with no handle are skipped here for the same reason the
    # loop skips them: there is no member to message.
    rows = []
    for position, member in enumerate(m for m in members if m.get("username")):
        draft, picture = _assign(member, drafts, pool, position)
        rows.append({
            "username": member.get("username") or "",
            "name": member.get("name") or "",
            # The draft's NAME for the table's column, its rendered text for the
            # row's tooltip — the operator wants "sms2" at a glance and the words
            # only when they stop on one.
            "sms": _draft_label(draft, drafts),
            "text": render((draft or {}).get("text") or "", member),
            "media": picture or "",
            "status": CHAT_PENDING,
            "detail": "",
        })
    ctl.meta["members"] = rows
    ctl.meta["drafts"] = [_draft_label(d, drafts) for d in drafts]

    ctl.total = len(rows)

    # -- Who runs which recipients ------------------------------------
    # One LANE per selected account: its own browser window, its own slice of
    # the queue, its own tab in the live panel. Dealt round-robin off the
    # already-shuffled list, so the slices come out within one of each other and
    # neither account gets a block of consecutive roster order.
    #
    # Splitting the work is the point of picking several: the run finishes in a
    # fraction of the time AND no single account starts the whole list of
    # conversations, which is the pattern that gets one flagged.
    ids = [a for a in (account_ids if account_ids is not None
                       else ([account_id] if account_id else [])) if a]
    if not ids:
        ids = [None]                    # no account picked -- the older shape
    lanes = [rows[i::len(ids)] for i in range(len(ids))]

    # How many recipients ONE account works at once -- Settings/campaign_tabs,
    # a tab each. Per lane, not shared: two accounts at five tabs is five tabs
    # in each of two windows, which is what picking two accounts asks for. A dry
    # run has no tabs and no pacing, so it stays single-file.
    width = 1 if dry_run else campaign_width()
    ctl.meta["tabs"] = width
    # THE tabs the live panel draws, one per account, each holding its own rows.
    # `members` stays as the flat list of every recipient, so anything reading
    # the run as one table still sees the whole campaign.
    ctl.meta["accounts"] = [
        {"id": aid, "label": account_label(aid), "members": lane, "tabs": width}
        for aid, lane in zip(ids, lanes)]

    sent, typed, failed, previews = 0, 0, [], []
    contacted: list[str] = []
    stopped = False                     # a stop ends EVERY lane, not just one

    async def _lane(account, queue: list[dict]) -> None:
        """One account's whole campaign: its window, its queue, its own pacing.

        The pacing state is per lane on purpose. Batch pauses and decoy browsing
        shape ONE account's traffic, and sharing them across accounts would make
        two windows go quiet and busy in lockstep -- which is a stronger signal
        that they belong together than either one's traffic alone.
        """
        nonlocal sent, typed, stopped, contacted

        if not queue:
            return
        page = context = None
        if not dry_run:
            page = await browser.open_page(
                profile=sessions.profile_dir(account) if account else None)
            # The window's own page stays where it is; every message gets its own
            # tab off the same context, so the session keeps its place on the
            # site.
            context = page.context

        cursor = 0
        gate = asyncio.Lock()
        done_rows = 0
        # Where the next batch pause falls, counted in COMPLETED recipients
        # rather than in list slices: with several workers in flight the old
        # fixed cut stopped meaning anything, but "a run of this many, then a
        # real stop" still does -- and it is the half that shapes the traffic.
        #
        # Sampled from the operator's range, and re-sampled after every pause.
        next_pause_at = behavior.pick_int(batch_span(values))
        next_decoy_at = behavior.pick_int(behavior.DECOY_EVERY)

        async def _worker() -> None:
            nonlocal cursor, done_rows, next_pause_at, next_decoy_at
            nonlocal sent, typed, stopped, contacted

            while True:
                # Held only while another worker in THIS lane is taking the
                # batch pause, so an uncontended acquire is the normal case and
                # costs nothing.
                async with gate:
                    pass
                if stopped or cursor >= len(queue):
                    return
                row = queue[cursor]
                cursor += 1

                handle = row["username"]
                if not await ctl.checkpoint(f"Messaging @{handle}"):
                    stopped = True
                    return
                text, chosen_image = row["text"], (row["media"] or None)

                if dry_run:
                    previews.append({"username": handle, "text": text,
                                     "image": chosen_image})
                    ctl.advance(1)
                    continue

                # Its tab is opening -- which IS the work starting, and the panel
                # says so before the page has loaded rather than after the
                # message went.
                row["status"] = CHAT_PROCESSING
                result, tab = await _send_in_tab(context, handle, row["name"],
                                                 text, chosen_image, ctl=ctl)

                # Ended mid-message. The tab still closes, the row goes back to
                # Pending -- nobody was messaged, and calling it failed would put
                # a red row against a recipient the operator chose not to reach.
                if result.get("stopped"):
                    if tab is not None:
                        await _close_tab(tab)
                    row["status"] = CHAT_PENDING
                    row["detail"] = result.get("detail") or ""
                    stopped = True
                    return

                went = bool(result.get("ok")) and result.get("sent", True)
                # EVERY tab closes once its recipient is done -- sent, written,
                # or failed. An earlier build kept the written-but-not-sent ones
                # up, on the reasoning that a loaded composer is what Click Send
                # off exists to produce; in practice a run of twenty leaves
                # twenty tabs open and the window is unusable long before anybody
                # reads one. The trade is real and worth knowing: with Click Send
                # off the message is typed and then the tab is shut, so the draft
                # goes with it.
                if tab is not None:
                    await _close_tab(tab)

                row["detail"] = result.get("detail") or ""
                if went:
                    row["status"] = CHAT_SENT
                    sent += 1
                    contacted.append(handle)
                elif result.get("ok"):
                    # Written, not sent: Click Send is off in Settings. It is not
                    # a failure and it is not a message either, so it is neither
                    # counted as sent nor marked against the member -- they have
                    # not been contacted until somebody presses the button.
                    row["status"] = CHAT_TYPED
                    typed += 1
                else:
                    row["status"] = CHAT_FAILED
                    failed.append(f"@{handle}: {result.get('detail')}")
                done_rows += 1
                ctl.advance(1, f"{sent} sent" if not typed else f"{typed} typed")

                # Bump in batches for the same reason bios are written in batches
                # -- one MinIO rewrite per message would dominate the run.
                # Swapped out before the await so a second worker cannot submit
                # the same names.
                if len(contacted) >= _BATCH:
                    batch, contacted = contacted, []
                    db.bump_member_uses(batch)

                # -- the quiet moments --------------------------------
                # Both are taken UNDER THE GATE, so they are quiet for this
                # lane's whole pool rather than for one worker while the others
                # keep sending -- a pause nobody else observes shapes nothing.
                if values["decoy_activity"] and done_rows >= next_decoy_at:
                    async with gate:
                        if done_rows >= next_decoy_at:  # re-check: another worker
                            next_decoy_at = done_rows + behavior.pick_int(
                                behavior.DECOY_EVERY)   # may have just done it
                            did = await meetup.decoy(page)
                            activity.log("campaign", f"Interleaved: {did}", "info")

                # A real stop between batches -- minutes, not seconds. This is
                # what stops the run being one long uninterrupted burst.
                if values["batch_campaigns"] and done_rows >= next_pause_at \
                        and cursor < len(queue):
                    async with gate:
                        if done_rows >= next_pause_at:
                            next_pause_at = done_rows + behavior.pick_int(
                                batch_span(values))
                            # Stoppable -- see _quiet. Held under the gate, so
                            # this is minutes during which no worker may start a
                            # recipient, and End must not have to wait them out.
                            waited = await _quiet(ctl, pause_span(values))
                            # Says the batch that just finished AND the next
                            # one, because both were rolled and neither is a
                            # number the operator can predict from the settings.
                            activity.log("campaign",
                                         f"{account_label(account)}: batch done "
                                         f"— waited {waited / 60:.1f} min, next "
                                         f"batch of "
                                         f"{next_pause_at - done_rows}", "info")

                # Between this worker's own recipients. Per worker, not per run:
                # it is how fast ONE thread of conversation-starting goes, and
                # the pool width is the separate decision about how many run
                # beside it.
                await human.pause()

        await asyncio.gather(*[_worker()
                               for _ in range(min(width, len(queue)))])

    if len(ids) > 1:
        activity.log("campaign", f"{len(rows)} recipient(s) split across "
                                 f"{len(ids)} account(s) - "
                                 + ", ".join(f"{account_label(a)}: {len(q)}"
                                             for a, q in zip(ids, lanes)), "ok")
    if width > 1:
        activity.log("campaign", f"{width} recipients at a time per account",
                     "info")
    # Every account at once. They are separate browsers with separate sessions,
    # so there is nothing for them to take turns over -- and running them one
    # after another would make picking several no faster than picking one.
    await asyncio.gather(*[_lane(aid, lane) for aid, lane in zip(ids, lanes)])

    if contacted:
        db.bump_member_uses(contacted)

    if dry_run:
        return {"sent": 0, "dry_run": True, "planned": len(previews),
                "previews": previews[:20],
                "detail": f"{len(previews)} message(s) would be sent"}

    # "typed" is its own outcome and reads as one: a run with Click Send off
    # reports what it wrote, not a row of zeroes and a pile of failures.
    detail = (f"{typed}/{len(members)} written, none sent — Click Send is off"
              if typed and not sent
              else f"{sent}/{len(members)} sent"
                   + (f", {typed} written but not sent" if typed else ""))
    if failed:
        detail += f", {len(failed)} failed"
    db.save_run({"kind": "outreach", "sent": sent, "typed": typed,
                 "failed": len(failed)})
    activity.log("campaign", detail, "ok" if sent or typed else "warn")
    return {"sent": sent, "typed": typed, "failed": failed[:20],
            "failed_count": len(failed),
            # The finished table, so the panel can settle on it: a run that ends
            # between two polls would otherwise leave its last row on
            # "Processing" forever.
            "members": rows, "detail": detail}


async def browse_categories(ctl, *, account_id: int | None = None,
                            keys: list[str] | None = None,
                            limit: int = 0,
                            visit_cards: int = 3,
                            attendee_target: int = 40,
                            keep_existing: bool = False) -> dict:
    """Open /find/ and click through its category tabs, one at a time.

    A cooperative run like the others: it checks in between categories, so pause
    and stop land between tabs rather than mid-click.

    The stored events are WIPED first. What the panel shows is meant to be "what
    this browse found", and keeping the last run's cards meant it opened holding
    events from a session the operator did not remember starting — filed under a
    category this run may not even visit, with counts that belong to neither.
    `keep_existing` is the way to accumulate across runs deliberately.

    `current_page`, not `open_page`: getting the tab must not navigate anywhere,
    because sweep_categories' own first step is the navigation to /find/ — and
    a bare open_page would first send the browser to PLAYWRIGHT_START_URL when
    one is configured, loading a page nobody asked for on the way past.
    """
    if not keep_existing:
        dropped = db.clear_events()
        if dropped:
            activity.log("scraping", f"Cleared {dropped} event(s) from the last "
                                     f"browse — this run starts empty", "info")

    page = await browser.current_page(sessions.profile_dir(account_id)
                                      if account_id else None)
    result = await meetup.sweep_categories(page, ctl=ctl, keys=keys, limit=limit,
                                           visit_cards_per_category=visit_cards,
                                           attendee_target=attendee_target)

    # Store what the browse saw. meetup.py read the cards; deciding what to KEEP
    # is this layer's job.
    # Tag each row with the category it was found under, so the panel can group
    # them the way the browse saw them — and so a stored row still knows where
    # it came from after a reload, when `visited` is long gone.
    events = [{**row, "category": visit.get("name", ""),
               "category_key": visit.get("key", "")}
              for visit in result.get("visited", [])
              for row in visit.get("events", [])]
    stored = {"added": 0, "updated": 0, "total": 0}
    groups = {"added": [], "total": 0}
    if events:
        stored = db.add_events(events)
        # The group slug off each card is the valuable half: it is exactly what
        # a scrape takes as a target, so browsing Find turns straight into
        # scrapeable groups instead of a list to copy out by hand.
        slugs = sorted({row["urlname"] for row in events if row.get("urlname")})
        groups = db.add_groups(slugs)
        activity.log("scraping",
                     f"{stored['added']} new event(s), {len(groups['added'])} new "
                     f"group(s) from {len(events)} card(s)", "ok")

    # The attendees are the point: they are members this bot can actually work
    # with, so they go into the member store the scraper and campaign both read.
    # WITH their names — the cards carry them, and a store of bare ids is a
    # recipient list nobody can read.
    handles = _named(m for row in events for m in (row.get("members") or []))
    members = {"added": [], "skipped": []}
    if handles:
        try:
            members = db.record_scraped_members(handles)
        except Exception as exc:  # noqa: BLE001 — MinIO down must not lose the browse
            activity.log("scraping",
                         f"Could not store {len(handles)} member(s): "
                         f"{type(exc).__name__}", "warn")

    opened = sum(v.get("visited_cards", 0) for v in result.get("visited", []))
    detail = f"{result['detail']} — {opened} card(s) opened, " \
             f"{stored['added']} new events, {len(groups['added'])} new groups, " \
             f"{len(handles)} member(s)"
    activity.log("scraping", detail, "ok" if result["ok"] else "warn")
    return {**result, "events": len(events), "opened": opened, "stored": stored,
            "groups_added": groups["added"], "members": len(handles),
            "members_added": len(members.get("added") or []), "detail": detail}


# ── The Find pipeline ─────────────────────────────────────────────────
# Category → one event → its members → chat each of them, a few tabs at a time.
# Three separate calls rather than one long run ON PURPOSE: the operator chooses
# at every step (which category, which event, which members), and a single job
# that swept from category to sent messages would take those choices away.


async def open_category(*, account_id: int | None = None, key: str = "") -> dict:
    """Click one category tab and return the events under it.

    Step 1→2. Stores what it saw the same way a full browse does, so an event
    picked here is the same stored row the rest of the app already knows about.
    """
    page = await browser.current_page(sessions.profile_dir(account_id)
                                      if account_id else None)
    opened = await meetup.open_find(page)
    if not opened["ok"]:
        return {**opened, "events": [], "key": key, "name": ""}

    tabs = await meetup.list_categories(page)
    chosen = next((c for c in tabs if c["key"] == str(key)), None)
    if chosen is None:
        return {"ok": False, "detail": f"No category tab {key}", "events": [],
                "key": key, "name": ""}

    result = await meetup.click_category(page, chosen["key"])
    if not result["ok"] or result.get("stale"):
        detail = result.get("detail") or "Category would not open"
        return {"ok": False, "detail": detail, "events": [], "key": chosen["key"],
                "name": chosen["name"]}

    # Settled, not instant: this list is what the operator picks their event
    # from, and a list of one because the grid was still rendering reads as the
    # app having chosen for them.
    events = await meetup.cards_when_settled(page)
    rows = [{**row, "category": chosen["name"], "category_key": chosen["key"]}
            for row in events]
    if rows:
        db.add_events(rows)
        db.add_groups(sorted({r["urlname"] for r in rows if r.get("urlname")}))
    activity.log("scraping", f"{chosen['name']}: {len(rows)} event(s) listed", "ok")
    return {"ok": True, "detail": f"{len(rows)} event(s) under {chosen['name']}",
            "events": rows, "key": chosen["key"], "name": chosen["name"]}


# The strip's first tabs are not categories: "all", "-999" (New Groups) and
# "-998" (From your Groups). A real category's key is its numeric id, so
# digits-only is the test — picking at random across the whole strip would land
# on "All events" often, which is not a category and would make every roll look
# the same.
def _real_categories(rows: list[dict]) -> list[dict]:
    return [row for row in rows if str(row.get("key") or "").isdigit()]


# How far down the event list to look when the first one hides its attendees.
# About EVENTS inside the one rolled category — the category itself is picked
# once and never walked past.
AUTO_EVENT_TRIES = 5

# How many cards to read when the ones already read turn out to be events this
# app has collected before. Only paid for in that case: the first read stays at
# AUTO_EVENT_TRIES, and the top of a category is exactly the part a previous
# roll is most likely to have taken.
AUTO_EVENT_POOL = 20

# The tab a sweep starts on. "all" is the first tab on /find/ — All events —
# and it is a real tab with a real data-testid, unlike the saved category strip
# which deliberately holds only the numbered ones.
SWEEP_TAB = "all"

# How many cards one pass over the tab reads before it starts opening them. The
# grid lazy-loads, so this is a screenful or two rather than the tab's whole
# catalogue: more arrive as the sweep scrolls back for them.
SWEEP_CARDS = 24

# Scrolls that produce no event the sweep has not already taken, before the tab
# is called finished. Three, because the grid fetches on scroll and a slow fetch
# looks exactly like the end of the list.
SWEEP_SCROLLS = 3

# A tab that runs dry, or an event that stalls, does not end the sweep — the lane
# picks ANOTHER city and works the tab there instead. This is how many times in a
# ROW that may produce nothing before the lane accepts there is nothing left to
# find: without it a sweep with an empty list of cities, a dead session or a
# broken selector would reload /find/ forever.
SWEEP_RESTARTS = 3

# How often the stall watchdog looks, relative to the window it is enforcing. A
# quarter of it, so the run notices reasonably promptly without a poll per second
# on a ninety-second clock. Floored so a very short window (the tests use
# fractions of a second) still ticks.
_STALL_STEP = 0.25
_STALL_STEP_MAX = 2.0


def stall_seconds(values: dict | None = None) -> float:
    """How long an event may produce nothing before it is abandoned. Settings.

    Clamped here as well as on save: the value can arrive from a store written
    by another build, and a window of 0 would abandon every event the moment it
    opened — before the page had even loaded.
    """
    try:
        want = float((values or app_settings.current()).get("scrape_stall_s", 90))
    except (TypeError, ValueError):
        want = 90.0
    return max(5.0, want)


def _members_seen(ctl) -> int:
    """How many members the run has published for the event in flight.

    The harvest publishes each member onto `ctl.meta["members"]` as their tab
    closes, so this number moving IS the definition of progress — and it is
    already there, which is why the watchdog needs nothing plumbed through the
    scraper to read it.
    """
    try:
        return len(ctl.meta.get("members") or [])
    except Exception:  # noqa: BLE001
        return 0


async def run_until_stalled(factory, ctl, seconds: float) -> tuple[dict, bool]:
    """Collect one event, giving up if it stops producing members. (result, stalled)

    THE watchdog. Everything in the scrape can fail loudly except stopping: a
    roster page that never serves its next twenty, a fetch that never returns, a
    list that quietly ends — none of them raise, so a sweep without a clock sits
    on one event until somebody notices.

    It is a STALL clock, not a time limit: every member that lands resets it, so
    a roster of nine hundred that keeps arriving is never cut off, and one that
    goes quiet for the window is dropped however long it has already run.

    `factory` is called to make the coroutine, so a cancelled attempt is never a
    coroutine that was created and never awaited.
    """
    task = asyncio.ensure_future(factory())
    step = min(_STALL_STEP_MAX, max(0.02, seconds * _STALL_STEP))
    seen, quiet = _members_seen(ctl), 0.0
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=step)
            if done:
                return task.result(), False
            now = _members_seen(ctl)
            # Any CHANGE, not just growth: the count resetting to zero is the
            # next event starting, which is progress by any reading.
            if now != seen:
                seen, quiet = now, 0.0
                continue
            # A PAUSED run produces nothing on purpose. Counting that time would
            # make every pause longer than the window look like a hung page and
            # throw away the event the operator paused in the middle of.
            if getattr(ctl, "paused", False):
                quiet = 0.0
                continue
            quiet += step
            if quiet < seconds:
                continue
            await _drop(task)
            return ({"ok": False, "members": [], "count": 0,
                     "detail": f"Stalled — no new member in {int(seconds)}s"},
                    True)
    except asyncio.CancelledError:
        # The whole run is going down. Take the event with it rather than
        # leaving a browser walk running with nothing watching it.
        task.cancel()
        raise


async def _drop(task) -> None:
    """Cancel one task and absorb whatever it dies of."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 — it was being abandoned either way
        pass


async def _lone_find_tab(page) -> int:
    """Back to ONE tab, on /find/. Returns how many were closed.

    Between two events, not after the sweep: walking a roster opens a tab per
    attendee, and a tab that failed to close — a profile that hung, a popup
    blocked mid-navigation — is left behind. Twenty events later that is twenty
    stray tabs on the window, each holding its own memory and each still counted
    by the browser pill.

    The lane's own page is the survivor and it is put back on /find/, so the next
    event starts from the same place the first one did rather than from whatever
    profile happened to be showing.
    """
    closed = 0
    for other in [p for p in getattr(getattr(page, "context", None), "pages", [])
                  if p is not page]:
        try:
            await other.close()
            closed += 1
        except Exception:  # noqa: BLE001 — a tab already gone is a tab closed
            pass
    await meetup.open_find(page)
    return closed


def _collected_event_keys() -> set[str]:
    """The events already in Data, by the name their roster is filed under.

    The Data panel IS the roster bucket, so that is what "already collected" is
    asked of — not the event store, which a roll clears at its own start and
    which would therefore answer "nothing" every time.

    A storage outage answers "nothing collected". Re-walking one event costs
    minutes; refusing to roll costs the whole press, and the press is what the
    operator asked for.
    """
    try:
        from services.storage import event_roster_storage as rosters

        return {str(row.get("key") or "") for row in rosters.listing()
                if row.get("key")}
    except Exception as exc:  # noqa: BLE001
        activity.log("scraping", f"Could not read what is already collected — "
                                 f"rolling without the skip "
                                 f"({type(exc).__name__})", "warn")
        return set()


# What a row in the scraped-events list can say, and the whole vocabulary of it:
# an event this run walked, one it passed over because its roster is already on
# file, and one that went quiet and was abandoned. The last two contribute
# nobody and are counted separately — the panel's total is this run's WORK.
SCRAPED, SKIPPED, STALLED = "scraped", "skipped", "stalled"


def _already_scraped(row: dict, collected: set[str]) -> bool:
    """Is this event's roster already in MinIO?

    Matched on the roster's own filing NAME rather than the URL, for the same
    reason _fresh_events is: the same meetup runs weekly under one title and one
    file, so "already scraped" has to mean the same thing here as it does in the
    bucket.
    """
    if not collected:
        return False
    from services.storage import event_roster_storage as rosters

    return rosters.safe_name(row.get("title") or "") in collected


def _fresh_events(rows: list[dict], collected: set[str]) -> list[dict]:
    """The events with no roster on file yet, in the order the grid listed them.

    Matched on the roster's own filing name rather than the URL: the same
    meetup runs weekly under one title and one file, so "already collected" has
    to mean the same thing here as it does in the bucket.
    """
    if not collected:
        return list(rows)
    from services.storage import event_roster_storage as rosters

    return [row for row in rows
            if rosters.safe_name(row.get("title") or "") not in collected]


async def save_categories(*, account_id: int | None = None) -> dict:
    """Read the Find strip once and keep it. {ok, categories, detail}.

    The strip is the same twenty tabs every time, so it is read on demand and
    saved rather than re-read per roll: a roll should be a click on a tab, not a
    page load to rediscover which tabs exist.
    """
    page = await browser.current_page(sessions.profile_dir(account_id)
                                      if account_id else None)
    opened = await meetup.open_find(page)
    if not opened["ok"]:
        return {**opened, "categories": []}

    rows = _real_categories(await meetup.list_categories(page))
    if not rows:
        return {"ok": False, "detail": "No category tabs on the Find page",
                "categories": []}
    db.save_find_categories(rows)
    activity.log("scraping", f"Saved {len(rows)} categor"
                             f"{'y' if len(rows) == 1 else 'ies'}", "ok")
    return {"ok": True, "categories": rows,
            "detail": f"{len(rows)} categories saved"}


async def sweep_events(ctl, *, account_id: int | None = None,
                       account_ids: list[int] | None = None,
                       key: str = SWEEP_TAB, target: int = 0,
                       keep_existing: bool = True) -> dict:
    """Walk ONE tab's events, scraping every one of them in turn. Non-stop.

    The roll this sits beside opens a RANDOM category and takes a single event
    out of it: one press, one event, somewhere else next time. This stays where
    it started -- All events, by default, which is the first tab on /find/ and
    the one that lists everything -- and works down it: its first event, then
    its second, then its third, coming back to the list between each and
    scrolling it for more once what is on screen runs out.

    `account_ids` runs it from SEVERAL accounts at once: a browser window each,
    all of them working down the same tab, taking events off one shared queue so
    no two ever walk the same roster. That is the point of picking several --
    the tab is scraped in a fraction of the time, and no single account is the
    one that opened every event on it.

    A lane that runs out of events, or lands on one that STALLS, does not end --
    it rolls another city and works the tab from there. That is what makes this
    non-stop in the sense that matters: the two ways a scrape quietly dies are a
    tab with nothing new left on it and a roster that stops arriving, and
    neither of them is a reason to stop scraping, only a reason to stop scraping
    HERE. A lane gives up after SWEEP_RESTARTS cities in a row with nothing to
    show for them.

    It ends when the operator stops it, or when every lane has given up that
    way. Cooperative, so a stop lands between two events rather than halfway
    through a roster.

    Events already in Data are skipped, same as the roll -- walking a roster
    this app already holds is minutes of browser work to rewrite one file.
    """
    if not keep_existing:
        dropped = db.clear_events()
        if dropped:
            activity.log("scraping", f"Cleared {dropped} event(s) from the last "
                                     f"run -- this sweep starts empty", "info")

    ids = [a for a in (account_ids if account_ids is not None
                       else ([account_id] if account_id else [])) if a]
    if not ids:
        ids = [None]                    # no account picked -- the older shape

    collected = _collected_event_keys()
    stall = stall_seconds()
    # SHARED across every lane. Two accounts walking the same event is the one
    # thing multi-account scraping must not do: it is minutes of browser work
    # each to write the same file twice.
    taken: set[str] = set()
    claim = asyncio.Lock()
    stopped = False
    events, members_total, last = 0, 0, None
    # What the panel's "scraped events" list draws: one row per event as it
    # FINISHES, newest first, with the account that did it. Appended to rather
    # than replaced, so the list is the run's history and not just its last
    # step.
    ctl.meta["events_done"] = []
    ctl.meta["accounts"] = [{"id": a, "label": account_label(a)} for a in ids]

    async def _lane(account) -> None:
        """One account: its own window, its own place in the list, the shared
        queue of events.

        A lane does not end when its tab runs dry or an event stalls — it moves
        to ANOTHER city and works the tab there. Only a run of passes that all
        produce nothing ends it.
        """
        nonlocal stopped
        page = await browser.current_page(sessions.profile_dir(account)
                                          if account else None)
        city, fruitless = "", 0
        while fruitless < SWEEP_RESTARTS:
            if stopped or not await ctl.checkpoint("Searching"):
                stopped = True
                return
            if not (await meetup.open_find(page))["ok"]:
                activity.log("scraping", f"{account_label(account)}: the Find "
                                         f"page would not load", "warn")
                return

            # ── The CITY, before the tab ─────────────────────────────
            # Changing the location re-fetches the grid and resets the tab with
            # it, so it goes first or it undoes the click. Each lane rolls its
            # OWN city: several accounts searching the same place at the same
            # moment is a stronger tie between them than either one's traffic
            # alone. `avoid` is the city this pass is leaving — rolling it again
            # is the one choice that cannot help.
            where = await _search_from_a_city(page, ctl, account=account,
                                              avoid=city)
            city = where.get("city") or ""

            # ── The TAB ──────────────────────────────────────────────
            # The distance filter is NOT touched. It used to be widened to its
            # maximum here, from back when a run searched from wherever the
            # profile happened to be pointed and 30km around that was too small
            # to be worth sweeping. The city is chosen deliberately now, so the
            # radius is Meetup's own default around a place the operator picked.
            if not (await meetup.click_category(page, key))["ok"]:
                activity.log("scraping", f"{account_label(account)}: {key} "
                                         f"would not open", "warn")
                return
            ctl.meta.setdefault("category",
                                {"key": key, "name": _tab_name(page, key)})

            got = await _pass(account, page)
            if stopped:
                return
            # A pass that scraped something has proved this city still has work
            # in it, so the counter starts over. Only a RUN of empty passes ends
            # the lane, because one empty city says nothing about the next.
            fruitless = 0 if got else fruitless + 1
            if fruitless < SWEEP_RESTARTS:
                activity.log("scraping",
                             f"{account_label(account)}: nothing more here — "
                             f"searching again from another city "
                             f"({fruitless}/{SWEEP_RESTARTS})"
                             if not got else
                             f"{account_label(account)}: this city is worked "
                             f"out — moving on", "info")

        activity.log("scraping", f"{account_label(account)}: {SWEEP_RESTARTS} "
                                 f"cities in a row with nothing new — stopping",
                     "warn")

    async def _pass(account, page) -> int:
        """ONE city's worth of the tab. Returns how many events it scraped.

        Ends when the grid runs dry, when an event stalls (the city is not the
        problem, but neither is sitting on it — somewhere else is a cheaper
        thing to try than the same page again), or when the operator stops.
        """
        nonlocal stopped, events, members_total, last
        mine = 0

        dry = 0
        while dry < SWEEP_SCROLLS:
            if stopped or not await ctl.checkpoint(
                    f"{events} event(s) · {members_total} member(s)"):
                stopped = True
                return mine

            cards = await meetup.cards_on_page(page, limit=SWEEP_CARDS)
            rows = [{**row, "category": ctl.meta["category"]["name"],
                     "category_key": key} for row in cards]
            if rows:
                db.add_events(rows)
                db.add_groups(sorted({r["urlname"] for r in rows
                                      if r.get("urlname")}))

            # CLAIMED under the lock, not merely filtered: two lanes reading the
            # same screenful a moment apart would otherwise both take the same
            # first event.
            #
            # An event whose roster is ALREADY in MinIO is claimed too, and
            # listed as skipped rather than passed over in silence. It used to
            # be invisible, which made a sweep over a tab this app had already
            # worked look like a sweep that was finding nothing.
            async with claim:
                fresh, already = [], []
                for row in rows:
                    url = row.get("url") or ""
                    if not url or url in taken:
                        continue
                    taken.add(url)
                    (already if _already_scraped(row, collected)
                     else fresh).append(row)

            for row in already:
                ctl.meta["events_done"].insert(0, {
                    "title": row.get("title") or row.get("url") or "",
                    "count": 0,
                    "url": row.get("url") or "",
                    "account": account_label(account),
                    "group": row.get("group") or row.get("urlname") or "",
                    "status": SKIPPED})
            if already:
                activity.log("scraping",
                             f"{account_label(account)}: skipped "
                             f"{len(already)} event(s) already in Data", "info")

            if not fresh:
                # Nothing new on screen. The grid lazy-loads, so this is "scroll
                # and look again", not "the tab is empty" -- until it has been
                # true SWEEP_SCROLLS times in a row.
                dry += 1
                try:
                    await meetup.visit.scroll_step(page, swift=True)
                except Exception:  # noqa: BLE001
                    return mine
                continue

            dry = 0
            ctl.total += len(fresh)
            for row in fresh:
                url = row.get("url") or ""
                title = row.get("title") or url
                if stopped or not await ctl.checkpoint(f"Opening {title}"):
                    stopped = True
                    return mine

                ctl.meta["event"] = {
                    "title": row.get("title") or "",
                    "when": row.get("when") or "",
                    "group": row.get("group") or row.get("urlname") or "",
                    "category": ctl.meta["category"]["name"],
                    "url": url,
                    "attendees": row.get("attendees") or 0}
                # WATCHED. An event that stops producing members does not raise
                # and does not end — it just sits there, so the clock is the
                # only thing that can tell the difference between a slow roster
                # and a dead one.
                result, stalled = await run_until_stalled(
                    lambda: event_members(account_id=account, url=url,
                                          target=target, ctl=ctl),
                    ctl, stall)
                if result["ok"]:
                    events += 1
                    mine += 1
                    members_total += result["count"]
                    last = result
                    # ON THE PANEL, as it lands. An event that took two minutes
                    # to walk and then vanished from the screen when the next
                    # one started was work the operator could not see happening.
                    ctl.meta["events_done"].insert(0, {
                        "title": row.get("title") or url,
                        "count": result["count"],
                        "with_id": result.get("with_id", result["count"]),
                        "url": url,
                        "account": account_label(account),
                        "group": row.get("group") or row.get("urlname") or "",
                        "status": SCRAPED})
                    activity.log("scraping",
                                 f"{account_label(account)} · {title}: "
                                 f"{result['count']} member(s) -- {events} "
                                 f"event(s) so far", "ok")
                else:
                    activity.log("scraping", f"{account_label(account)} · "
                                             f"{title}: {result['detail']}",
                                 "warn")
                ctl.advance(1, f"{events} event(s) · {members_total} member(s)")

                # ONE tab again, on /find/, before the next event opens.
                # Walking a roster opens a tab per attendee and the odd one is
                # left behind — a profile that hung, a popup blocked mid-
                # navigation. Twenty events later that is twenty stray tabs.
                shut = await _lone_find_tab(page)
                if shut:
                    activity.log("scraping", f"{account_label(account)}: closed "
                                             f"{shut} leftover tab(s)", "info")

                if stalled:
                    # ON THE LIST, like a skipped one. Ninety seconds of nothing
                    # followed by the panel looking untouched is the operator
                    # watching a run they cannot tell is still alive.
                    ctl.meta["events_done"].insert(0, {
                        "title": row.get("title") or url, "count": 0,
                        "url": url, "account": account_label(account),
                        "group": row.get("group") or row.get("urlname") or "",
                        "status": STALLED})
                    # Somewhere else, rather than the next event on a grid this
                    # session has just stopped being served properly. The lane
                    # rolls a new city and starts again — which is the whole
                    # reason a stall ends the pass instead of the event.
                    activity.log("scraping",
                                 f"{account_label(account)} · {title}: "
                                 f"{result['detail']} — starting again "
                                 f"somewhere else", "warn")
                    return mine

            # Back to the tab for the next screenful. The events were opened in
            # this same page, so the grid has to be asked for again -- and it
            # comes back at the top, which is why `taken` is what stops a second
            # pass re-taking the ones already done.
            if stopped or not await ctl.checkpoint("Back to the list"):
                stopped = True
                return mine
            if not (await meetup.open_find(page))["ok"]:
                return mine
            await meetup.click_category(page, key)

        return mine

    if len(ids) > 1:
        activity.log("scraping", f"Sweeping {_tab_name(None, key)} from "
                                 f"{len(ids)} accounts at once -- "
                                 + ", ".join(account_label(a) for a in ids),
                     "ok")
    # Every account at once. They are separate browsers with separate sessions,
    # so there is nothing for them to take turns over.
    await asyncio.gather(*[_lane(a) for a in ids])

    detail = (f"{events} event(s) · {members_total} member(s) collected"
              if events else "No new events on this tab")
    activity.log("scraping", f"Sweep finished -- {detail}",
                 "ok" if events else "warn")
    return {"ok": events > 0, "events": events, "count": members_total,
            "members": (last or {}).get("members", []),
            "event": (last or {}).get("event"),
            "events_done": ctl.meta["events_done"],
            "detail": detail}


def _tab_name(page, key: str) -> str:
    """What the tab being swept is called, for the log and the panel header.

    Off the saved strip rather than the page — `page` is unused and may be None,
    because the sweep has just clicked the tab and has no reason to read it
    back, and the strip is where every other part of this app gets a category's
    name from.
    """
    if key == SWEEP_TAB:
        return "All events"
    row = next((c for c in db.list_find_categories()
                if str(c.get("key")) == str(key)), None)
    return (row or {}).get("name") or key


async def auto_pipeline(ctl, *, account_id: int | None = None,
                        target: int = 0, key: str = "",
                        keep_existing: bool = False) -> dict:
    """ONE random category off the saved strip, its first event, its members.

    One category per press, and no walking. An earlier version tried the next
    category when a roll came back empty, which is a tour of the taxonomy by
    another name — so an empty category is now reported as the outcome of the
    roll, and pressing again is what rolls another. The operator, not the run,
    decides how many categories get opened.

    Cooperative like every other run: it checks in between stages, so a stop
    lands between them rather than mid-scroll.

    Starts from an empty store, like a browse: a roll is the beginning of a
    piece of work, and the panel filling with the last one's events under a
    heading this roll never touched is the confusing half of what it shows.
    """
    if not keep_existing:
        dropped = db.clear_events()
        if dropped:
            activity.log("scraping", f"Cleared {dropped} event(s) from the last "
                                     f"run — this roll starts empty", "info")

    page = await browser.current_page(sessions.profile_dir(account_id)
                                      if account_id else None)
    ctl.total = 3

    saved = _real_categories(db.list_find_categories())
    if not saved:
        # Nothing saved yet — read the strip once, keep it, and carry on. This
        # is the only path that lists categories at all.
        first = await save_categories(account_id=account_id)
        if not first["ok"]:
            return {**first, "category": None, "event": None, "members": []}
        saved = first["categories"]

    category = (next((c for c in saved if c["key"] == str(key)), None) if key
                else behavior.shuffled(saved)[0])
    if not category:
        return {"ok": False, "detail": f"No saved category {key}",
                "category": None, "event": None, "members": []}

    if not await ctl.checkpoint(f"Opening {category['name']}"):
        return {"ok": False, "detail": "Stopped", "category": category,
                "event": None, "members": []}

    opened = await meetup.open_find(page)
    if not opened["ok"]:
        return {**opened, "category": category, "event": None, "members": []}

    # ── 1 · the city this roll searches from ─────────────────────────
    # Before the tab and the range, because changing it re-fetches the grid and
    # resets both.
    await _search_from_a_city(page, ctl)

    # ── 2 · the rolled category ──────────────────────────────────────
    # The distance filter is NOT touched — see sweep_events. The city is picked
    # deliberately now, so the radius is Meetup's own default around a place the
    # operator chose.
    result = await meetup.click_category(page, category["key"])
    if not result["ok"] or result.get("stale"):
        return {"ok": False, "category": category, "event": None, "members": [],
                "detail": f"{category['name']}: {result.get('detail')}"}

    # Only the cards this roll may actually OPEN, and only once the first one
    # exists — not the whole settled grid. The roll's job is the first event,
    # and reading twelve cards' titles, times and attendee counts for a list it
    # never picks from was a visible pause between the click and the event.
    #
    # READ AFTER the radius: widening re-fetches the grid, so cards read before
    # it are the 30km list and every one of them is about to be replaced.
    events = await meetup.cards_when_ready(page, limit=AUTO_EVENT_TRIES)
    if not events:
        return {"ok": False, "category": category, "event": None, "members": [],
                "detail": f"{category['name']} has no events right now — "
                          f"press again for another category"}

    # An event already in Data is one whose roster this app holds, and walking
    # its attendee list again is minutes of browser work to rewrite the same
    # file. So the roll takes the first event it has NOT collected: the second
    # card when the first is done, the third when the first two are.
    collected = _collected_event_keys()
    if collected and not _fresh_events(events, collected):
        # Everything read is already done. Look further down the grid ONCE
        # before giving the category up — a category is a hundred events, and
        # the five at the top being taken says nothing about the rest.
        wider = await meetup.cards_on_page(page, limit=AUTO_EVENT_POOL)
        if len(wider) > len(events):
            events = wider

    rows = [{**row, "category": category["name"], "category_key": category["key"]}
            for row in events]
    db.add_events(rows)
    db.add_groups(sorted({r["urlname"] for r in rows if r.get("urlname")}))
    # What the roll landed on, said now rather than in the result — the panel's
    # header is the context for the rows arriving under it, and a table filling
    # in beneath three em-dashes reads as somebody else's roster.
    ctl.meta["category"] = category
    ctl.meta["events_count"] = len(rows)
    ctl.advance(1, f"{category['name']}: {len(rows)} event(s)")

    # ── 3 · its first UNCOLLECTED event, and 4 · everyone on it ──────
    # The first event, as asked — but "first" now means first one not already
    # in Data. An event that hides its attendee list still has nobody to hand
    # back, so the next one down is tried after that too, and the run reports
    # which event it ended up on either way.
    fresh = _fresh_events(rows, collected)
    skipped = len(rows) - len(fresh)
    if not fresh:
        return {"ok": False, "category": category, "event": None, "members": [],
                "events": rows,
                "detail": f"{category['name']}: all {len(rows)} event(s) listed "
                          f"are already in Data — press again for another "
                          f"category"}
    activity.log("scraping", f"Rolled {category['name']} — {len(rows)} card(s) "
                             + (f"read, {skipped} already in Data, opening the "
                                f"first of the {len(fresh)} left"
                                if skipped else "read, opening the first"), "ok")

    tried = []
    stall = stall_seconds()
    for index, row in enumerate(fresh[:AUTO_EVENT_TRIES]):
        if not await ctl.checkpoint(f"Opening {row.get('title') or row.get('url')}"):
            break
        if index:
            activity.log("scraping", f"{tried[-1]} — trying the next event", "warn")
        ctl.advance(1 if index == 0 else 0, f"Opening event {index + 1}")
        ctl.meta["event"] = {"title": row.get("title") or "",
                             "when": row.get("when") or "",
                             "group": row.get("group") or row.get("urlname") or "",
                             "category": category["name"],
                             "url": row.get("url", ""),
                             "attendees": row.get("attendees") or 0}
        # The roll opens the profiles as it walks the list — that is the point
        # of a roll: one press, and what comes back is people it can message.
        #
        # Watched, same as a sweep's: an event that stops producing members is
        # given up on and the next one down is tried, which is exactly what
        # already happens to an event that hides its list. Without the clock a
        # rolled press just never comes back.
        result, _ = await run_until_stalled(
            lambda: event_members(account_id=account_id, url=row.get("url", ""),
                                  target=target, ctl=ctl),
            ctl, stall)
        if result["ok"]:
            ctl.advance(1, f"{result['count']} member(s)")
            return {"ok": True, "category": category, "event": result["event"],
                    "members": result["members"], "count": result["count"],
                    "events": rows, "event_index": index,
                    "detail": f"{category['name']} → "
                              f"{row.get('title') or 'event'} → "
                              f"{result['count']} member(s)"}
        tried.append(f"{row.get('title') or 'event'}: {result['detail']}")

    return {"ok": False, "category": category, "event": None, "members": [],
            "events": rows,
            "detail": f"{category['name']}: no attendee list on the first "
                      f"{len(tried)} event(s)"}


async def event_members(*, account_id: int | None = None, url: str = "",
                        target: int = 0, click_all: bool = False,
                        open_profiles: bool = True, tabs: int = 5,
                        ctl=None) -> dict:
    """Open one event's attendee list and take everyone on it, WITH their ids.

    Step 2→3. `target` 0 means the whole roster — this is the step the operator
    asked for explicitly on one event, not the twenty-category browse where a
    cap is what keeps a browse a browse.

    `open_profiles` is the default path and the whole shape of this step: the
    cards are opened as the list is walked, a tab-full at a time, each one read
    for its id and closed. The order it replaces — collect every name, scroll to
    the end, then go back and click them all — made the operator watch the same
    list being walked twice with a button press in between, for the same result.

    Turning it off collects names only, which is what a capped browse wants:
    there, the point is to see who is on an event, not to identify them.
    """
    url = (url or "").split("?")[0].strip()
    if not url:
        return {"ok": False, "detail": "No event URL", "members": []}

    page = await browser.current_page(sessions.profile_dir(account_id)
                                      if account_id else None)
    if not await meetup.visit.goto(page, url):
        return {"ok": False, "detail": "Event page would not load", "members": []}
    await meetup.visit.dismiss_overlays(page, meetup.SEL["overlays"])

    if not await meetup.open_attendees(page, url):
        return {"ok": False, "members": [],
                "detail": "This event does not show its attendee list"}

    if ctl is not None:
        # Live rows on the run, so the panel fills in as the walk goes rather
        # than at the end of it — TAGGED with the event they belong to, because
        # a roll that finds no attendee list moves down to the next event and
        # the abandoned event's rows must not sit under the new one's header.
        ctl.meta["members"] = []
        ctl.meta["event_url"] = url

    if open_profiles:
        members = await meetup.harvest_attendees(
            page, width=tabs, ctl=ctl, target=target,
            on_found=(lambda rows: ctl.meta.__setitem__("members", rows))
            if ctl is not None else None)
    else:
        members = await meetup.collect_attendees(page, target=target,
                                                 click_all=click_all)
    if not members:
        return {"ok": False, "detail": "No attendees on the list", "members": []}

    handles = _named(members)
    try:
        db.record_scraped_members(handles)
    except Exception as exc:  # noqa: BLE001 — MinIO down must not lose the roster
        activity.log("scraping", f"Could not store {len(handles)} member(s): "
                                 f"{type(exc).__name__}", "warn")
    # Fold them onto the stored event too, so the panel still has them after a
    # reload — same reason a browse writes what it found.
    event_id = meetup.event_ref(url)[1]
    db.add_events([{"event_id": event_id, "url": url, "members": members}])

    named = sum(1 for m in members if m.get("username"))
    # The event's own file, under its own name. Written here as well as after a
    # click pass, because a roster whose cards DID carry ids never goes through
    # one and would otherwise never be filed.
    header = _event_header(url, event_id)
    save_event_roster(header.get("title") or url, members,
                      category=header.get("category") or "")
    activity.log("scraping", f"{len(members)} member(s) on {url} — "
                             f"{named} with an id", "ok")
    return {"ok": True, "members": members, "count": len(members),
            "with_id": named,
            # The header above the table — name, date, category, how many. Read
            # back out of the store rather than echoed from the request, so it
            # says what is actually recorded against this event.
            "event": _event_header(url, event_id),
            # The id count is in the line the operator reads, because a roster
            # of twenty where three have no id is three people the batches and
            # the composer cannot touch — and that is worth saying out loud.
            "detail": f"{len(members)} member(s) collected"
                      + (f", {len(members) - named} without an id"
                         if named < len(members) else "")}


def _event_header(url: str, event_id: str = "") -> dict:
    """The stored event, as the four facts the panel's header shows."""
    wanted = url.rstrip("/")
    row = next((e for e in db.list_events()
                if (event_id and str(e.get("event_id")) == str(event_id))
                or str(e.get("url") or "").rstrip("/") == wanted), {})
    return {"title": row.get("title") or "", "when": row.get("when") or "",
            "group": row.get("group") or row.get("urlname") or "",
            "category": row.get("category") or "", "url": url,
            "attendees": row.get("attendees") or 0}


# A ceiling on the rolling window, not a suggestion. The runner opens one tab per
# member queued ahead, and a burst of parallel tabs on one session is exactly the
# shape Meetup treats as scripted — see playwright_runner.open_page, which keeps
# ordinary runs to a single tab for the same reason.
CHAT_TABS_MAX = 8

# What a member's row can say, and the whole vocabulary of it. The panel's
# badges are these names, so they live in one place rather than being spelled
# out at each site that sets one.
#
#   pending     picked, waiting its turn
#   processing  its tab is open — being loaded, or being written to right now
#   sent        the message went
#   typed       it is written and sitting in the composer, unsent, because
#               Settings → Click Send is off. Not sent and not failed: the run
#               did everything it was asked to, and the last press is a
#               person's
#   failed      it did not, and the row says why
#
# A member who was never picked has no row at all; the panel shows those as
# "Not started", which is a fact about the operator's selection rather than
# about a run, so the run does not invent a status for them.
CHAT_PENDING, CHAT_PROCESSING = "pending", "processing"
CHAT_SENT, CHAT_FAILED = "sent", "failed"
CHAT_TYPED = "typed"


def _mark_contacted(handles: list[str]) -> None:
    """Bump used_count, swallowing a member-store outage.

    The messages have already gone by the time this runs. Letting MinIO being
    down raise here would abandon the tabs still open and lose the report on
    sends that actually happened — the bookkeeping is the least valuable thing
    in the run, so it is the thing that gives way.
    """
    if not handles:
        return
    try:
        db.bump_member_uses(handles)
    except Exception as exc:  # noqa: BLE001
        activity.log("campaign", f"Could not mark {len(handles)} member(s) "
                                 f"contacted: {type(exc).__name__}", "warn")


async def _close_extra_tabs(context) -> int:
    """Close every tab except the first — the event screen the batch works from.

    The first page is the one the pipeline has been driving all along (the
    category, then the event), so it is the operator's place in the site and
    must survive. Everything after it is a profile some earlier batch opened.
    """
    closed = 0
    for page in list(context.pages[1:]):
        try:
            await page.close()
            closed += 1
        except Exception:  # noqa: BLE001 — a tab already gone is a tab closed
            pass
    return closed


def _open_window_for(account_id: int | None):
    """The open window this account's work belongs to, or None if it has none.

    With one browser per account there is no "the browser" any more: an action
    aimed at account 3 must land on account 3's window even while two others are
    up. Without an account it falls back to the active one, which is what a
    profile-less call has always meant.
    """
    live = browser.open_profiles()
    if account_id:
        wanted = sessions.profile_dir(account_id)
        return wanted if wanted in live else None
    active = browser.current_profile()
    return active if active in live else None


async def open_member_tabs(*, account_id: int | None = None,
                           handles: list[str] | None = None,
                           batch: int = 0) -> dict:
    """Open one batch of member profiles as tabs beside the open event screen.

    The manual half of step 3→4: the operator wants the five people in front of
    them to read and message by hand, not a run that writes for them. So this
    opens the tabs and stops — no chat, no send, nothing typed.

    The previous batch's tabs are closed first. Five at a time is the unit the
    panel offers; letting each press stack another five leaves a session with
    forty profile tabs open, which is both unusable and the exact shape a burst
    of scripted reading has.

    Uses whatever browser is already up rather than launching one, because the
    event screen IS the context this is run from — with nothing open there is
    no batch to run, and saying so beats opening a blank window.
    """
    handles = [str(h).strip().strip("@") for h in (handles or []) if str(h).strip()]
    if not handles:
        return {"ok": False, "detail": "No members in this batch", "opened": []}
    if len(handles) > CHAT_TABS_MAX:
        return {"ok": False, "opened": [],
                "detail": f"A batch is at most {CHAT_TABS_MAX} profiles"}

    # THIS account's window, out of however many are open — the batch belongs to
    # the account whose event screen it was started from, and with several up
    # "the browser" is not an answer.
    target = _open_window_for(account_id)
    if target is None:
        return {"ok": False, "opened": [],
                "detail": "That account's browser is not open — open it and "
                          "pick an event first."}

    context = await browser.get_context(target)
    closed = await _close_extra_tabs(context)

    opened = []
    for handle in handles:
        try:
            page = await context.new_page()
            ok = await meetup.visit.goto(page, meetup.member_url(handle))
        except Exception as exc:  # noqa: BLE001 — one dead tab is not a dead batch
            opened.append({"username": handle, "ok": False,
                           "detail": f"{type(exc).__name__}: {exc}"})
            continue
        opened.append({"username": handle, "ok": True,
                       "url": meetup.member_url(handle),
                       "detail": "" if ok else "opened, but the page was slow"})
        # A beat between tabs. Five profiles appearing in the same millisecond is
        # not something a hand does, and this is the one place the timing is free.
        await behavior.beat(behavior.SCRAPE_BATCH_PAUSE_S)

    live = [row for row in opened if row["ok"]]
    label = f"Batch {batch}" if batch else "Batch"
    detail = f"{label}: opened {len(live)} of {len(handles)} profile(s)"
    if closed:
        detail += f", closed {closed} from the last batch"
    activity.log("campaign", detail, "ok" if live else "warn")
    return {"ok": bool(live), "opened": opened, "closed": closed,
            "batch": batch, "detail": detail}


async def open_all_profiles(ctl, *, account_id: int | None = None,
                            handles: list[str] | None = None,
                            members: list[dict] | None = None,
                            event_url: str = "",
                            tabs: int = 5, dwell: bool = True) -> dict:
    """Open EVERY member's profile, a tab-full at a time.

    The step after the roster is on screen, and deliberately after: collecting
    is what fills the table, and a page load per member inside that meant the
    operator watched a blank table for a minute. Now the list appears at once
    and this walks it — open a tab-full, look at them, close them, next.

    Anyone whose row has no id is clicked on the attendee list first, since a
    profile URL cannot be built for them. That is the ONLY way their id exists.

    A run rather than a call: two hundred profiles is minutes of work, so it
    reports through the run box and takes pause and stop like everything else.
    """
    target = _open_window_for(account_id)
    if target is None:
        return {"ok": False, "opened": 0,
                "detail": "That account's browser is not open"}
    context = await browser.get_context(target)

    # The ids that are still missing come first, off the list itself. It has to
    # happen here, on the attendee page, because a card is the only door to them.
    recovered = 0
    # Names matter as much as ids here: a recovered id is matched back onto the
    # person it belongs to BY NAME, and the panel needs the pair to update its
    # table. `handles` alone is the older shape, still accepted.
    rows = [{"username": str((m or {}).get("username") or "").strip().strip("@"),
             "name": (m or {}).get("name") or ""}
            for m in (members or [])]
    if not rows:
        rows = [{"username": str(h).strip().strip("@"), "name": ""}
                for h in (handles or [])]
    if any(not row["username"] for row in rows):
        page = context.pages[0] if context.pages else await context.new_page()
        # THE list the panel reads while this runs. ctl.meta is shallow-copied
        # into every snapshot, so these rows are live: an id lands here and the
        # table has it on the next poll, rather than after the last member.
        ctl.total = len(rows)
        ctl.meta["members"] = rows
        try:
            recovered = await meetup.collect_ids(
                page, rows, width=tabs, ctl=ctl,
                on_found=lambda updated: ctl.meta.__setitem__("members", updated))
            if not recovered:
                # No context, or not on an attendee list: the older path still
                # works from wherever the browser actually is.
                recovered = await meetup.ids_by_clicking(page, rows, ctl=ctl)
        except Exception as exc:  # noqa: BLE001
            activity.log("campaign", f"Could not click the attendee list "
                                     f"({type(exc).__name__})", "warn")

    # Whatever the clicking found is worth keeping whether or not the opening
    # half goes well — it is the expensive part, and losing it would mean doing
    # every one of those page loads again.
    if recovered:
        _keep_recovered(rows, event_url)

    # Anyone the click pass just visited has HAD their profile opened — that is
    # what reading the id off it means. Opening them a second time in tabs is
    # the same page load again for no new information, and on a roster where
    # nobody had an id that doubled the whole run.
    queue = [row["username"] for row in rows
             if row["username"] and not row.get("clicked")]
    if not queue:
        # Nothing more to open — so nothing this run opened should still be up.
        await _close_extra_tabs(context)
        detail = (f"Clicked {recovered} attendee(s) — ids stored" if recovered
                  else "No members with a profile to open")
        activity.log("campaign", detail, "ok" if recovered else "warn")
        return {"ok": bool(recovered), "opened": recovered,
                "recovered": recovered, "members": rows, "detail": detail}

    width = max(1, min(int(tabs or 1), CHAT_TABS_MAX))
    groups = [queue[i:i + width] for i in range(0, len(queue), width)]
    ctl.total = len(queue)
    ctl.meta["tabs"] = width

    opened, stopped = 0, False
    for index, group in enumerate(groups):
        if not await ctl.checkpoint(f"Profiles {index + 1}/{len(groups)}"):
            stopped = True
            break
        # The last tab-full goes before the next opens — the same rule the chat
        # runner keeps, and for the same reason: a session with forty profile
        # tabs open is not one anybody is browsing.
        await _close_extra_tabs(context)
        live = []
        for handle in group:
            try:
                page = await context.new_page()
                await meetup.visit.goto(page, meetup.member_url(handle))
                live.append(page)
                opened += 1
            except Exception as exc:  # noqa: BLE001 — one dead tab, not a dead run
                activity.log("campaign", f"@{handle}: {type(exc).__name__}", "warn")
            await behavior.beat(behavior.SCRAPE_BATCH_PAUSE_S)
        ctl.advance(len(group), f"{opened} opened")
        # Long enough to have been looked at. Skipped when the caller is only
        # after the ids.
        if dwell and live:
            await behavior.beat(behavior.ACTION_DWELL_S)

    await _close_extra_tabs(context)
    detail = f"Opened {opened} of {len(queue)} profile(s)"
    if recovered:
        detail += f", {recovered} id(s) recovered by clicking"
    if stopped:
        detail += " — stopped"
    activity.log("campaign", detail, "ok" if opened else "warn")
    # `members` is the point for the panel: the rows it sent back, now carrying
    # the ids the clicking found, so its table stops saying "no profile link"
    # for people this run has just identified.
    return {"ok": bool(opened), "opened": opened, "recovered": recovered,
            "total": len(queue), "members": rows, "detail": detail}


def _keep_recovered(rows: list[dict], event_url: str = "") -> None:
    """Store the ids a click pass found — the member store, the event, and the
    event's own roster file."""
    handles = _named(rows)
    if not handles:
        return
    try:
        db.record_scraped_members(handles)
    except Exception as exc:  # noqa: BLE001 — MinIO down must not lose the run
        activity.log("campaign", f"Could not store {len(handles)} member(s): "
                                 f"{type(exc).__name__}", "warn")
    url = (event_url or "").split("?")[0].strip()
    if not url:
        return
    event_id = meetup.event_ref(url)[1]
    db.add_events([{"event_id": event_id, "url": url, "members": rows}])
    header = _event_header(url, event_id)
    save_event_roster(header.get("title") or url, rows,
                      category=header.get("category") or "")


def save_event_roster(event: str, members: list[dict],
                      category: str = "") -> dict:
    """Write one event's members — name and id — to their own file in MinIO.

    The members table answers "who have we ever seen"; this answers "who was on
    this event", which is the unit the operator actually works in and the one
    thing a table keyed by handle cannot give back.

    `category` is the Find tab it came off, kept on the file so the Data panel
    can say where a roster came from without joining back to an event store that
    is cleared at every boot.

    Best-effort by design: it is a convenience copy of what is already in the
    member store and the event, so a MinIO outage must not fail the run that
    collected them.
    """
    named = [m for m in (members or []) if (m or {}).get("username")]
    if not named:
        return {"ok": False, "members": 0, "detail": "No members with an id"}
    try:
        from services.storage import event_roster_storage as rosters

        result = rosters.save(event, named, category=category)
    except Exception as exc:  # noqa: BLE001
        activity.log("scraping", f"Could not write the roster file: "
                                 f"{type(exc).__name__}", "warn")
        return {"ok": False, "members": 0, "detail": str(exc)}
    if result.get("ok"):
        activity.log("scraping", f"Roster: {result['detail']}", "ok")
    return result


async def close_member_tabs(*, account_id: int | None = None) -> dict:
    """Close the profile tabs a batch left open, keeping the event screen.

    Scoped to one account's window: with several open, closing "the tabs" must
    not reach into another account's browser.
    """
    target = _open_window_for(account_id)
    if target is None:
        return {"ok": True, "closed": 0, "detail": "No browser is open"}
    context = await browser.get_context(target)
    closed = await _close_extra_tabs(context)
    return {"ok": True, "closed": closed,
            "detail": f"Closed {closed} profile tab(s)" if closed
                      else "No profile tabs were open"}


async def chat_members(ctl, *, account_id: int | None = None,
                       handles: list[str] | None = None, text: str = "",
                       tabs: int = 5, image: str | None = None,
                       event_url: str = "") -> dict:
    """Message a picked set of members, a tab-full at a time.

    Step 3→4, and the shape the operator asked for: open `tabs` member profiles
    in their own tabs, work them one at a time — profile, Chat, write, send —
    then close ALL of them before the next tab-full opens. The batch is the unit,
    so the session shows a burst of reading followed by a gap, rather than a
    permanently-open set of tabs churning one at a time.

    The SENDS stay sequential even though the tabs are opened together. Loading a
    few profiles at once is ordinary browsing; two composers submitting at the
    same moment from one account is not, and the tabs are there to have the next
    profiles ready, not to send faster.

    Live progress rides in `ctl.meta["members"]` — one row per member carrying
    its status (pending → opening → processing → sent/failed) and the text it
    gets. The panel polls the task anyway, so the table it draws is the run's own
    state rather than a guess reconstructed from a percentage.
    """
    handles = [str(h).strip().strip("@") for h in (handles or []) if str(h).strip()]
    if not handles:
        return {"sent": 0, "detail": "No members picked", "results": []}
    text = (text or "").strip()
    if not text:
        return {"sent": 0, "detail": "Nothing to send — write the message first",
                "results": []}

    width = max(1, min(int(tabs or 1), CHAT_TABS_MAX))
    context = await browser.get_context(sessions.profile_dir(account_id)
                                        if account_id else None)

    # THE list the panel renders. Mutated in place as the run goes — ctl.meta is
    # shallow-copied into every snapshot, so each row's status is live.
    rows = [{"username": handle, "status": CHAT_PENDING, "detail": "",
             "stage": "", "text": text} for handle in handles]
    ctl.total = len(rows)
    ctl.meta["members"] = rows
    ctl.meta["tabs"] = width

    sent, contacted, stopped = 0, [], False
    batches = [rows[i:i + width] for i in range(0, len(rows), width)]

    async def _close(page) -> None:
        try:
            await page.close()
        except Exception:  # noqa: BLE001 — a tab that is already gone is fine
            pass

    def _land(row: dict, ok: bool, detail: str, stage: str = "",
              status: str | None = None) -> None:
        row["status"] = status or (CHAT_SENT if ok else CHAT_FAILED)
        row["detail"] = detail
        row["stage"] = stage
        activity.log("campaign", f"@{row['username']}: {detail or 'no detail'}",
                     "ok" if ok else "warn")

    for index, batch in enumerate(batches):
        if not await ctl.checkpoint():
            stopped = True
            break
        activity.log("campaign", f"Tab-full {index + 1} of {len(batches)} — "
                                 f"opening {len(batch)} profile(s)", "info")

        opened: list[tuple[dict, object]] = []
        for row in batch:
            # Processing from the moment its tab opens. The tab IS the work
            # starting, and splitting "loading" from "writing" gave the panel two
            # badges for one state nobody can act on differently.
            row["status"] = CHAT_PROCESSING
            try:
                page = await context.new_page()
                await meetup.visit.goto(page, meetup.member_url(row["username"]))
            except Exception as exc:  # noqa: BLE001
                _land(row, False, f"{type(exc).__name__}: {exc}", "profile")
                ctl.advance(1, f"{sent} sent")
                continue
            opened.append((row, page))

        try:
            for row, page in opened:
                if not await ctl.checkpoint(f"Chatting @{row['username']}"):
                    stopped = True
                    break
                try:
                    result = await meetup.chat_with_member(page, row["username"],
                                                           text, image=image)
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "stage": "send",
                              "detail": f"{type(exc).__name__}: {exc}"}
                ok = bool(result.get("ok"))
                # Written but not sent, because Click Send is off. Its own row
                # state: green would claim a message that is still sitting in
                # the composer, red would call a working step a failure.
                went = ok and result.get("sent", True)
                _land(row, ok, result.get("detail", ""), result.get("stage", ""),
                      status=None if went else (CHAT_TYPED if ok else None))
                if went:
                    sent += 1
                    contacted.append(row["username"])
                ctl.advance(1, f"{sent} sent")

                if len(contacted) >= _BATCH:
                    _mark_contacted(contacted)
                    contacted = []

                # Between people, not between clicks — the pacing that matters
                # here is how fast one account starts new conversations.
                await human.pause()
        finally:
            # All of them together: the tab-full is the unit, and a stop must not
            # leave four profiles sitting open on the session.
            for _, page in opened:
                await _close(page)

        if stopped:
            break

    _mark_contacted(contacted)

    # A row still pending is one the run never reached — a stop, not a failure,
    # and calling it failed would misreport what happened to it.
    results = [{"username": r["username"], "ok": r["status"] == CHAT_SENT,
                "status": r["status"], "stage": r["stage"],
                "detail": r["detail"]} for r in rows]
    failed = [r for r in results if r["status"] == CHAT_FAILED]
    typed = [r for r in results if r["status"] == CHAT_TYPED]
    skipped = [r for r in results
               if r["status"] not in (CHAT_SENT, CHAT_FAILED, CHAT_TYPED)]

    detail = f"{sent}/{len(rows)} messaged"
    if typed:
        detail += f", {len(typed)} written but not sent"
    if failed:
        detail += f", {len(failed)} failed"
    if skipped:
        detail += f", {len(skipped)} not reached"
    db.save_run({"kind": "chat", "sent": sent, "failed": len(failed),
                 "event": event_url or None})
    activity.log("campaign", detail, "ok" if sent else "warn")
    return {"sent": sent, "failed": len(failed), "skipped": len(skipped),
            "results": results, "detail": detail}


async def reply_to_first_chat(*, account_id: int | None = None, text: str = "",
                              image: str | None = None,
                              login: bool = True) -> dict:
    """Log in if needed, open the inbox, and reply in the conversation on top.

    Not a `ctl` run: this is one message to one thread, so there is nothing to
    pause between and nothing to resume. It returns the same {ok, detail} shape
    the panel renders for every other action.

    `login` is what makes this usable straight from the panel — the inbox is
    behind the login wall, and a stored session is reused rather than re-entered,
    because every fresh login is a chance for Meetup to ask for a code.
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "detail": "Nothing to send — write the message first"}

    if login and account_id:
        from backend import accounts

        signed_in = await accounts.login_account(account_id)
        # A skipped login means the stored session was still good, which is the
        # outcome we want; only a real failure stops the send.
        if not (signed_in.get("ok") or signed_in.get("skipped")):
            return {"ok": False, "stage": "login",
                    "detail": f"Login failed: {signed_in.get('detail')}"}

    page = await browser.open_page(profile=sessions.profile_dir(account_id)
                                   if account_id else None)
    try:
        return await meetup.message_first_chat(page, text, image=image)
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"
        activity.log("campaign", f"First-chat reply failed — {detail}", "error")
        return {"ok": False, "detail": detail, "stage": "send"}


async def rsvp_events(ctl, *, account_id: int | None = None,
                      event_urls: list[str] | None = None) -> dict:
    """RSVP to a list of events.

    Capped by `max_rsvps_per_run` and never inferred from a scrape: an RSVP is a
    public commitment to attend something, so the events are always ones the
    operator named.
    """
    values = app_settings.current()
    urls = (event_urls or [])[:values["max_rsvps_per_run"]]
    if not urls:
        return {"rsvps": 0, "detail": "No events given"}

    page = await browser.open_page(profile=sessions.profile_dir(account_id)
                                   if account_id else None)
    ctl.total = len(urls)
    done, failed = 0, []
    for url in urls:
        if not await ctl.checkpoint(f"RSVP {url}"):
            break
        try:
            result = await meetup.rsvp(page, url)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        if result.get("ok"):
            done += 1
        else:
            failed.append(f"{url}: {result.get('detail')}")
        ctl.advance(1)
        await human.pause()

    detail = f"{done}/{len(urls)} RSVP'd"
    db.save_run({"kind": "rsvp", "rsvps": done, "failed": len(failed)})
    activity.log("campaign", detail, "ok" if done else "warn")
    return {"rsvps": done, "failed": failed, "detail": detail}
