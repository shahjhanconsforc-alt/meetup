"""The Find pipeline: category → one event → its members → chat each of them.

Four operator choices, not one long run — so the pieces are tested the way they
are used: `open_category` hands back events, `event_members` hands back a
roster, and `chat_members` works that roster a few tabs at a time.

The tab window is the part with real behaviour worth pinning. Profiles load in
parallel because that is ordinary browsing; the SENDS stay one at a time,
because two composers submitting at once from one account is not.
"""
from __future__ import annotations

import asyncio

import pytest

from backend import db
from backend.automation import campaign, meetup
from tests.test_first_chat import FakePage


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def _instant(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(meetup.asyncio, "sleep", _instant)
    monkeypatch.setattr(meetup.human, "think", _instant)
    monkeypatch.setattr(meetup.human, "pause", _instant)
    monkeypatch.setattr(meetup.human, "before_submit", _instant)
    monkeypatch.setattr(campaign.human, "pause", _instant)
    monkeypatch.setattr(meetup, "_INBOX_WAIT_S", 0.2)


@pytest.fixture(autouse=True)
def member_store(monkeypatch):
    """The member store lives in MinIO, which is not running in a test. Stubbed
    in memory rather than skipped: whether a messaged member is marked contacted
    is part of what the pipeline does."""
    # handle -> {"name": display name, "used": how many times contacted}. The
    # NAME is stored because the real store keeps one: a scrape hands over rows,
    # not bare handles, and "did the name survive the trip" is a thing these
    # tests have to be able to ask.
    store: dict[str, dict] = {}

    def _record(rows):
        added = []
        for row in rows:
            # Bare handles are still a real caller — the routes post a list of
            # usernames — so the double takes both shapes, like db does.
            handle = row["username"] if isinstance(row, dict) else row
            name = row.get("name", "") if isinstance(row, dict) else ""
            if handle not in store:
                store[handle] = {"name": name, "used": 0}
                added.append(handle)
            elif name and not store[handle]["name"]:
                store[handle]["name"] = name
        return {"added": added,
                "skipped": [r["username"] if isinstance(r, dict) else r
                            for r in rows
                            if (r["username"] if isinstance(r, dict) else r)
                            not in added]}

    def _bump(names):
        for name in names:
            store.setdefault(name, {"name": "", "used": 0})["used"] += 1
        return {"bumped": len(names)}

    monkeypatch.setattr(db, "record_scraped_members", _record)
    monkeypatch.setattr(db, "bump_member_uses", _bump)
    monkeypatch.setattr(db, "list_members", lambda: sorted(store))
    monkeypatch.setattr(db, "list_scraped_members",
                        lambda: [{"username": n, "name": row["name"],
                                  "used_count": row["used"]}
                                 for n, row in store.items()])
    return store


class Ctl:
    """A control.Run stand-in: counts the checkpoints, never stops."""

    def __init__(self, stop_after: int | None = None):
        self.total = 0
        self.done = 0
        self.meta: dict = {}
        self.labels: list[str] = []
        self.stop_after = stop_after
        self.seen = 0

    async def checkpoint(self, label=None):
        self.seen += 1
        if label:
            self.labels.append(label)
        return self.stop_after is None or self.seen <= self.stop_after

    def advance(self, n, label=None):
        self.done += n


# ── Step 4: the tab window ───────────────────────────────────────────────

class FakeTab:
    def __init__(self, context, url=""):
        self.context = context
        self.url = url
        self.closed = False

    async def close(self):
        self.closed = True
        self.context.open_now.discard(self)


class TabContext:
    """A browser context that records how many tabs are open AT ONCE."""

    def __init__(self):
        self.opened: list[FakeTab] = []
        self.open_now: set = set()
        self.high_water = 0

    async def new_page(self):
        tab = FakeTab(self)
        self.opened.append(tab)
        self.open_now.add(tab)
        self.high_water = max(self.high_water, len(self.open_now))
        return tab


@pytest.fixture
def chat_stubs(monkeypatch):
    """A context that hands out fake tabs, and a chat that always succeeds."""
    context = TabContext()
    log = {"gotos": [], "sent": [], "open_when_sending": []}

    async def _context(profile=None):
        return context

    async def _goto(page, url, **kwargs):
        log["gotos"].append(url)
        page.url = url
        return True

    async def _chat(page, handle, text, image=None):
        # How many tabs were open at the moment of the send — the number the
        # sequential-send rule is about.
        log["open_when_sending"].append(len(context.open_now))
        log["sent"].append((handle, text))
        return {"ok": True, "detail": f"Messaged {handle}", "stage": "send"}

    monkeypatch.setattr(campaign.browser, "get_context", _context)
    monkeypatch.setattr(campaign.meetup.visit, "goto", _goto)
    monkeypatch.setattr(campaign.meetup, "chat_with_member", _chat)
    return {"context": context, "log": log}


def run_chat(handles, **kwargs):
    ctl = kwargs.pop("ctl", None) or Ctl()
    result = asyncio.run(campaign.chat_members(
        ctl, handles=handles, text=kwargs.pop("text", "hello"), **kwargs))
    return result, ctl


def test_every_picked_member_is_messaged(chat_stubs):
    result, _ = run_chat(["alice", "bob", "carol"])

    assert result["sent"] == 3
    assert [h for h, _ in chat_stubs["log"]["sent"]] == ["alice", "bob", "carol"]
    assert "3/3 messaged" in result["detail"]


def test_each_member_is_opened_on_their_own_profile(chat_stubs):
    run_chat(["alice", "bob"])
    assert chat_stubs["log"]["gotos"] == [
        meetup.member_url("alice"), meetup.member_url("bob")]


def test_no_more_tabs_are_open_than_one_tab_full(chat_stubs):
    """Five picked with a width of two must never put five tabs on one session —
    a burst of parallel tabs is the shape Meetup reads as scripted."""
    run_chat(["a", "b", "c", "d", "e"], tabs=2)
    assert chat_stubs["context"].high_water == 2


def test_every_tab_is_closed_by_the_end(chat_stubs):
    run_chat(["a", "b", "c"], tabs=2)
    assert all(tab.closed for tab in chat_stubs["context"].opened)


def test_one_tab_per_member_and_no_more(chat_stubs):
    run_chat(["a", "b", "c", "d"], tabs=3)
    assert len(chat_stubs["context"].opened) == 4


def test_a_tab_full_is_opened_together_and_closed_together(chat_stubs):
    """THE batch. Open five, work all five, close all five, THEN open the next
    five — not a rolling window that keeps one tab churning."""
    run_chat(["a", "b", "c", "d", "e"], tabs=2)
    # Two open while the first of a pair sends, one while its partner does (the
    # earlier tab is only closed once the whole pair is done — so the count
    # falls to 1 on the last member of an odd tail, never mid-pair).
    assert chat_stubs["log"]["open_when_sending"] == [2, 2, 2, 2, 1]


def test_the_next_tab_full_only_opens_once_the_last_one_is_closed(chat_stubs):
    """The batch boundary: no tab from batch two exists while a tab from batch
    one is still open."""
    context = chat_stubs["context"]
    run_chat(["a", "b", "c", "d"], tabs=2)

    first, second = context.opened[:2], context.opened[2:]
    # Every tab of the first pair was closed before either of the second pair
    # was created — which the high-water mark of 2 over four members proves.
    assert context.high_water == 2
    assert all(tab.closed for tab in first + second)


def test_the_sends_stay_sequential(chat_stubs):
    """Tabs open together; messages do not. The recorded order IS the picked
    order, which a parallel send could not guarantee."""
    run_chat(["a", "b", "c", "d"], tabs=4)
    assert [h for h, _ in chat_stubs["log"]["sent"]] == ["a", "b", "c", "d"]


def test_a_member_who_cannot_be_messaged_does_not_stop_the_rest(chat_stubs, monkeypatch):
    async def _chat(page, handle, text, image=None):
        if handle == "bob":
            return {"ok": False, "detail": "Member cannot be messaged",
                    "stage": "chat"}
        return {"ok": True, "detail": f"Messaged {handle}", "stage": "send"}

    monkeypatch.setattr(campaign.meetup, "chat_with_member", _chat)
    result, _ = run_chat(["alice", "bob", "carol"])

    assert result["sent"] == 2
    assert result["failed"] == 1
    by_handle = {r["username"]: r for r in result["results"]}
    assert by_handle["bob"]["ok"] is False
    assert by_handle["bob"]["stage"] == "chat"
    assert by_handle["carol"]["ok"] is True          # the queue kept going


def test_a_thrown_error_is_reported_not_raised(chat_stubs, monkeypatch):
    async def _boom(page, handle, text, image=None):
        raise RuntimeError("browser died")

    monkeypatch.setattr(campaign.meetup, "chat_with_member", _boom)
    result, _ = run_chat(["alice"])

    assert result["sent"] == 0
    assert "RuntimeError" in result["results"][0]["detail"]


def test_a_stop_leaves_no_tab_behind(chat_stubs):
    """Cooperative stop: the member being worked finishes, the ones queued in
    their tabs are closed rather than left open on the session."""
    ctl = Ctl(stop_after=2)
    result, _ = run_chat(["a", "b", "c", "d"], tabs=3, ctl=ctl)

    assert result["sent"] < 4
    assert all(tab.closed for tab in chat_stubs["context"].opened)
    assert chat_stubs["context"].open_now == set()


def test_the_window_is_capped_however_many_tabs_are_asked_for(chat_stubs):
    run_chat([f"m{n}" for n in range(12)], tabs=99)
    assert chat_stubs["context"].high_water == campaign.CHAT_TABS_MAX


def test_nothing_is_opened_for_an_empty_pick(chat_stubs):
    result, _ = run_chat([])
    assert result["sent"] == 0
    assert chat_stubs["context"].opened == []
    assert "No members picked" in result["detail"]


def test_an_empty_message_sends_nothing(chat_stubs):
    result, _ = run_chat(["alice"], text="   ")
    assert result["sent"] == 0
    assert chat_stubs["context"].opened == []
    assert "write the message first" in result["detail"].lower()


# ── What the table reads: one live row per member ────────────────────────

def test_the_run_publishes_a_row_per_member(chat_stubs):
    """The panel's table IS this list — polled off the run rather than guessed
    at from a percentage."""
    _, ctl = run_chat(["alice", "bob"], text="hello there")
    rows = ctl.meta["members"]

    assert [r["username"] for r in rows] == ["alice", "bob"]
    assert all(r["text"] == "hello there" for r in rows)   # the SMS column
    assert [r["status"] for r in rows] == ["sent", "sent"]


def test_a_member_reads_as_processing_while_they_are_being_worked(chat_stubs, monkeypatch):
    """The status the operator watches. Captured DURING the send, which is the
    only moment it is true.

    A whole tab-full reads Processing at once — the tab being open IS the work
    starting, and the panel showed two badges for one state nobody can act on
    differently. Members in a LATER tab-full are still Pending.
    """
    seen = {}
    ctl = Ctl()

    async def _chat(page, handle, text, image=None):
        seen[handle] = [dict(r) for r in ctl.meta["members"]]
        return {"ok": True, "detail": "Messaged", "stage": "send"}

    monkeypatch.setattr(campaign.meetup, "chat_with_member", _chat)
    run_chat(["alice", "bob", "carol"], tabs=2, ctl=ctl)

    during = {r["username"]: r["status"] for r in seen["alice"]}
    assert during["alice"] == "processing"
    assert during["bob"] == "processing"      # same tab-full, tab already open
    assert during["carol"] == "pending"       # next tab-full, not opened yet


def test_the_statuses_are_the_four_the_panel_draws(chat_stubs, monkeypatch):
    """The panel's badges ARE these names. A fifth one invented here would
    render as a blank pill."""
    async def _chat(page, handle, text, image=None):
        return {"ok": handle == "alice", "detail": "…", "stage": "send"}

    monkeypatch.setattr(campaign.meetup, "chat_with_member", _chat)
    ctl = Ctl(stop_after=3)
    run_chat(["alice", "bob", "carol", "dave"], tabs=1, ctl=ctl)

    allowed = {campaign.CHAT_PENDING, campaign.CHAT_PROCESSING,
               campaign.CHAT_SENT, campaign.CHAT_FAILED}
    assert {r["status"] for r in ctl.meta["members"]} <= allowed


def test_a_member_the_run_never_reached_stays_pending(chat_stubs):
    """A stop is not a failure. Reporting the untouched members as failed would
    say four messages were refused when four were never attempted."""
    ctl = Ctl(stop_after=2)
    result, _ = run_chat(["a", "b", "c", "d"], tabs=2, ctl=ctl)

    statuses = [r["status"] for r in ctl.meta["members"]]
    assert "pending" in statuses
    assert result["skipped"] >= 1
    assert "not reached" in result["detail"]


def test_a_failure_carries_its_reason_into_the_row(chat_stubs, monkeypatch):
    async def _chat(page, handle, text, image=None):
        return {"ok": False, "detail": "Member cannot be messaged", "stage": "chat"}

    monkeypatch.setattr(campaign.meetup, "chat_with_member", _chat)
    _, ctl = run_chat(["alice"])

    row = ctl.meta["members"][0]
    assert row["status"] == "failed"
    assert row["detail"] == "Member cannot be messaged"
    assert row["stage"] == "chat"


def test_the_tab_width_is_published_for_the_panel(chat_stubs):
    _, ctl = run_chat(["a", "b", "c"], tabs=2)
    assert ctl.meta["tabs"] == 2


def test_messaged_members_are_marked_used(chat_stubs):
    db.record_scraped_members(["alice", "bob"])
    run_chat(["alice", "bob"])

    used = {row["username"]: row["used_count"] for row in db.list_scraped_members()}
    assert used["alice"] == 1
    assert used["bob"] == 1


# ── Step 4, one member: profile → Chat → send ────────────────────────────

@pytest.fixture
def profile_stubs(monkeypatch):
    """A pointer that follows the control it is given, and navigation."""
    log = {"clicks": [], "gotos": []}

    async def _goto(page, url, **kwargs):
        log["gotos"].append(url)
        page.url = url
        return True

    async def _dismiss(page, selectors):
        return 0

    class _Pointer:
        def __init__(self, page):
            self.page = page

        async def click(self, locator, *, swift=False):
            element = locator._elements[0] if locator._elements else {}
            log["clicks"].append(element.get("_what", "?"))
            if element.get("on_click_url"):
                self.page.url = element["on_click_url"]
            hook = element.get("on_click")
            if hook:
                hook()
            return True

    monkeypatch.setattr(meetup.visit, "goto", _goto)
    monkeypatch.setattr(meetup.visit, "dismiss_overlays", _dismiss)
    monkeypatch.setattr(meetup, "Pointer", _Pointer)
    return log


def profile_page(*, chat_selector="a[href*='/messages/new']", composer=True):
    """A member profile carrying a Chat control, and a composer behind it."""
    page = FakePage({}, url="https://www.meetup.com/members/alice/")
    if chat_selector:
        page.matches[chat_selector] = [{
            "visible": True, "_what": "chat",
            "on_click_url": "https://www.meetup.com/messages/new/?member=alice",
        }]
    if composer:
        page.matches["message_box"] = [{"visible": True, "value": ""}]
        page.matches["message_send"] = [{"visible": True, "data-disabled": "false"}]
    return page


def test_the_profile_s_own_chat_control_is_what_gets_clicked(profile_stubs):
    """The door a person uses. send_message()'s composer URL reaches the same
    place, but typing an address is not what opening a chat looks like."""
    page = profile_page()
    result = asyncio.run(meetup.open_chat(page, "alice"))

    assert result["ok"] is True
    assert result["via"] == "button"
    assert profile_stubs["clicks"] == ["chat"]
    assert profile_stubs["gotos"] == []              # no address typed


def test_a_profile_with_no_chat_control_falls_back_to_the_composer(profile_stubs):
    """And the address it falls back to is Settings → chat_link, not a URL
    frozen into this module: a build that repoints campaigns at a different
    composer must repoint this too, or the fallback quietly goes somewhere the
    operator has stopped using."""
    page = profile_page(chat_selector=None)
    result = asyncio.run(meetup.open_chat(page, "alice"))

    assert result["ok"] is True
    assert result["via"] == "route"
    assert profile_stubs["gotos"] == [meetup.chat_link("alice")]


def test_a_member_who_cannot_be_messaged_is_reported(profile_stubs):
    """Privacy settings, or no shared group — a normal outcome, not an error."""
    page = profile_page(chat_selector=None, composer=False)
    result = asyncio.run(meetup.open_chat(page, "alice"))

    assert result["ok"] is False
    assert "cannot be messaged" in result["detail"]


def test_a_page_already_on_a_composer_is_left_alone(profile_stubs):
    page = profile_page()
    page.url = "https://www.meetup.com/messages/new/?member=alice"
    result = asyncio.run(meetup.open_chat(page, "alice"))

    assert result["via"] == "already"
    assert profile_stubs["clicks"] == []
    assert profile_stubs["gotos"] == []


def test_chat_with_member_walks_profile_then_chat_then_send(profile_stubs, monkeypatch):
    sent = {}

    async def _send(page, text, image=None):
        sent["text"] = text
        return {"ok": True, "detail": "Message sent"}

    monkeypatch.setattr(meetup, "send_in_open_chat", _send)
    page = FakePage({}, url="https://www.meetup.com/")
    page.matches["a[href*='/messages/new']"] = [{
        "visible": True, "_what": "chat",
        "on_click_url": "https://www.meetup.com/messages/new/?member=alice"}]

    result = asyncio.run(meetup.chat_with_member(page, "alice", "hi there"))

    assert result["ok"] is True
    assert result["stage"] == "send"
    assert sent["text"] == "hi there"
    assert profile_stubs["gotos"][0] == meetup.member_url("alice")


def test_chat_with_member_reports_which_step_stopped_it(profile_stubs):
    """A dead profile, a member who cannot be messaged and a composer that
    refused the text are three different failures — the stage says which."""
    page = FakePage({}, url="https://www.meetup.com/")
    result = asyncio.run(meetup.chat_with_member(page, "alice", "hi"))

    assert result["ok"] is False
    assert result["stage"] == "chat"


def test_an_at_prefixed_handle_is_accepted(profile_stubs, monkeypatch):
    async def _send(page, text, image=None):
        return {"ok": True, "detail": "Message sent"}

    monkeypatch.setattr(meetup, "send_in_open_chat", _send)
    page = profile_page()
    page.url = "https://www.meetup.com/"

    result = asyncio.run(meetup.chat_with_member(page, "@alice", "hi"))

    assert result["handle"] == "alice"
    assert profile_stubs["gotos"][0] == meetup.member_url("alice")


# ── Steps 1 and 2: the category's events, the event's members ────────────

@pytest.fixture
def find_stubs(monkeypatch):
    """Stub the DOM side of steps 1-2; the ORDER and what gets stored is what
    these tests are about."""
    state = {
        "categories": [{"key": "652", "name": "Social Activities"}],
        "clicked": [],
        "cards": [{"event_id": "315749481", "urlname": "htb-lahore",
                   "title": "MITRE ATT&CK", "url":
                   "https://www.meetup.com/htb-lahore/events/315749481/"}],
        "click_result": {"ok": True, "detail": "Opened", "cards": 1, "stale": False},
    }

    async def _current(profile=None):
        return FakePage({}, url=meetup.FIND_URL)

    async def _open_find(page):
        return {"ok": True, "detail": "Find page open"}

    async def _list(page):
        return state["categories"]

    async def _click(page, key):
        state["clicked"].append(key)
        return state["click_result"]

    async def _cards(page, **kwargs):
        return list(state["cards"])

    monkeypatch.setattr(campaign.browser, "current_page", _current)
    monkeypatch.setattr(campaign.meetup, "open_find", _open_find)
    monkeypatch.setattr(campaign.meetup, "list_categories", _list)
    monkeypatch.setattr(campaign.meetup, "click_category", _click)
    # Two readers, on purpose: a manual pick hands the operator a list to choose
    # from and waits for the settled grid; a roll opens the first card and only
    # reads the few it may actually open.
    monkeypatch.setattr(campaign.meetup, "cards_when_settled", _cards)
    monkeypatch.setattr(campaign.meetup, "cards_when_ready", _cards)
    return state


def test_the_event_list_waits_for_the_grid_to_stop_growing(monkeypatch):
    """THE list of one.

    The results render in stages. Reading the grid the instant the category
    swapped returned its first card and nothing else — which is not a list
    anyone can pick from: it looks like the app chose the event for you.
    """
    grid = {"n": 0}
    # 1 card, then 3, then 3 again, then 3 — it has to wait for the plateau.
    sizes = [1, 3, 3, 3, 3, 3]

    async def _cards(page, limit=200):
        size = sizes[min(grid["n"], len(sizes) - 1)]
        grid["n"] += 1
        return [{"event_id": str(i)} for i in range(size)]

    monkeypatch.setattr(meetup, "cards_on_page", _cards)
    rows = asyncio.run(meetup.cards_when_settled(FakePage({}), budget=2.0))

    assert len(rows) == 3


def test_a_grid_that_never_stops_growing_still_returns(monkeypatch):
    """Bounded: a page that keeps lazy-loading must not hold the step open."""
    grid = {"n": 1}

    async def _cards(page, limit=200):
        grid["n"] += 1
        return [{"event_id": str(i)} for i in range(grid["n"])]

    monkeypatch.setattr(meetup, "cards_on_page", _cards)
    rows = asyncio.run(meetup.cards_when_settled(FakePage({}), budget=0.5))

    assert rows                      # whatever it had when the budget ran out


# ── The roll: random category → first event → its members ────────────────

@pytest.fixture
def auto_stubs(find_stubs, member_stubs, monkeypatch):
    """Steps 1-3 stubbed at the DOM edge. What is under test is which category
    gets rolled, which event is taken, and what happens when either is a dud."""
    find_stubs["categories"] = [
        {"key": "all", "name": "All events"},
        {"key": "-999", "name": "New Groups"},
        {"key": "652", "name": "Social Activities"},
        {"key": "405", "name": "Technology"},
    ]
    find_stubs["cards"] = [
        {"event_id": "1", "urlname": "htb-lahore", "title": "First event",
         "url": "https://www.meetup.com/htb-lahore/events/1/"},
        {"event_id": "2", "urlname": "htb-lahore", "title": "Second event",
         "url": "https://www.meetup.com/htb-lahore/events/2/"},
    ]
    # campaign.event_members is called for real; its DOM half is member_stubs.
    monkeypatch.setattr(campaign.behavior, "shuffled", lambda rows: list(rows))
    # What is already in Data, stubbed: the real one reads the roster bucket,
    # and a test whose outcome depends on what the operator collected last week
    # is a test that fails on somebody else's machine. Nothing collected is the
    # default, so "which event does a roll take" is asked of an empty Data.
    collected: set[str] = set()
    monkeypatch.setattr(campaign, "_collected_event_keys", lambda: set(collected))
    return {"find": find_stubs, "members": member_stubs, "collected": collected}


def test_the_roll_never_lands_on_a_tab_that_is_not_a_category(auto_stubs):
    """"All events", "New Groups" and "From your Groups" sit on the same strip
    and are not categories — rolling across the lot would land on All events
    often, and every roll would look the same."""
    result = asyncio.run(campaign.auto_pipeline(Ctl()))

    assert result["category"]["key"] == "652"          # the first REAL one
    assert auto_stubs["find"]["clicked"] == ["652"]


def test_the_strip_is_read_once_and_saved(auto_stubs):
    """The tabs are the same twenty every time. Re-reading the page per roll is
    a page load to rediscover a list that has not changed."""
    asyncio.run(campaign.auto_pipeline(Ctl()))

    saved = db.list_find_categories()
    assert [c["key"] for c in saved] == ["652", "405"]     # the real ones only


def test_a_later_roll_uses_the_saved_strip_instead_of_the_page(auto_stubs):
    listed = {"n": 0}
    original = campaign.meetup.list_categories

    async def _counted(page):
        listed["n"] += 1
        return await original(page)

    campaign.meetup.list_categories = _counted
    try:
        asyncio.run(campaign.auto_pipeline(Ctl()))
        asyncio.run(campaign.auto_pipeline(Ctl()))
    finally:
        campaign.meetup.list_categories = original

    assert listed["n"] == 1                       # the second roll read nothing


def test_one_press_opens_exactly_one_category(auto_stubs, monkeypatch):
    """No walking. An earlier version tried the next category when a roll came
    back empty, which is a tour of the taxonomy by another name — how many
    categories get opened is the operator's call, one press at a time."""
    async def _cards(page, **kwargs):
        return []

    monkeypatch.setattr(campaign.meetup, "cards_when_ready", _cards)
    result = asyncio.run(campaign.auto_pipeline(Ctl()))

    assert len(auto_stubs["find"]["clicked"]) == 1
    assert result["ok"] is False
    assert "no events right now" in result["detail"]
    assert result["category"]["name"] == "Social Activities"   # says which


def test_a_named_category_can_be_rolled_on_purpose(auto_stubs):
    """The key is honoured when given — the roll is the default, not the only
    way in."""
    result = asyncio.run(campaign.auto_pipeline(Ctl(), key="405"))

    assert auto_stubs["find"]["clicked"] == ["405"]
    assert result["category"]["name"] == "Technology"


def test_saving_the_strip_keeps_only_the_real_categories(auto_stubs):
    result = asyncio.run(campaign.save_categories())

    assert [c["name"] for c in result["categories"]] == [
        "Social Activities", "Technology"]
    assert len(db.list_find_categories()) == 2


def test_saving_again_replaces_rather_than_piles_up(auto_stubs):
    asyncio.run(campaign.save_categories())
    auto_stubs["find"]["categories"] = [{"key": "652", "name": "Social Activities"}]
    asyncio.run(campaign.save_categories())

    assert [c["key"] for c in db.list_find_categories()] == ["652"]


def test_the_roll_takes_the_first_event_and_all_of_its_members(auto_stubs):
    result = asyncio.run(campaign.auto_pipeline(Ctl()))

    assert result["ok"] is True
    assert result["event"]["title"] == "First event"
    assert result["event_index"] == 0
    assert [m["username"] for m in result["members"]] == ["alice", "bob"]


def test_a_roll_leaves_the_distance_filter_alone(auto_stubs, monkeypatch):
    """It used to widen the range to its maximum before picking an event, from
    back when a run searched from wherever the profile happened to be pointed
    and 30km around that was too small to sweep.

    The city is chosen deliberately now, so the radius is Meetup's own default
    around a place the operator picked — which is a more ordinary-looking search
    than one set to the whole country."""
    touched = []

    async def _widen(page):
        touched.append("distance")
        return {"ok": True, "label": "Any distance", "detail": "widened"}

    monkeypatch.setattr(campaign.meetup, "widen_distance", _widen)

    result = asyncio.run(campaign.auto_pipeline(Ctl()))

    assert touched == []
    assert result["ok"] is True
    assert result["event"]["title"] == "First event"


def test_an_event_already_in_data_is_skipped_for_the_next_one(auto_stubs):
    """The roll takes the first event it has NOT collected. Walking a roster
    this app already holds is minutes of browser work to rewrite the same file,
    and the operator pressed the button to get someone new."""
    auto_stubs["collected"].add("first-event")

    result = asyncio.run(campaign.auto_pipeline(Ctl()))

    assert result["ok"] is True
    assert result["event"]["title"] == "Second event"
    assert result["event_index"] == 0          # first of the ones left, not of all


def test_a_category_with_nothing_left_to_collect_says_so(auto_stubs):
    """Every event on it is already in Data. That is an outcome worth naming —
    silently re-walking the first one is the exact work this avoids."""
    auto_stubs["collected"].update({"first-event", "second-event"})

    result = asyncio.run(campaign.auto_pipeline(Ctl()))

    assert result["ok"] is False
    assert result["members"] == []
    assert "already in Data" in result["detail"]
    assert "press again" in result["detail"]


def test_nothing_collected_yet_leaves_the_roll_exactly_as_it_was(auto_stubs):
    """The skip is a filter over an empty set on a fresh install, not a new
    code path the first roll has to survive."""
    result = asyncio.run(campaign.auto_pipeline(Ctl()))

    assert result["event"]["title"] == "First event"


def test_a_storage_outage_answers_nothing_collected(monkeypatch):
    """MinIO being down must not cost the press. Re-walking one event is
    minutes; refusing to roll is the whole button — so a dead bucket reads as
    an empty one and the roll goes ahead unfiltered."""
    from services.storage import event_roster_storage as rosters

    def _boom(*args, **kwargs):
        raise RuntimeError("minio is down")

    monkeypatch.setattr(rosters, "listing", _boom)

    assert campaign._collected_event_keys() == set()


def test_what_counts_as_already_collected_is_the_rosters_own_filing_name(monkeypatch):
    """Matched on the roster key, not the URL: the same meetup runs weekly
    under one title and one file, so both places have to agree on what "this
    event" means."""
    rows = [{"title": "Coffee & Coloring Date!", "url": "https://m/1/"},
            {"title": "Guided Discussion", "url": "https://m/2/"}]

    fresh = campaign._fresh_events(rows, {"coffee-coloring-date"})

    assert [r["title"] for r in fresh] == ["Guided Discussion"]


def test_an_empty_category_is_reported_not_papered_over(auto_stubs, monkeypatch):
    """An empty category is an ordinary outcome of a roll, and the reply names
    it — so pressing again is an informed choice rather than a shrug."""
    async def _cards(page, **kwargs):
        return []

    monkeypatch.setattr(campaign.meetup, "cards_when_ready", _cards)
    result = asyncio.run(campaign.auto_pipeline(Ctl()))

    assert result["ok"] is False
    assert result["members"] == []
    assert "press again" in result["detail"]


def test_an_event_that_hides_its_attendees_falls_through_to_the_next(auto_stubs, monkeypatch):
    """The FIRST event, as asked — but an event with no attendee list has nobody
    to hand back, and answering a request for members with an empty table helps
    no one. The reply says which event it ended up on."""
    seen = []

    async def _open_attendees(page, url):
        seen.append(url)
        return len(seen) > 1                       # the first one hides them

    monkeypatch.setattr(campaign.meetup, "open_attendees", _open_attendees)
    result = asyncio.run(campaign.auto_pipeline(Ctl()))

    assert result["ok"] is True
    assert result["event_index"] == 1
    assert result["event"]["url"].endswith("/events/2/")


def test_a_category_whose_events_all_hide_their_attendees_is_reported(auto_stubs, monkeypatch):
    async def _open_attendees(page, url):
        return False

    monkeypatch.setattr(campaign.meetup, "open_attendees", _open_attendees)
    result = asyncio.run(campaign.auto_pipeline(Ctl()))

    assert result["ok"] is False
    assert result["members"] == []
    assert "no attendee list" in result["detail"]


def test_the_roll_stops_when_the_run_is_stopped(auto_stubs):
    result = asyncio.run(campaign.auto_pipeline(Ctl(stop_after=0)))
    assert result["ok"] is False
    assert auto_stubs["find"]["clicked"] == []


def test_a_find_page_with_no_strip_is_reported(auto_stubs):
    auto_stubs["find"]["categories"] = []
    result = asyncio.run(campaign.auto_pipeline(Ctl()))

    assert result["ok"] is False
    assert "No category tabs" in result["detail"]


def test_the_rolled_events_are_stored_under_their_category(auto_stubs):
    asyncio.run(campaign.auto_pipeline(Ctl()))

    stored = {e["event_id"]: e for e in db.list_events()}
    assert stored["1"]["category"] == "Social Activities"
    assert {g["urlname"] for g in db.list_groups()} == {"htb-lahore"}


def test_picking_a_category_clicks_its_tab_and_returns_its_events(find_stubs):
    result = asyncio.run(campaign.open_category(key="652"))

    assert find_stubs["clicked"] == ["652"]
    assert result["name"] == "Social Activities"
    assert [e["title"] for e in result["events"]] == ["MITRE ATT&CK"]


def test_those_events_are_stored_under_that_category(find_stubs):
    asyncio.run(campaign.open_category(key="652"))

    stored = db.list_events()[0]
    assert stored["category"] == "Social Activities"
    assert stored["category_key"] == "652"
    assert {g["urlname"] for g in db.list_groups()} == {"htb-lahore"}


def test_an_unknown_category_key_is_reported(find_stubs):
    result = asyncio.run(campaign.open_category(key="nope"))
    assert result["ok"] is False
    assert "No category tab" in result["detail"]


def test_a_category_whose_grid_never_changed_returns_no_events(find_stubs):
    """The stale grid again: its cards belong to the category before it, and
    handing them back here would let the operator message the wrong event's
    attendees."""
    find_stubs["click_result"] = {"ok": True, "detail": "never changed",
                                  "cards": 1, "stale": True}
    result = asyncio.run(campaign.open_category(key="652"))

    assert result["ok"] is False
    assert result["events"] == []
    assert db.list_events() == []


@pytest.fixture
def member_stubs(monkeypatch):
    state = {"attendees_ok": True, "members": [
        {"username": "alice", "name": "Alice"}, {"username": "bob", "name": "Bob"}]}

    async def _current(profile=None):
        return FakePage({}, url="https://www.meetup.com/")

    async def _goto(page, url, **kwargs):
        page.url = url
        return True

    async def _dismiss(page, selectors):
        return 0

    async def _open_attendees(page, url):
        return state["attendees_ok"]

    async def _collect(page, target=40, **kwargs):
        state["collect_kwargs"] = kwargs      # click_all and friends land here
        return list(state["members"])

    async def _harvest(page, **kwargs):
        # The default path: the list is walked and each card opened as it
        # appears, so this hands back the same rows the walk would have.
        state["harvest_kwargs"] = kwargs
        rows = list(state["members"])
        found = kwargs.get("on_found")
        if found is not None:
            found(rows)
        return rows

    monkeypatch.setattr(campaign.browser, "current_page", _current)
    monkeypatch.setattr(campaign.meetup.visit, "goto", _goto)
    monkeypatch.setattr(campaign.meetup.visit, "dismiss_overlays", _dismiss)
    monkeypatch.setattr(campaign.meetup, "open_attendees", _open_attendees)
    monkeypatch.setattr(campaign.meetup, "collect_attendees", _collect)
    monkeypatch.setattr(campaign.meetup, "harvest_attendees", _harvest)
    return state


EVENT_URL = "https://www.meetup.com/htb-lahore/events/315749481/"


def test_an_event_s_members_are_collected_and_returned(member_stubs):
    result = asyncio.run(campaign.event_members(url=EVENT_URL))

    assert result["count"] == 2
    assert [m["username"] for m in result["members"]] == ["alice", "bob"]


def test_those_members_land_in_the_member_store(member_stubs):
    asyncio.run(campaign.event_members(url=EVENT_URL))
    assert set(db.list_members()) == {"alice", "bob"}


def test_the_members_are_folded_onto_the_stored_event(member_stubs):
    """So the panel still has them after a reload — the same reason a browse
    writes what it found rather than only returning it."""
    db.add_events([{"event_id": "315749481", "url": EVENT_URL,
                    "title": "MITRE ATT&CK", "category": "Social Activities"}])
    asyncio.run(campaign.event_members(url=EVENT_URL))

    stored = db.list_events()
    assert len(stored) == 1                      # updated, not duplicated
    assert [m["username"] for m in stored[0]["members"]] == ["alice", "bob"]
    assert stored[0]["category"] == "Social Activities"


def test_the_header_facts_come_back_with_the_roster(member_stubs):
    """The strip above the table — category, event, date, how many — read out of
    the store rather than echoed from the request, so it says what is actually
    recorded against this event."""
    db.add_events([{"event_id": "315749481", "url": EVENT_URL,
                    "title": "MITRE ATT&CK", "group": "HTB Lahore",
                    "when": "2026-08-21T16:00:00+05:00[Asia/Karachi]",
                    "category": "Technology"}])

    header = asyncio.run(campaign.event_members(url=EVENT_URL))["event"]

    assert header["title"] == "MITRE ATT&CK"
    assert header["category"] == "Technology"
    assert header["group"] == "HTB Lahore"
    assert header["when"].startswith("2026-08-21")


def test_the_header_is_empty_for_an_event_nothing_is_stored_about(member_stubs):
    """Reached by URL without going through a category — the roster still
    comes back, the header just has nothing to say."""
    header = asyncio.run(campaign.event_members(url=EVENT_URL))["event"]
    assert header["title"] == ""
    assert header["url"] == EVENT_URL


def test_an_event_that_hides_its_attendees_is_reported(member_stubs):
    member_stubs["attendees_ok"] = False
    result = asyncio.run(campaign.event_members(url=EVENT_URL))

    assert result["ok"] is False
    assert "does not show its attendee list" in result["detail"]
    assert result["members"] == []


def test_an_event_with_nobody_on_the_list_is_reported(member_stubs):
    member_stubs["members"] = []
    result = asyncio.run(campaign.event_members(url=EVENT_URL))

    assert result["ok"] is False
    assert "No attendees" in result["detail"]


def test_the_tracking_query_is_dropped_from_the_event_url(member_stubs):
    asyncio.run(campaign.event_members(url=EVENT_URL + "?recId=abc"))
    assert db.list_events()[0]["url"] == EVENT_URL


def test_no_url_is_reported_rather_than_navigating(member_stubs):
    result = asyncio.run(campaign.event_members(url=""))
    assert result["ok"] is False
    assert "No event URL" in result["detail"]


# ── The batch button: five profiles as tabs, nothing sent ────────────────

class BatchTab:
    def __init__(self, context, url=""):
        self.context = context
        self.url = url
        self.closed = False

    async def close(self):
        self.closed = True
        if self in self.context.pages:
            self.context.pages.remove(self)


class BatchContext:
    """A browser already showing an event. `pages[0]` is that screen — the thing
    a batch must never take away."""

    def __init__(self):
        self.event = BatchTab(self, url=EVENT_URL)
        self.pages = [self.event]
        self.opened: list[BatchTab] = []

    async def new_page(self):
        tab = BatchTab(self)
        self.pages.append(tab)
        self.opened.append(tab)
        return tab


@pytest.fixture
def batch_stubs(monkeypatch):
    context = BatchContext()
    log = {"gotos": [], "open": True}
    # One window among however many are open. The batch has to find THIS one.
    profile = campaign.sessions.profile_dir(1)

    def _open_profiles():
        return [profile] if log["open"] else []

    def _current_profile():
        return profile if log["open"] else None

    async def _context(profile=None):
        return context

    async def _goto(page, url, **kwargs):
        log["gotos"].append(url)
        page.url = url
        return True

    async def _beat(*args, **kwargs):
        return 0.0

    async def _never_chat(*args, **kwargs):
        raise AssertionError("a batch must not send anything")

    monkeypatch.setattr(campaign.browser, "open_profiles", _open_profiles)
    monkeypatch.setattr(campaign.browser, "current_profile", _current_profile)
    monkeypatch.setattr(campaign.browser, "get_context", _context)
    monkeypatch.setattr(campaign.meetup.visit, "goto", _goto)
    monkeypatch.setattr(campaign.behavior, "beat", _beat)
    monkeypatch.setattr(campaign.meetup, "chat_with_member", _never_chat)
    return {"context": context, "log": log}


def run_batch(handles, **kwargs):
    return asyncio.run(campaign.open_member_tabs(handles=handles, **kwargs))


def test_a_batch_opens_a_tab_per_member(batch_stubs):
    result = run_batch(["a", "b", "c", "d", "e"], batch=1)

    assert result["ok"] is True
    assert len(batch_stubs["context"].opened) == 5
    assert batch_stubs["log"]["gotos"] == [
        meetup.member_url(h) for h in ["a", "b", "c", "d", "e"]]
    assert [r["username"] for r in result["opened"]] == ["a", "b", "c", "d", "e"]


def test_the_event_screen_is_left_alone(batch_stubs):
    """The batch works FROM the event screen — closing it would take the
    operator's place in the site away with it."""
    context = batch_stubs["context"]
    run_batch(["a", "b"], batch=1)

    assert context.pages[0] is context.event
    assert context.event.closed is False


def test_the_next_batch_closes_the_last_one_s_tabs(batch_stubs):
    """Five at a time is the unit. Stacking presses would leave forty profile
    tabs open — unusable, and the exact shape scripted reading has."""
    context = batch_stubs["context"]
    run_batch(["a", "b", "c", "d", "e"], batch=1)
    first = list(context.opened)
    result = run_batch(["f", "g", "h", "i", "j"], batch=2)

    assert all(tab.closed for tab in first)
    assert result["closed"] == 5
    assert len(context.pages) == 6                 # the event screen + five
    assert all(not tab.closed for tab in context.opened[5:])


def test_closing_the_tabs_keeps_the_event_screen(batch_stubs):
    context = batch_stubs["context"]
    run_batch(["a", "b"], batch=1)
    result = asyncio.run(campaign.close_member_tabs())

    assert result["closed"] == 2
    assert context.pages == [context.event]


def test_a_batch_needs_a_browser_that_is_already_open(batch_stubs):
    """No window, no event screen to run the batch from — and opening a blank
    one would look like the button worked."""
    batch_stubs["log"]["open"] = False
    result = run_batch(["a", "b"], batch=1)

    assert result["ok"] is False
    assert "not open" in result["detail"]
    assert batch_stubs["context"].opened == []


def test_a_batch_is_capped_at_the_tab_ceiling(batch_stubs):
    result = run_batch([f"m{n}" for n in range(campaign.CHAT_TABS_MAX + 1)])

    assert result["ok"] is False
    assert batch_stubs["context"].opened == []


def test_an_empty_batch_opens_nothing(batch_stubs):
    result = run_batch([])

    assert result["ok"] is False
    assert batch_stubs["context"].opened == []


def test_one_dead_tab_does_not_end_the_batch(batch_stubs, monkeypatch):
    async def _goto(page, url, **kwargs):
        if "bob" in url:
            raise RuntimeError("tab died")
        page.url = url
        return True

    monkeypatch.setattr(campaign.meetup.visit, "goto", _goto)
    result = run_batch(["alice", "bob", "carol"], batch=1)

    by_handle = {r["username"]: r for r in result["opened"]}
    assert by_handle["bob"]["ok"] is False
    assert by_handle["carol"]["ok"] is True
    assert result["ok"] is True


def test_the_step_walks_the_list_opening_each_profile(member_stubs):
    """THE shape of this step. Reading the list IS opening the profiles: the
    cards are opened as the walk goes, a tab-full at a time, rather than every
    name being collected first and clicked through afterwards on a button."""
    asyncio.run(campaign.event_members(url=EVENT_URL, tabs=5))

    assert member_stubs["harvest_kwargs"]["width"] == 5
    assert "collect_kwargs" not in member_stubs        # no separate name pass


def test_names_only_is_still_available_for_a_capped_read(member_stubs):
    """A browse wants to see who is on an event, not to identify them — a page
    load per attendee inside a twenty-category sweep is not a browse."""
    asyncio.run(campaign.event_members(url=EVENT_URL, open_profiles=False,
                                       click_all=True))

    assert member_stubs["collect_kwargs"]["click_all"] is True
    assert "harvest_kwargs" not in member_stubs


def test_the_rows_are_published_while_the_walk_runs(member_stubs):
    """The panel fills in as the list is walked — forty page loads with nothing
    on screen until the end is not a thing anybody can watch."""
    ctl = Ctl()
    asyncio.run(campaign.event_members(url=EVENT_URL, ctl=ctl))

    assert [m["name"] for m in ctl.meta["members"]] == ["Alice", "Bob"]


def test_members_without_an_id_are_counted_in_the_result(member_stubs):
    """Three of twenty with no id is three people the batches and the composer
    cannot touch — the line the operator reads has to say so."""
    member_stubs["members"] = [{"username": "alice", "name": "Alice"},
                               {"username": "", "name": "Co-host"}]
    result = asyncio.run(campaign.event_members(url=EVENT_URL))

    assert result["with_id"] == 1
    assert "1 without an id" in result["detail"]


# ── A run starts from an empty store ─────────────────────────────────────
# The panel is meant to show what THIS browse found. Keeping the last run's
# cards meant it opened holding events from a session the operator did not
# remember starting, filed under a category this run never visited.

def test_a_roll_clears_the_events_the_last_run_left(member_stubs, monkeypatch):
    db.add_events([{"event_id": "1", "url": "https://www.meetup.com/g/events/1/",
                    "title": "Stale", "category": "Movements & Politics"}])
    assert db.list_events()

    async def _sweep(*args, **kwargs):
        return {"ok": False, "detail": "no categories", "categories": []}

    monkeypatch.setattr(campaign, "save_categories", _sweep)
    asyncio.run(campaign.auto_pipeline(Ctl(), target=0))

    assert db.list_events() == []


def test_keep_existing_leaves_them_alone(member_stubs, monkeypatch):
    db.add_events([{"event_id": "1", "url": "https://www.meetup.com/g/events/1/",
                    "title": "Kept"}])

    async def _sweep(*args, **kwargs):
        return {"ok": False, "detail": "no categories", "categories": []}

    monkeypatch.setattr(campaign, "save_categories", _sweep)
    asyncio.run(campaign.auto_pipeline(Ctl(), target=0, keep_existing=True))

    assert [e["title"] for e in db.list_events()] == ["Kept"]


# ── Opening every profile, after the roster is on screen ─────────────────

@pytest.fixture
def open_all_stubs(monkeypatch):
    """A window with an event screen open, and tabs that record what they load."""
    context = BatchContext()
    log = {"gotos": [], "clicked": []}
    profile = campaign.sessions.profile_dir(1)

    async def _context(p=None):
        return context

    async def _goto(page, url, **kwargs):
        log["gotos"].append(url)
        page.url = url
        return True

    async def _beat(*args, **kwargs):
        return 0.0

    async def _click(page, rows, ctl=None, limit=0, force=False, **kwargs):
        # The click pass: everyone with no id gets one, and is marked as having
        # had their profile opened — which is what the real _fill_id does, and
        # what stops the run opening the same profile a second time.
        filled = 0
        for row in rows:
            if not row.get("username"):
                filled += 1
                row["username"] = f"clicked-{filled}"
                row["clicked"] = True
                log["clicked"].append(filled)
        return filled

    monkeypatch.setattr(campaign.browser, "open_profiles", lambda: [profile])
    monkeypatch.setattr(campaign.browser, "current_profile", lambda: profile)
    monkeypatch.setattr(campaign.browser, "get_context", _context)
    monkeypatch.setattr(campaign.meetup.visit, "goto", _goto)
    monkeypatch.setattr(campaign.behavior, "beat", _beat)
    monkeypatch.setattr(campaign.meetup, "ids_by_clicking", _click)
    return {"context": context, "log": log}


def run_open_all(handles, **kwargs):
    ctl = kwargs.pop("ctl", None) or Ctl()
    result = asyncio.run(campaign.open_all_profiles(ctl, handles=handles, **kwargs))
    return result, ctl


def test_every_member_gets_their_profile_opened(open_all_stubs):
    result, _ = run_open_all([f"m{n}" for n in range(12)], tabs=5)

    assert result["opened"] == 12
    assert open_all_stubs["log"]["gotos"] == [
        meetup.member_url(f"m{n}") for n in range(12)]


def test_the_profiles_open_a_tab_full_at_a_time(open_all_stubs):
    """Never twelve tabs at once: the last tab-full is closed before the next
    opens, the same rule the chat runner keeps."""
    context = open_all_stubs["context"]
    run_open_all([f"m{n}" for n in range(12)], tabs=5)

    # The event screen survives, and nothing is left open at the end.
    assert context.pages == [context.event]
    assert len(context.opened) == 12


def test_members_with_no_id_are_clicked_off_the_list_first(open_all_stubs):
    """A profile URL cannot be built for them — the card is the only door.

    And only the one that already HAD an id gets a tab: the other two had their
    profiles opened by the click that produced their ids, and opening those a
    second time is the same page load for nothing.
    """
    result, _ = run_open_all(["alice", "", ""], tabs=5)

    assert result["recovered"] == 2
    assert result["opened"] == 1
    assert open_all_stubs["log"]["gotos"] == [meetup.member_url("alice")]


def test_a_stop_ends_the_open_run(open_all_stubs):
    ctl = Ctl(stop_after=1)
    result, _ = run_open_all([f"m{n}" for n in range(20)], tabs=5, ctl=ctl)

    assert result["opened"] < 20
    assert "stopped" in result["detail"]
    assert open_all_stubs["context"].pages == [open_all_stubs["context"].event]


def test_a_roster_with_no_ids_at_all_says_so(open_all_stubs, monkeypatch):
    async def _no_click(page, rows, **kwargs):
        return 0

    monkeypatch.setattr(campaign.meetup, "ids_by_clicking", _no_click)
    result, _ = run_open_all(["", ""], tabs=5)

    assert result["ok"] is False
    assert "No members with a profile" in result["detail"]


def test_the_roll_never_reads_the_whole_grid(auto_stubs, monkeypatch):
    """The pause the operator was watching: a settled read of every card sat
    between the category click and the event opening. The roll asks for the few
    cards it may open and nothing else."""
    asked = {}

    async def _ready(page, limit=5, **kwargs):
        asked["limit"] = limit
        return list(auto_stubs["find"]["cards"])

    async def _settled(page, **kwargs):
        raise AssertionError("the roll must not wait for the settled grid")

    monkeypatch.setattr(campaign.meetup, "cards_when_ready", _ready)
    monkeypatch.setattr(campaign.meetup, "cards_when_settled", _settled)
    result = asyncio.run(campaign.auto_pipeline(Ctl()))

    assert result["ok"] is True
    assert asked["limit"] == campaign.AUTO_EVENT_TRIES


# ── The sweep: one tab, every event on it, one after another ─────────────
# The roll's opposite. It stays on the tab it started on — All events — and
# works down the list rather than opening a random category and taking one
# event out of it.

@pytest.fixture
def sweep_stubs(auto_stubs, monkeypatch):
    """The grid, as a lazy-loading list of cards: `pages` is what each read
    returns in turn, so a scroll is what produces the next screenful."""
    # `collected` is auto_stubs' set of rosters already on file, re-exposed here
    # so a sweep test can say "this one is already scraped" without reaching for
    # a second fixture to do it.
    state = {"pages": [], "reads": 0, "scrolls": 0, "clicked": [],
             "opened": [], "back": 0, "collected": auto_stubs["collected"]}

    async def _cards(page, limit=200, **kwargs):
        state["reads"] += 1
        # Everything loaded so far, which is what a real grid hands back after
        # it has been scrolled — not just the newest screenful.
        rows = []
        for batch in state["pages"][:state["scrolls"] + 1]:
            rows.extend(batch)
        return rows[:limit]

    async def _scroll(page, pointer=None, **kwargs):
        state["scrolls"] += 1

    async def _click(page, key):
        state["clicked"].append(key)
        return {"ok": True, "detail": "Opened", "cards": 1, "stale": False}

    async def _open_find(page):
        state["back"] += 1
        return {"ok": True, "detail": "Find page open"}

    async def _widen(page):
        return {"ok": True, "label": "150 kilometers", "detail": "Distance widened"}

    original = campaign.event_members

    async def _members(**kwargs):
        state["opened"].append(kwargs.get("url"))
        return await original(**kwargs)

    monkeypatch.setattr(campaign.meetup, "cards_on_page", _cards)
    monkeypatch.setattr(campaign.meetup.visit, "scroll_step", _scroll)
    monkeypatch.setattr(campaign.meetup, "click_category", _click)
    monkeypatch.setattr(campaign.meetup, "open_find", _open_find)
    monkeypatch.setattr(campaign.meetup, "widen_distance", _widen)
    monkeypatch.setattr(campaign, "event_members", _members)
    return state


def card(n: int) -> dict:
    return {"event_id": str(n), "urlname": "g", "title": f"Event {n}",
            "url": f"https://www.meetup.com/g/events/{n}/"}


def test_the_sweep_opens_every_event_on_the_tab_in_order(sweep_stubs):
    sweep_stubs["pages"] = [[card(1), card(2), card(3)]]

    result = asyncio.run(campaign.sweep_events(Ctl()))

    assert [u.rsplit("/", 2)[1] for u in sweep_stubs["opened"]] == ["1", "2", "3"]
    assert result["events"] == 3
    assert result["count"] == 6                  # two members on each


def test_the_sweep_stays_on_the_tab_it_started_on(sweep_stubs):
    """THE difference from the roll: no second category, ever. It comes back to
    the same tab between events and works down it."""
    sweep_stubs["pages"] = [[card(1), card(2)]]

    asyncio.run(campaign.sweep_events(Ctl()))

    assert set(sweep_stubs["clicked"]) == {campaign.SWEEP_TAB}
    assert sweep_stubs["clicked"][0] == "all"    # All events, not a category


def test_the_sweep_scrolls_for_more_when_the_screen_runs_out(sweep_stubs):
    """The grid lazy-loads, so "nothing new on screen" is a scroll away from
    being wrong. Events that arrive on the second screenful are taken too."""
    sweep_stubs["pages"] = [[card(1)], [card(2)], [card(3)]]

    result = asyncio.run(campaign.sweep_events(Ctl()))

    assert result["events"] == 3
    assert sweep_stubs["scrolls"] >= 2


def test_the_sweep_ends_when_scrolling_stops_producing_events(sweep_stubs):
    """It has to end by itself — a non-stop run with no stop condition is one
    that never returns.

    A dry tab is a RESTART now rather than the end, so what ends it is
    SWEEP_RESTARTS passes in a row with nothing to show for them: three scrolls
    each, plus the pass that actually took the one event there was."""
    sweep_stubs["pages"] = [[card(1)]]

    result = asyncio.run(campaign.sweep_events(Ctl()))

    assert result["events"] == 1
    assert sweep_stubs["scrolls"] == \
        campaign.SWEEP_SCROLLS * (campaign.SWEEP_RESTARTS + 1)


def test_the_sweep_never_takes_the_same_event_twice(sweep_stubs):
    """Coming back to the tab puts the grid at the top again, so the second
    pass reads the events the first one already took."""
    sweep_stubs["pages"] = [[card(1), card(2)], [card(3)]]

    asyncio.run(campaign.sweep_events(Ctl()))

    assert sorted(sweep_stubs["opened"]) == sorted(set(sweep_stubs["opened"]))


def test_the_sweep_skips_events_already_in_data(sweep_stubs, auto_stubs):
    auto_stubs["collected"].add("event-2")
    sweep_stubs["pages"] = [[card(1), card(2), card(3)]]

    asyncio.run(campaign.sweep_events(Ctl()))

    assert [u.rsplit("/", 2)[1] for u in sweep_stubs["opened"]] == ["1", "3"]


def test_a_stop_lands_between_two_events(sweep_stubs):
    """Cooperative: the roster it is on finishes, and the next one is not
    started. A sweep killed mid-roster would lose the ids it had just read."""
    sweep_stubs["pages"] = [[card(n) for n in range(1, 6)]]

    result = asyncio.run(campaign.sweep_events(Ctl(stop_after=3)))

    assert result["events"] < 5
    assert len(sweep_stubs["opened"]) < 5


def test_a_sweep_leaves_the_distance_filter_alone(sweep_stubs):
    """Same as the roll — see test_a_roll_leaves_the_distance_filter_alone. The
    run publishes no distance either, because it did not set one."""
    sweep_stubs["pages"] = [[card(1)]]
    ctl = Ctl()

    asyncio.run(campaign.sweep_events(ctl))

    assert "distance" not in ctl.meta


def test_the_run_hands_back_the_ids_it_recovered(open_all_stubs):
    """The panel's table has no id to match on — that is why the run exists —
    so the rows come back carrying both name and id for it to fold in."""
    result, _ = run_open_all([], members=[{"username": "", "name": "Colleen"},
                                          {"username": "", "name": "Aliya"}])

    by_name = {r["name"]: r["username"] for r in result["members"]}
    assert by_name["Colleen"] and by_name["Aliya"]
    assert result["recovered"] == 2


def test_recovered_ids_are_stored_even_though_the_opening_follows(open_all_stubs):
    """The clicking is the expensive half. Losing it because the opening went
    wrong would mean doing every one of those page loads again."""
    run_open_all([], members=[{"username": "", "name": "Colleen"}],
                 event_url=EVENT_URL)

    assert db.list_members()                      # in the member store
    stored = {e["url"]: e for e in db.list_events()}
    assert stored[EVENT_URL]["members"][0]["username"]


def test_no_tab_is_left_open_when_the_run_only_clicked(open_all_stubs):
    """Everyone was identified by clicking, so there is nothing left to open —
    and nothing this run opened should still be up."""
    context = open_all_stubs["context"]
    result, _ = run_open_all([], members=[{"username": "", "name": "Colleen"},
                                          {"username": "", "name": "Aliya"}])

    assert result["ok"] is True
    assert context.pages == [context.event]


def test_a_member_already_identified_is_not_opened_twice(open_all_stubs):
    """Reading the id off a profile IS opening it. Opening it again in a tab is
    the same page load for no new information."""
    run_open_all([], members=[{"username": "", "name": "Colleen"}])
    assert open_all_stubs["log"]["gotos"] == []      # no second pass


# ── Sweeping from several accounts at once ───────────────────────────────
# One LANE per ticked account: a browser window each, all working down the SAME
# tab, taking events off one shared queue. The point is that the tab is scraped
# in a fraction of the time AND no single account opened every event on it.


def test_every_event_is_walked_exactly_once_across_the_accounts(sweep_stubs):
    """THE risk of a shared queue. Two accounts walking the same event is
    minutes of browser work each to write the same file twice."""
    sweep_stubs["pages"] = [[card(n) for n in range(1, 9)]]

    result = asyncio.run(campaign.sweep_events(Ctl(), account_ids=[7, 8]))

    opened = sweep_stubs["opened"]
    assert sorted(opened) == sorted(set(opened))       # nobody twice
    assert len(opened) == 8
    assert result["events"] == 8


def test_the_work_is_actually_shared_between_them(sweep_stubs, monkeypatch):
    """Not one account doing all of it while the other watches: each lane opens
    its own window and takes whatever is unclaimed when it looks."""
    profiles = []
    monkeypatch.setattr(campaign.sessions, "profile_dir",
                        lambda i: profiles.append(i) or f"profile-{i}")
    sweep_stubs["pages"] = [[card(n) for n in range(1, 7)]]

    asyncio.run(campaign.sweep_events(Ctl(), account_ids=[7, 8]))

    assert sorted(set(profiles)) == [7, 8]             # both windows were used


def test_one_account_still_sweeps_as_one_lane(sweep_stubs):
    sweep_stubs["pages"] = [[card(n) for n in range(1, 4)]]

    result = asyncio.run(campaign.sweep_events(Ctl(), account_ids=[7]))

    assert result["events"] == 3
    assert len(sweep_stubs["opened"]) == 3


def test_no_account_at_all_is_still_one_lane(sweep_stubs):
    """The older shape — a sweep with nothing ticked drives the live window."""
    sweep_stubs["pages"] = [[card(1), card(2)]]

    result = asyncio.run(campaign.sweep_events(Ctl()))

    assert result["events"] == 2


def test_a_stop_ends_every_lane(sweep_stubs):
    """One button over the whole sweep, not one per window."""
    sweep_stubs["pages"] = [[card(n) for n in range(1, 21)]]

    result = asyncio.run(campaign.sweep_events(Ctl(stop_after=4),
                                               account_ids=[7, 8]))

    assert result["events"] < 20
    assert len(sweep_stubs["opened"]) < 20


# ── The scraped-events list ──────────────────────────────────────────────
# Published as each event FINISHES, so the panel shows the run's history rather
# than only whatever it happens to be on.


def test_each_finished_event_lands_on_the_list(sweep_stubs):
    sweep_stubs["pages"] = [[card(1), card(2), card(3)]]
    ctl = Ctl()

    asyncio.run(campaign.sweep_events(ctl))

    done = ctl.meta["events_done"]
    assert [r["title"] for r in done] == ["Event 3", "Event 2", "Event 1"]


def test_the_newest_event_is_first(sweep_stubs):
    """A list that grows downward puts the row that just landed off the bottom
    of the box after a dozen."""
    sweep_stubs["pages"] = [[card(n) for n in range(1, 6)]]
    ctl = Ctl()

    asyncio.run(campaign.sweep_events(ctl))

    assert ctl.meta["events_done"][0]["title"] == "Event 5"


def test_a_row_carries_its_member_count(sweep_stubs):
    """The number the operator is watching — an event walked for two minutes
    that hands back nothing is worth seeing as zero."""
    sweep_stubs["pages"] = [[card(1)]]
    ctl = Ctl()

    asyncio.run(campaign.sweep_events(ctl))

    row = ctl.meta["events_done"][0]
    assert row["count"] == 2                      # the stub roster's two members
    assert row["url"].endswith("/events/1/")


def test_a_row_names_the_account_that_walked_it(sweep_stubs, monkeypatch):
    """With several sweeping the same tab, which of them did what is only
    answerable here."""
    monkeypatch.setattr(campaign, "account_label",
                        lambda a: {7: "Tom", 8: "Nick"}.get(a) or "—")
    sweep_stubs["pages"] = [[card(n) for n in range(1, 5)]]
    ctl = Ctl()

    asyncio.run(campaign.sweep_events(ctl, account_ids=[7, 8]))

    assert {r["account"] for r in ctl.meta["events_done"]} <= {"Tom", "Nick"}


def test_an_event_that_gave_nothing_is_not_listed_as_done(sweep_stubs,
                                                          monkeypatch):
    """The list is what was COLLECTED. An event whose attendee list is hidden
    was opened and walked away from, and a row claiming otherwise would make a
    sweep look more productive than it was."""
    async def _nothing(**kwargs):
        sweep_stubs["opened"].append(kwargs.get("url"))
        return {"ok": False, "detail": "This event does not show its attendee "
                                       "list", "members": []}

    monkeypatch.setattr(campaign, "event_members", _nothing)
    sweep_stubs["pages"] = [[card(1), card(2)]]
    ctl = Ctl()

    asyncio.run(campaign.sweep_events(ctl))

    assert ctl.meta["events_done"] == []


def test_the_finished_list_comes_back_with_the_result(sweep_stubs):
    """A run that ends between two polls would otherwise leave the panel one
    event short of what it actually did."""
    sweep_stubs["pages"] = [[card(1), card(2)]]

    result = asyncio.run(campaign.sweep_events(Ctl()))

    assert [r["title"] for r in result["events_done"]] == ["Event 2", "Event 1"]


# ── Events already in MinIO ──────────────────────────────────────────────
# Their rosters are on file, so walking them again is minutes of browser work to
# rewrite the same object. They were skipped in SILENCE, which made a sweep over
# a tab this app had already worked look like one that was finding nothing.


def test_an_event_already_on_file_is_not_walked_again(sweep_stubs):
    sweep_stubs["collected"].add("event-1")
    sweep_stubs["pages"] = [[card(1), card(2)]]

    asyncio.run(campaign.sweep_events(Ctl()))

    assert sweep_stubs["opened"] == ["https://www.meetup.com/g/events/2/"]


def test_it_is_listed_as_already_scraped(sweep_stubs):
    """On the list, with a status — not passed over invisibly."""
    sweep_stubs["collected"].add("event-1")
    sweep_stubs["pages"] = [[card(1), card(2)]]
    ctl = Ctl()

    asyncio.run(campaign.sweep_events(ctl))

    rows = {r["title"]: r for r in ctl.meta["events_done"]}
    assert rows["Event 1"]["status"] == "skipped"
    assert rows["Event 2"]["status"] == "scraped"


def test_a_skipped_event_contributes_nobody(sweep_stubs):
    """It says 0 rather than borrowing the count of the roster on file: this
    run collected nobody from it, and the panel's total is this run's work."""
    sweep_stubs["collected"].add("event-1")
    sweep_stubs["pages"] = [[card(1)]]
    ctl = Ctl()

    result = asyncio.run(campaign.sweep_events(ctl))

    assert ctl.meta["events_done"][0]["count"] == 0
    assert result["count"] == 0
    assert result["events"] == 0


def test_a_skipped_event_is_listed_once_however_often_the_grid_is_read(
        sweep_stubs):
    """The grid comes back at the top after every event, so the same card is
    read again and again — it is CLAIMED like a fresh one, or the list fills
    with copies of it."""
    sweep_stubs["collected"].add("event-1")
    sweep_stubs["pages"] = [[card(1), card(2), card(3)]]
    ctl = Ctl()

    asyncio.run(campaign.sweep_events(ctl))

    titles = [r["title"] for r in ctl.meta["events_done"]]
    assert titles.count("Event 1") == 1


def test_a_whole_screenful_already_on_file_still_scrolls_for_more(sweep_stubs):
    """A tab whose first screenful is all done is not a finished tab — the grid
    lazy-loads, and there may be a hundred more under it."""
    for n in range(1, 4):
        sweep_stubs["collected"].add(f"event-{n}")
    sweep_stubs["pages"] = [[card(1), card(2), card(3)], [card(4)]]

    result = asyncio.run(campaign.sweep_events(Ctl()))

    assert sweep_stubs["scrolls"] > 0
    assert sweep_stubs["opened"] == ["https://www.meetup.com/g/events/4/"]
    assert result["events"] == 1


# The autouse `no_waiting` fixture points asyncio.sleep at a no-op, which is what
# keeps the rest of this file fast and would make every test below meaningless: a
# walk that hangs is the thing under test, and one that returns instantly is not
# a hang. Captured at import, before any of that is in place.
REAL_SLEEP = asyncio.sleep


# ── Getting stuck ────────────────────────────────────────────────────────
# The two ways a scrape quietly dies: a roster that stops arriving, and a tab
# with nothing new left on it. NEITHER raises, so without a clock a sweep sits on
# one event — or reloads one worked-out city — for as long as it is left running.
# Both now mean the same thing: not "stop", but "stop HERE".


@pytest.fixture
def quick_stall(monkeypatch):
    """The stall window, in fractions of a second rather than 90 of them."""
    monkeypatch.setattr(campaign, "stall_seconds", lambda *a, **k: 0.12)
    return 0.12


@pytest.fixture
def cities(monkeypatch):
    """Where each pass searched from, and in which order."""
    asked: list[dict] = []

    async def _search(page, ctl, account=None, avoid=""):
        city = f"City {len(asked) + 1}"
        asked.append({"city": city, "avoid": avoid})
        ctl.meta["city"] = city
        return {"ok": True, "city": city, "label": city}

    monkeypatch.setattr(campaign, "_search_from_a_city", _search)
    return asked


def hangs(state, after: int = 0):
    """An event walk that produces `after` members and then never another."""
    async def _members(**kwargs):
        state["opened"].append(kwargs.get("url"))
        ctl = kwargs.get("ctl")
        if ctl is not None:
            ctl.meta["members"] = [{"username": f"m{n}"} for n in range(after)]
        await REAL_SLEEP(30)             # cancelled by the watchdog, not awaited
        return {"ok": True, "members": [], "count": 0}
    return _members


def test_an_event_that_stops_producing_members_is_given_up_on(
        sweep_stubs, quick_stall, monkeypatch):
    """THE hang this exists for: Meetup stops serving the next page of a roster
    and nothing raises, so the walk sits there. The sweep has to come back."""
    monkeypatch.setattr(campaign, "event_members", hangs(sweep_stubs))
    sweep_stubs["pages"] = [[card(1)]]
    ctl = Ctl()

    result = asyncio.run(campaign.sweep_events(ctl))

    assert result["events"] == 0
    # Listed as stalled rather than as an event that was scraped — see
    # test_a_stalled_event_says_so_on_the_list.
    assert [r["status"] for r in ctl.meta["events_done"]] == ["stalled"]


def test_a_stall_is_not_the_end_of_the_sweep(sweep_stubs, quick_stall,
                                             monkeypatch):
    """It searches again somewhere else. An event that hangs is a fact about one
    event, not about whether there are events left to scrape."""
    calls = {"n": 0}
    real = campaign.event_members
    hang = hangs(sweep_stubs)

    async def _first_one_hangs(**kwargs):
        calls["n"] += 1
        return await (hang if calls["n"] == 1 else real)(**kwargs)

    monkeypatch.setattr(campaign, "event_members", _first_one_hangs)
    sweep_stubs["pages"] = [[card(1)], [card(2)]]

    result = asyncio.run(campaign.sweep_events(Ctl()))

    assert result["events"] == 1                  # event 2, on a later pass
    assert sweep_stubs["opened"][0].endswith("/events/1/")


def test_a_stalled_event_says_so_on_the_list(sweep_stubs, quick_stall,
                                             monkeypatch):
    """Ninety seconds of nothing followed by a panel that looks untouched is an
    operator who cannot tell a working run from a dead one."""
    monkeypatch.setattr(campaign, "event_members", hangs(sweep_stubs))
    sweep_stubs["pages"] = [[card(1)]]
    ctl = Ctl()

    asyncio.run(campaign.sweep_events(ctl))

    row = ctl.meta["events_done"][0]
    assert row["status"] == "stalled"
    assert row["title"] == "Event 1"
    assert row["count"] == 0                      # it collected nobody


def test_a_stalled_event_is_not_counted_as_work(sweep_stubs, quick_stall,
                                                monkeypatch):
    """Listed, but not in the total. The panel's count is what this run
    collected, and a stall collected nothing."""
    monkeypatch.setattr(campaign, "event_members", hangs(sweep_stubs, after=4))
    sweep_stubs["pages"] = [[card(1)]]

    result = asyncio.run(campaign.sweep_events(Ctl()))

    assert result["events"] == 0
    assert result["count"] == 0


def test_a_stalled_event_is_never_opened_again(sweep_stubs, quick_stall,
                                               monkeypatch):
    """It is claimed like any other. Walking back into the same hang on every
    pass is how a restart becomes a loop."""
    monkeypatch.setattr(campaign, "event_members", hangs(sweep_stubs))
    sweep_stubs["pages"] = [[card(1)]]

    asyncio.run(campaign.sweep_events(Ctl()))

    assert sweep_stubs["opened"] == ["https://www.meetup.com/g/events/1/"]


def test_a_dry_tab_starts_again_from_another_city(sweep_stubs, cities):
    """Not the end of the run. A tab with nothing new on it in one city says
    nothing about the same tab in another one."""
    sweep_stubs["pages"] = [[card(1)]]

    asyncio.run(campaign.sweep_events(Ctl()))

    assert [c["city"] for c in cities] == ["City 1", "City 2", "City 3", "City 4"]


def test_each_restart_avoids_the_city_it_just_left(sweep_stubs, cities):
    """Rolling the same place again is the one choice that cannot help — and
    with ten cities in the box it comes up often enough to matter."""
    sweep_stubs["pages"] = [[card(1)]]

    asyncio.run(campaign.sweep_events(Ctl()))

    assert [c["avoid"] for c in cities] == ["", "City 1", "City 2", "City 3"]


def test_a_stall_also_moves_the_lane_to_another_city(sweep_stubs, quick_stall,
                                                     cities, monkeypatch):
    """Somewhere else, rather than the next event on a grid this session has
    just stopped being served properly."""
    monkeypatch.setattr(campaign, "event_members", hangs(sweep_stubs))
    sweep_stubs["pages"] = [[card(1)]]

    asyncio.run(campaign.sweep_events(Ctl()))

    assert len(cities) > 1


def test_a_city_that_produced_something_resets_the_count(sweep_stubs, cities):
    """Only a RUN of empty passes ends a lane. One city with nothing left in it
    says nothing about the next, so the counter starts over on every event."""
    sweep_stubs["pages"] = [[card(1)], [card(2)], [card(3)]]

    asyncio.run(campaign.sweep_events(Ctl()))

    # Three events came in, so three passes had something in them — the lane can
    # only have ended on SWEEP_RESTARTS empty ones after the last of them.
    assert len(cities) > campaign.SWEEP_RESTARTS


def test_a_lane_gives_up_rather_than_reloading_find_forever(sweep_stubs, cities):
    """The backstop. With a dead session or a broken selector every pass finds
    nothing, and "search somewhere else" would otherwise never stop."""
    sweep_stubs["pages"] = [[]]

    result = asyncio.run(campaign.sweep_events(Ctl()))

    assert result["events"] == 0
    assert len(cities) == campaign.SWEEP_RESTARTS


def test_a_stop_still_lands_while_a_lane_is_between_cities(sweep_stubs, cities):
    """The restart loop is a loop like any other — End must not have to wait for
    three more cities to be searched."""
    sweep_stubs["pages"] = [[]]

    asyncio.run(campaign.sweep_events(Ctl(stop_after=1)))

    assert len(cities) < campaign.SWEEP_RESTARTS


# ── The watchdog itself ──────────────────────────────────────────────────


class Meta:
    """A control with just the two things the watchdog reads."""

    def __init__(self, paused=False):
        self.meta: dict = {}
        self.paused = paused


def test_a_roster_that_keeps_arriving_is_never_cut_off():
    """A STALL clock, not a time limit. A big roster takes longer than the
    window by definition, and cutting it off would throw away the page load the
    run has already paid for."""
    ctl = Meta()

    async def _slow():
        for n in range(8):
            await REAL_SLEEP(0.02)
            ctl.meta["members"] = [{"username": f"m{i}"} for i in range(n + 1)]
        return {"ok": True, "count": 8}

    async def _go():
        return await campaign.run_until_stalled(_slow, ctl, 0.06)

    result, stalled = asyncio.run(_go())

    assert stalled is False
    assert result["count"] == 8


def test_a_walk_that_goes_quiet_is_cancelled():
    ctl = Meta()

    async def _hang():
        await REAL_SLEEP(30)

    async def _go():
        return await campaign.run_until_stalled(_hang, ctl, 0.08)

    result, stalled = asyncio.run(_go())

    assert stalled is True
    assert result["ok"] is False
    assert "no new member" in result["detail"]


def test_a_paused_run_is_not_a_stalled_one():
    """A paused run produces nothing on purpose. Without this the first pause
    longer than the window would throw away the event it was paused in."""
    ctl = Meta(paused=True)
    ran = {"done": False}

    async def _work():
        await REAL_SLEEP(0.3)
        ran["done"] = True
        return {"ok": True}

    async def _go():
        return await campaign.run_until_stalled(_work, ctl, 0.05)

    result, stalled = asyncio.run(_go())

    assert stalled is False
    assert ran["done"] is True


def test_the_window_is_the_operators(monkeypatch):
    monkeypatch.setattr(campaign.app_settings, "current",
                        lambda: {"scrape_stall_s": 45})
    assert campaign.stall_seconds() == 45


def test_a_window_of_nothing_is_refused():
    """It would abandon every event the moment it opened — before the page had
    even loaded."""
    assert campaign.stall_seconds({"scrape_stall_s": 0}) >= 5


# ── One tab between events ───────────────────────────────────────────────
# Walking a roster opens a tab per attendee, and the odd one is left behind: a
# profile that hung, a popup blocked mid-navigation. Twenty events later that is
# twenty stray tabs on the window.


class Tab:
    def __init__(self, url=""):
        self.url = url
        self.closed = False

    async def close(self):
        self.closed = True


def find_window(extra: int = 3):
    """One Find tab and `extra` leftover profiles, in one window."""
    page = Tab(meetup.FIND_URL)
    others = [Tab(f"https://www.meetup.com/members/{n}/") for n in range(extra)]

    class Context:
        pages = [page] + others

    page.context = Context()
    return page, others


def test_every_other_tab_is_closed(monkeypatch):
    async def _open_find(page):
        return {"ok": True}

    monkeypatch.setattr(campaign.meetup, "open_find", _open_find)
    page, others = find_window()

    closed = asyncio.run(campaign._lone_find_tab(page))

    assert closed == 3
    assert all(tab.closed for tab in others)
    assert page.closed is False


def test_the_surviving_tab_is_put_back_on_find(monkeypatch):
    """Not left on whatever profile happened to be showing — the next event
    starts from the same place the first one did."""
    went = []

    async def _open_find(page):
        went.append(page)
        return {"ok": True}

    monkeypatch.setattr(campaign.meetup, "open_find", _open_find)
    page, _ = find_window()

    asyncio.run(campaign._lone_find_tab(page))

    assert went == [page]


def test_a_tab_that_will_not_close_is_not_fatal(monkeypatch):
    """Tidying is the least valuable thing in the run. A window that has gone
    must not take the sweep with it."""
    async def _open_find(page):
        return {"ok": True}

    monkeypatch.setattr(campaign.meetup, "open_find", _open_find)
    page, others = find_window()

    async def _boom():
        raise RuntimeError("target closed")

    others[0].close = _boom

    assert asyncio.run(campaign._lone_find_tab(page)) == 2


def test_the_tabs_are_cleared_between_two_events(sweep_stubs, monkeypatch):
    """Between events, not at the end of the sweep — otherwise the leftovers
    accumulate for the whole run and the next event opens into a window with
    forty tabs on it."""
    cleared = []

    async def _lone(page):
        cleared.append(len(sweep_stubs["opened"]))
        return 0

    monkeypatch.setattr(campaign, "_lone_find_tab", _lone)
    sweep_stubs["pages"] = [[card(1), card(2), card(3)]]

    asyncio.run(campaign.sweep_events(Ctl()))

    assert cleared == [1, 2, 3]        # after each event, before the next
