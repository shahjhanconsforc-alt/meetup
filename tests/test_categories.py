"""Walking the Find page's category strip.

Each tab is a div carrying both a `category-item` class and a
`data-testid="category-<id>"` — numeric for a real category, a word or a
negative for the three specials (all, -999 New Groups, -998 From your Groups).

Two things about the live strip drive the design here: it renders late (React),
and it scrolls horizontally with a floating "Scroll right" button over its right
edge that can swallow a pointer click aimed at the tab beneath it.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.automation import meetup
from tests.test_first_chat import FakePage

DEFAULT_TABS = [("all", "All events", 12), ("-999", "New Groups", 5),
                ("652", "Social Activities", 8)]

# The real one, captured before no_waiting patches the asyncio module itself.
# The tests about CONCURRENCY need something that actually yields to the loop;
# the instant stub the rest of this file runs on never does.
REAL_SLEEP = asyncio.sleep


async def yielding_sleep(*args, **kwargs):
    """A sleep that hands control to the loop without spending the time.

    The concurrency tests need both halves and the file's two other stubs give
    only one each: `no_waiting`'s returns without ever yielding, so tasks that
    should overlap run one after another instead; the real one yields but then
    actually waits, and the list loader polls on multi-second budgets that turn
    a test into twenty seconds of nothing.
    """
    for _ in range(50):
        await REAL_SLEEP(0)
    return 0.0


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def _instant(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(meetup.asyncio, "sleep", _instant)
    monkeypatch.setattr(meetup.human, "think", _instant)
    monkeypatch.setattr(meetup.human, "pause", _instant)
    monkeypatch.setattr(meetup, "_CATEGORY_WAIT_S", 0.3)
    monkeypatch.setattr(meetup, "_CATEGORY_SWAP_S", 0.3)
    monkeypatch.setattr(meetup, "_INBOX_WAIT_S", 0.3)


def select(page, key):
    """Mark one tab selected, as the page does: its label gets ds2-m12 and every
    other label goes back to ds2-k12."""
    for index, element in enumerate(page.matches["category_item"]):
        chosen = element["data-testid"] == f"category-{key}"
        element["span"]["class"] = "ds2-m12" if chosen else "ds2-k12"
        if chosen and "cards" in element:
            # Selecting a tab swaps the results grid in place. Distinct NUMERIC
            # ids — the real ones are numeric and the url carries them, so the
            # dedupe isn't silently collapsing the grid and event_ref still
            # finds the group slug.
            base = 315700000 + index * 1000
            page.matches["event_card"] = [
                card(event_id=str(base + n)) for n in range(element["cards"])]


@pytest.fixture(autouse=True)
def stub_bits(monkeypatch):
    """The human pointer, and navigation. `swallow` models the Scroll-right
    button floating over a tab: the pointer press lands on it, not the tab."""
    log = {"clicks": [], "gotos": [], "swallow": set(), "swift": []}

    async def _beat(_span):
        return 0.0

    monkeypatch.setattr(meetup.behavior, "beat", _beat)

    class _Pointer:
        def __init__(self, page):
            self.page = page

        async def click(self, locator, *, swift=False):
            element = locator._elements[0] if locator._elements else {}
            key = element.get("data-testid", "?")
            log["clicks"].append(key)
            log["swift"].append(swift)
            # Only a category tab selects. Cards are clicked through here too,
            # and treating one as a tab used to blow up inside expect_page —
            # which the visit swallows, so it silently took the fallback path.
            if key.startswith("category-") and key not in log["swallow"]:
                select(self.page, key.replace("category-", "", 1))
            return True

    async def _goto(page, url, **kwargs):
        log["gotos"].append(url)
        page.url = url
        return True

    async def _dismiss(page, selectors):
        return 0

    monkeypatch.setattr(meetup, "Pointer", _Pointer)
    monkeypatch.setattr(meetup.visit, "goto", _goto)
    monkeypatch.setattr(meetup.visit, "dismiss_overlays", _dismiss)
    return log


def find_page(tabs=None, url="https://www.meetup.com/find/"):
    """A Find page with a category strip. `tabs` is [(key, name, cards)]."""
    tabs = DEFAULT_TABS if tabs is None else tabs
    page = FakePage({"category_item": [], "event_card": []}, url=url)
    items = []
    for key, name, cards in tabs:
        span = {"text": name, "class": "ds2-k12"}
        element = {
            "data-testid": f"category-{key}", "visible": True, "cards": cards,
            "span": span,
            "children": {meetup._sel("category_name"): [span]},
            # Playwright's own click always reaches the tab, which is what the
            # fallback relies on.
            "on_direct_click": (lambda p=page, k=key: select(p, k)),
        }
        items.append(element)
        # click_category builds this selector by hand, so it must resolve to the
        # SAME dict — otherwise selection wouldn't be observable through it.
        page.matches[f"[data-testid='category-{key}']"] = [element]
    page.matches["category_item"] = items
    return page


# ── Reading the strip ────────────────────────────────────────────────────

def test_lists_every_category_in_order():
    cats = asyncio.run(meetup.list_categories(find_page()))
    assert [c["key"] for c in cats] == ["all", "-999", "652"]
    assert [c["name"] for c in cats] == ["All events", "New Groups", "Social Activities"]


def test_the_same_tab_matched_twice_is_listed_once():
    """`.category-item` and `[data-testid^='category-']` are comma-joined, so
    every tab matches both and appears twice in the locator."""
    page = find_page()
    page.matches["category_item"] = page.matches["category_item"] * 2
    cats = asyncio.run(meetup.list_categories(page))
    assert [c["key"] for c in cats] == ["all", "-999", "652"]


def test_a_page_without_the_strip_lists_nothing():
    assert asyncio.run(meetup.list_categories(FakePage({}))) == []


def test_a_nameless_tab_falls_back_to_its_key():
    page = find_page(tabs=[("652", "", 3)])
    page.matches["category_item"][0]["children"] = {}
    assert asyncio.run(meetup.list_categories(page))[0]["name"] == "652"


def test_the_label_is_found_past_an_icon_span():
    """THE missing names.

    A live tab wraps its icon in one span and its name in another, and the icon
    comes first on plenty of them — so reading `span.first` got an empty string,
    every one of those tabs fell back to its numeric key, and the panel grouped
    events under "652" instead of "Social Activities".
    """
    page = find_page(tabs=[("652", "Social Activities", 3)])
    tab = page.matches["category_item"][0]
    icon = {"text": "", "class": "ds2-k12"}
    tab["children"][meetup._sel("category_name")] = [icon, tab["span"]]

    assert asyncio.run(meetup.list_categories(page))[0]["name"] == "Social Activities"


def test_a_tab_whose_spans_are_all_empty_reads_its_own_text():
    page = find_page(tabs=[("652", "", 3)])
    tab = page.matches["category_item"][0]
    tab["children"][meetup._sel("category_name")] = [{"text": "", "class": "ds2-k12"}]
    tab["text"] = "\n Social Activities \n42 events"

    assert asyncio.run(meetup.list_categories(page))[0]["name"] == "Social Activities"


def test_selection_is_seen_on_a_label_that_is_not_the_first_span():
    """Same shape, the other consequence: the selected class sits on the label,
    so checking only the icon's span reported "not selected" for a tab that
    plainly was — and sent every click down the retry path."""
    page = find_page(tabs=[("652", "Social Activities", 3)])
    tab = page.matches["category_item"][0]
    tab["children"][meetup._sel("category_name")] = [
        {"text": "", "class": "ds2-k12"}, tab["span"]]
    tab["span"]["class"] = "ds2-m12"                      # this one is selected

    assert asyncio.run(meetup._category_selected(page.locator(
        "[data-testid='category-652']"))) is True


# ── Waiting for a strip that renders late ────────────────────────────────

def test_open_find_waits_for_the_strip(stub_bits):
    """THE bug this file exists for.

    /find/ is a React page: its strip arrives well after domcontentloaded.
    Listing the tabs the instant navigation returned found none, so the sweep
    reported "no categories" and clicked nothing on a page that visibly had
    twenty.
    """
    page = find_page(url="https://www.meetup.com/")
    page.matches["category_item"] = []            # not rendered yet
    tabs = find_page().matches["category_item"]

    calls = {"n": 0}
    original = meetup._first_visible

    async def _late(locator, scan=12):
        calls["n"] += 1
        if calls["n"] >= 2:                       # arrives on the second poll
            page.matches["category_item"] = tabs
        return await original(page.locator(meetup._sel("category_item")))

    meetup._first_visible, restore = _late, original
    try:
        result = asyncio.run(meetup.open_find(page))
    finally:
        meetup._first_visible = restore

    assert result["ok"] is True
    assert page.matches["category_item"]          # the wait let it land


def test_open_find_reports_a_strip_that_never_arrives(stub_bits):
    page = find_page(tabs=[], url="https://www.meetup.com/")
    result = asyncio.run(meetup.open_find(page))
    assert result["ok"] is False
    assert "no category strip" in result["detail"]


def test_the_sweep_stops_when_no_strip_renders(stub_bits):
    result = asyncio.run(meetup.sweep_categories(find_page(tabs=[]), visit_cards_per_category=0))
    assert result["ok"] is False
    assert result["visited"] == []


# ── Clicking one ─────────────────────────────────────────────────────────

def test_clicking_a_category_reports_the_cards_that_landed(stub_bits):
    page = find_page()
    result = asyncio.run(meetup.click_category(page, "652"))

    assert result["ok"] is True
    assert stub_bits["clicks"] == ["category-652"]
    assert result["cards"] == 8


def test_a_tab_is_scrolled_into_view_before_it_is_clicked(stub_bits):
    """Most of the strip starts outside its horizontal scroll container, and a
    pointer aims at coordinates — an off-screen tab has none worth clicking."""
    page = find_page()
    asyncio.run(meetup.click_category(page, "652"))
    assert page.matches["[data-testid='category-652']"][0]["scrolled"] is True


def test_a_swallowed_click_is_retried_directly(stub_bits):
    """The "Scroll right" button floats over the strip's right edge at z-20. A
    pointer press aimed at the tab beneath it hits the button and merely
    scrolls, so the tab never selects — Playwright's own click waits for the
    element to actually receive pointer events, which rescues exactly this."""
    page = find_page()
    stub_bits["swallow"].add("category-652")

    result = asyncio.run(meetup.click_category(page, "652"))

    assert result["ok"] is True
    assert stub_bits["clicks"] == ["category-652"]          # the pointer tried
    assert page.matches["[data-testid='category-652']"][0]["direct_clicks"] == 1
    assert result["cards"] == 8                              # and it landed


def test_a_tab_that_takes_the_pointer_click_is_not_clicked_twice(stub_bits):
    page = find_page()
    asyncio.run(meetup.click_category(page, "652"))
    assert "direct_clicks" not in page.matches["[data-testid='category-652']"][0]


def test_tabs_are_clicked_with_the_swift_pointer(stub_bits):
    """Read-only tab flicking, not a send: the slow pacing protects nothing here
    and made twenty tabs take minutes."""
    asyncio.run(meetup.sweep_categories(find_page(), visit_cards_per_category=0))
    assert stub_bits["swift"] == [True, True, True]


def test_a_category_with_no_matching_cards_settles_within_the_grace(stub_bits):
    """THE slowness, and the line it now has to be balanced against.

    The settle wait (_settled_count) used to need a POSITIVE count before it
    would return — so whenever the card selectors matched nothing on /find/ (the
    normal case; they were written for group and event pages) every category
    waited out the whole budget for a number that was never coming.

    An empty grid is still a settled answer, just not an INSTANT one: after a
    click it is watched for _GRID_CLICK_GRACE_S, because "nothing yet" and
    "nothing at all" look identical and reading the first as the second is what
    reported a category full of events as having none. That grace is bounded and
    small; the load budget it must never reach is _GRID_LOAD_S.
    """
    page = find_page(tabs=[("652", "Social Activities", 0)])
    polls = {"n": 0}
    original = meetup._card_count

    async def _counted(p):
        polls["n"] += 1
        return await original(p)

    meetup._card_count = _counted
    try:
        result = asyncio.run(meetup.click_category(page, "652"))
    finally:
        meetup._card_count = original

    assert result["ok"] is True
    assert result["cards"] == 0
    # The click grace, then two matching polls — the full load budget would be
    # more than twice this many.
    grace_polls = meetup._GRID_CLICK_GRACE_S / meetup._GRID_STEP
    assert polls["n"] <= grace_polls + 6
    assert polls["n"] < meetup._GRID_LOAD_S / meetup._GRID_STEP


# ── The grid has to be the NEW category's ────────────────────────────────

def frozen(page, key):
    """Make one tab's click land without its results ever changing — the live
    failure this section is about."""
    page.matches[f"[data-testid='category-{key}']"][0].pop("cards", None)


def test_a_grid_that_never_swaps_is_reported_stale(stub_bits):
    """THE mislabelling.

    Waiting only for the card COUNT to stop changing meant a click was followed
    ~0.3s later by a grid that had not swapped yet and had been perfectly stable
    the whole time. Two categories with twelve events each look identical by
    count, so the previous category's cards passed for the new one's.
    """
    page = find_page()
    asyncio.run(meetup.click_category(page, "all"))       # its grid lands
    frozen(page, "652")

    result = asyncio.run(meetup.click_category(page, "652"))

    assert result["ok"] is True                            # the click did land
    assert result["stale"] is True                         # the results did not
    assert "never changed" in result["detail"]


def test_a_grid_that_swaps_is_not_stale(stub_bits):
    page = find_page()
    asyncio.run(meetup.click_category(page, "all"))
    result = asyncio.run(meetup.click_category(page, "652"))

    assert result["stale"] is False
    assert result["cards"] == 8


def test_a_grid_of_the_same_size_but_different_events_is_not_stale(stub_bits):
    """Equal counts are exactly the case the count-only wait could not tell
    apart, so the fingerprint is the ids rather than the number."""
    page = find_page(tabs=[("all", "All events", 6), ("652", "Social", 6)])
    asyncio.run(meetup.click_category(page, "all"))
    result = asyncio.run(meetup.click_category(page, "652"))

    assert result["stale"] is False
    assert result["cards"] == 6


def test_an_empty_category_is_not_stale(stub_bits):
    """Nothing on screen is an answer, and there is nothing there to mislabel —
    so an empty grid must not be held against the budget or the category."""
    page = find_page(tabs=[("all", "All events", 0), ("652", "Social", 0)])
    asyncio.run(meetup.click_category(page, "all"))
    result = asyncio.run(meetup.click_category(page, "652"))

    assert result["stale"] is False
    assert result["cards"] == 0


def test_the_sweep_files_nothing_under_a_stale_category(stub_bits):
    """The whole point: a category whose results never changed contributes NO
    events, rather than a copy of the previous category's filed under its name.
    """
    page = find_page()
    frozen(page, "-999")

    result = asyncio.run(meetup.sweep_categories(page, visit_cards_per_category=0))
    by_name = {v["name"]: v for v in result["visited"]}

    assert by_name["New Groups"]["stale"] is True
    assert by_name["New Groups"]["events"] == []            # not All events' twelve
    assert len(by_name["All events"]["events"]) == 12
    assert len(by_name["Social Activities"]["events"]) == 8
    assert "1 showed no new results" in result["detail"]


def test_a_stale_category_does_not_count_as_visited(stub_bits):
    page = find_page()
    frozen(page, "-999")
    result = asyncio.run(meetup.sweep_categories(page, visit_cards_per_category=0))
    assert "Visited 2/3 categories" in result["detail"]


def test_an_unknown_category_is_reported_not_raised(stub_bits):
    result = asyncio.run(meetup.click_category(find_page(), "nope"))
    assert result["ok"] is False
    assert "No category tab" in result["detail"]
    assert stub_bits["clicks"] == []


# ── The sweep ────────────────────────────────────────────────────────────

def test_the_sweep_clicks_every_tab_once_in_order(stub_bits):
    result = asyncio.run(meetup.sweep_categories(find_page(), visit_cards_per_category=0))

    assert result["ok"] is True
    assert stub_bits["clicks"] == ["category-all", "category--999", "category-652"]
    assert [v["name"] for v in result["visited"]] == [
        "All events", "New Groups", "Social Activities"]
    assert [v["cards"] for v in result["visited"]] == [12, 5, 8]


def test_the_sweep_navigates_to_find_when_elsewhere(stub_bits):
    page = find_page(url="https://www.meetup.com/")
    asyncio.run(meetup.sweep_categories(page, visit_cards_per_category=0))
    assert stub_bits["gotos"] == [meetup.FIND_URL]


def test_a_page_already_on_find_is_not_reloaded(stub_bits):
    asyncio.run(meetup.sweep_categories(find_page(), visit_cards_per_category=0))
    assert stub_bits["gotos"] == []


def test_the_sweep_can_be_limited(stub_bits):
    result = asyncio.run(meetup.sweep_categories(find_page(), limit=2, visit_cards_per_category=0))
    assert len(result["visited"]) == 2
    assert stub_bits["clicks"] == ["category-all", "category--999"]


def test_the_sweep_can_target_named_keys(stub_bits):
    result = asyncio.run(meetup.sweep_categories(find_page(), keys=["652"], visit_cards_per_category=0))
    assert [v["key"] for v in result["visited"]] == ["652"]


def test_the_sweep_stops_on_a_control_stop(stub_bits):
    """Pause and stop land BETWEEN tabs, never mid-click."""
    class Ctl:
        total = 0

        def __init__(self):
            self.seen = 0

        async def checkpoint(self, _label=None):
            self.seen += 1
            return self.seen <= 2          # stop before the third

        def advance(self, _n, _label=None):
            pass

    result = asyncio.run(meetup.sweep_categories(find_page(), ctl=Ctl(), visit_cards_per_category=0))

    assert len(result["visited"]) == 2
    assert stub_bits["clicks"] == ["category-all", "category--999"]


def test_the_sweep_reports_a_find_page_that_will_not_load(stub_bits, monkeypatch):
    async def _fail(page, url, **kwargs):
        return False

    monkeypatch.setattr(meetup.visit, "goto", _fail)
    result = asyncio.run(meetup.sweep_categories(
        find_page(url="https://www.meetup.com/"), visit_cards_per_category=0))

    assert result["ok"] is False
    assert result["visited"] == []


# ── Reading the event cards ──────────────────────────────────────────────

def card(*, event_id="315749481", urlname="pakistan-agile-education",
         title="Run Better Workshops with Miro",
         when="2026-08-21T16:00:00+05:00[Asia/Karachi]",
         text=None, href=None):
    """One card shaped like the live markup."""
    href = href if href is not None else (
        f"https://www.meetup.com/{urlname}/events/{event_id}/"
        f"?recId=5de3d35e&recSource=ml-popular&eventOrigin=find_page%24all")
    text = text if text is not None else (
        f"Going\n{title}\nFri, Aug 21 · 4:00 PM PKT\n"
        f"by Pakistan Agile Education\n4.6\n38 attendees")
    return {
        "data-eventref": event_id,
        "visible": True,
        "text": text,
        "children": {
            meetup._sel("card_link"): [{"href": href}],
            meetup._sel("card_title"): [{"title": title, "text": title}],
            meetup._sel("card_time"): [{"datetime": when}],
        },
    }


def cards_page(cards):
    return FakePage({"event_card": cards})


def test_a_card_is_read_into_a_row():
    row = asyncio.run(meetup.cards_on_page(cards_page([card()])))[0]

    assert row["event_id"] == "315749481"
    assert row["urlname"] == "pakistan-agile-education"
    assert row["title"] == "Run Better Workshops with Miro"
    assert row["group"] == "Pakistan Agile Education"
    assert row["when"] == "2026-08-21T16:00:00+05:00[Asia/Karachi]"
    assert row["attendees"] == 38
    assert row["rating"] == "4.6"
    assert row["going"] is True
    assert row["online"] is False


def test_the_tracking_query_is_dropped_from_the_url():
    """recId / searchId / eventOrigin are this visit's telemetry, not the
    event's identity — keeping them would make the same event look new."""
    row = asyncio.run(meetup.cards_on_page(cards_page([card()])))[0]
    assert row["url"] == ("https://www.meetup.com/pakistan-agile-education"
                          "/events/315749481/")
    assert "recId" not in row["url"]


def test_an_online_event_is_flagged():
    row = asyncio.run(meetup.cards_on_page(cards_page([
        card(text="Creative Writing Workshop\nTue, Aug 25 · Online\n"
                  "by Creative Writing Meet- UP\n844 attendees")])))[0]
    assert row["online"] is True
    assert row["attendees"] == 844


def test_a_card_without_a_rating_or_host_still_reads():
    row = asyncio.run(meetup.cards_on_page(cards_page([
        card(text="WooCommerce Meetup\nSat, Aug 15 · 4:30 PM PKT\n11 attendees")])))[0]
    assert row["group"] == ""
    assert row["rating"] == ""
    assert row["attendees"] == 11


def test_a_thousands_separator_in_the_attendee_count_is_handled():
    row = asyncio.run(meetup.cards_on_page(cards_page([
        card(text="Big one\n1,204 attendees")])))[0]
    assert row["attendees"] == 1204


def test_a_card_with_no_link_is_skipped():
    broken = card()
    broken["children"][meetup._sel("card_link")] = []
    broken.pop("data-eventref")
    assert asyncio.run(meetup.cards_on_page(cards_page([broken]))) == []


def test_the_same_card_matched_twice_is_read_once():
    one = card()
    assert len(asyncio.run(meetup.cards_on_page(cards_page([one, one])))) == 1


def test_event_ref_pulls_the_group_and_id_out_of_a_url():
    assert meetup.event_ref(
        "https://www.meetup.com/hack-the-box-meetup-lahore-pk/events/315992342/?x=1"
    ) == ("hack-the-box-meetup-lahore-pk", "315992342")


def test_event_ref_on_a_non_event_url_is_empty():
    assert meetup.event_ref("https://www.meetup.com/find/") == ("", "")


def test_the_sweep_carries_each_category_s_events(stub_bits):
    """Each category's own grid is harvested after its tab is selected — the
    events come back attached to the category they were found under."""
    result = asyncio.run(meetup.sweep_categories(find_page(), visit_cards_per_category=0))

    assert [len(v["events"]) for v in result["visited"]] == [12, 5, 8]
    first = result["visited"][0]["events"][0]
    assert first["event_id"] == "315700000"             # from that tab's grid
    assert first["urlname"] == "pakistan-agile-education"
    assert first["title"] == "Run Better Workshops with Miro"


# ── Opening the cards ────────────────────────────────────────────────────

class FakePopup(FakePage):
    """The tab a target=_blank card opens."""

    def __init__(self, matches=None, url=""):
        super().__init__(matches, url=url)
        self.closed = False
        self.load_states = []

    async def wait_for_load_state(self, state, **kwargs):
        self.load_states.append(state)

    async def close(self):
        self.closed = True


class FakeContext:
    """Models expect_page(): the click opens a tab, and we catch it."""

    def __init__(self, popup=None):
        self.popup = popup
        self.expected = 0

    def expect_page(self, timeout=None):
        self.expected += 1
        context = self

        class _Catcher:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            @property
            def value(self_inner):
                async def _get():
                    if context.popup is None:
                        raise TimeoutError("no page opened")
                    return context.popup
                return _get()

        return _Catcher()


def grid_page(cards, popup=None, with_context=True):
    page = FakePage({"event_card": cards})
    if with_context:
        page.context = FakeContext(popup)
    return page


def event_popup(title="Run Better Workshops with Miro",
                description="A hands-on workshop in Lahore."):
    return FakePopup({
        meetup._sel("og_title"): [{"content": title}],
        meetup._sel("og_description"): [{"content": description}],
    }, url="https://www.meetup.com/pakistan-agile-education/events/315749481/")


def test_a_card_click_opens_a_tab_which_is_read_and_closed(stub_bits):
    """The card is a target=_blank link, so clicking spawns a tab rather than
    navigating. Catching, reading and closing it is both the correct handling
    and the shape of a person opening a listing and closing it."""
    popup = event_popup()
    page = grid_page([card()], popup=popup)

    result = asyncio.run(meetup.visit_card(page, 0))

    assert result["ok"] is True
    assert result["info"]["title"] == "Run Better Workshops with Miro"
    assert result["info"]["description"] == "A hands-on workshop in Lahore."
    assert popup.closed is True                      # no tab left behind
    assert "domcontentloaded" in popup.load_states


def test_the_visited_url_drops_its_tracking_query(stub_bits):
    page = grid_page([card()], popup=event_popup())
    result = asyncio.run(meetup.visit_card(page, 0))
    assert result["info"]["url"] == ("https://www.meetup.com/"
                                     "pakistan-agile-education/events/315749481/")


def test_a_card_that_opens_no_tab_falls_back_to_navigating(stub_bits):
    """A card rendered without target=_blank, or a blocked window: go there in
    this tab and come back, so the grid survives."""
    page = grid_page([card()], popup=None)
    page.matches[meetup._sel("og_title")] = [{"content": "MITRE ATT&CK Framework"}]
    page.matches[meetup._sel("og_description")] = [{"content": "HTB Lahore"}]
    page.went_back = False

    async def _go_back(**kwargs):
        page.went_back = True

    page.go_back = _go_back

    result = asyncio.run(meetup.visit_card(page, 0))

    assert result["ok"] is True
    assert result["info"]["title"] == "MITRE ATT&CK Framework"
    assert page.went_back is True                    # back to the grid
    assert stub_bits["gotos"][0].endswith("/events/315749481/")
    # And on to the attendees, which is the reason for opening an event at all.
    assert stub_bits["gotos"][1].endswith("/events/315749481/attendees/")


def test_a_card_with_no_link_is_reported(stub_bits):
    broken = card()
    broken["children"][meetup._sel("card_link")] = []
    result = asyncio.run(meetup.visit_card(grid_page([broken]), 0))
    assert result["ok"] is False
    assert "no link" in result["detail"]


def test_visiting_folds_the_page_text_back_into_the_row(stub_bits):
    rows = [{"event_id": "1", "title": "from the card"}]
    page = grid_page([card()], popup=event_popup(description="The full blurb."))

    count = asyncio.run(meetup.visit_cards(page, rows, 1))

    assert count == 1
    assert rows[0]["description"] == "The full blurb."
    assert rows[0]["visited"] is True


def test_the_visit_cap_is_respected(stub_bits):
    rows = [{"event_id": str(n)} for n in range(10)]
    page = grid_page([card(event_id=str(n)) for n in range(10)],
                     popup=event_popup())

    count = asyncio.run(meetup.visit_cards(page, rows, 3))

    assert count == 3
    assert [r.get("visited") for r in rows[:3]] == [True, True, True]
    assert "visited" not in rows[3]                  # the rest untouched


def test_a_zero_cap_opens_nothing(stub_bits):
    rows = [{"event_id": "1"}]
    page = grid_page([card()], popup=event_popup())
    assert asyncio.run(meetup.visit_cards(page, rows, 0)) == 0
    assert "visited" not in rows[0]


def test_visiting_stops_on_a_control_stop(stub_bits):
    class Ctl:
        async def checkpoint(self, _label=None):
            return False

    rows = [{"event_id": "1"}, {"event_id": "2"}]
    page = grid_page([card(), card(event_id="2")], popup=event_popup())

    assert asyncio.run(meetup.visit_cards(page, rows, 2, ctl=Ctl())) == 0


# ── The attendees inside an opened event ─────────────────────────────────

def attendee_popup(members=None, with_link=True):
    """An event page, with a See-all link and an attendee list behind it."""
    members = members if members is not None else [
        {"username": "alice", "name": "Alice"}, {"username": "bob", "name": "Bob"}]
    popup = FakePopup({
        meetup._sel("og_title"): [{"content": "Run Better Workshops with Miro"}],
        meetup._sel("og_description"): [{"content": "A workshop."}],
    }, url="https://www.meetup.com/pakistan-agile-education/events/315749481/")
    if with_link:
        # Keyed by the SEL NAME, not the comma-joined selector: open_attendees
        # now tries the candidates one at a time (the href shape has to beat the
        # `See all` text match), and a fake that only answers to the joined form
        # has nothing to give those individual asks.
        popup.matches["attendees_link"] = [{
            "visible": True,
            "href": popup.url + "attendees/",
            # Following the link is what moves the page.
            "on_click_url": popup.url + "attendees/",
        }]
    popup.members = members
    return popup


@pytest.fixture
def attendee_stubs(monkeypatch, stub_bits):
    """Collecting attendees, and a pointer that follows a link's href."""
    async def _members(page):
        return getattr(page, "members", [])

    async def _scroll(page, collect=None, ctl=None, target=0, **kwargs):
        return await collect(page)

    class _Pointer:
        def __init__(self, page):
            self.page = page

        async def click(self, locator, *, swift=False):
            element = locator._elements[0] if locator._elements else {}
            key = element.get("data-testid", "")
            # Label by what the element IS. Calling every unlabelled click
            # "see-all" also caught the card click and made the skip test pass
            # for the wrong reason.
            if element.get("on_click_url"):
                stub_bits["clicks"].append("see-all")
                self.page.url = element["on_click_url"]
            elif key.startswith("category-"):
                stub_bits["clicks"].append(key)
                if key not in stub_bits["swallow"]:
                    select(self.page, key.replace("category-", "", 1))
            else:
                stub_bits["clicks"].append("card")
            return True

    monkeypatch.setattr(meetup, "_members_on_page", _members)
    monkeypatch.setattr(meetup.visit, "scroll_to_end", _scroll)
    monkeypatch.setattr(meetup, "Pointer", _Pointer)
    return stub_bits


def test_an_opened_event_clicks_see_all_and_collects_the_attendees(attendee_stubs):
    """The attendees are the point of opening an event — they are the only
    people this bot can actually work with."""
    popup = attendee_popup()
    page = grid_page([card()], popup=popup)

    result = asyncio.run(meetup.visit_card(page, 0))

    assert "see-all" in attendee_stubs["clicks"]
    assert [m["username"] for m in result["info"]["attendees"]] == ["alice", "bob"]
    assert popup.closed is True


def test_no_see_all_link_falls_back_to_the_attendees_route(attendee_stubs):
    """/<group>/events/<id>/attendees/ is a Meetup route — the same place by a
    different door when the link isn't rendered."""
    popup = attendee_popup(with_link=False)
    page = grid_page([card()], popup=popup)

    result = asyncio.run(meetup.visit_card(page, 0))

    assert attendee_stubs["gotos"][-1].endswith("/events/315749481/attendees/")
    assert len(result["info"]["attendees"]) == 2


def test_duplicate_attendees_are_collapsed(attendee_stubs):
    popup = attendee_popup(members=[{"username": "alice"}, {"username": "alice"},
                                    {"username": "bob"}])
    page = grid_page([card()], popup=popup)
    result = asyncio.run(meetup.visit_card(page, 0))
    assert [m["username"] for m in result["info"]["attendees"]] == ["alice", "bob"]


def test_the_attendee_target_caps_the_haul(attendee_stubs):
    """An 800-RSVP event would otherwise scroll for minutes inside a browse."""
    popup = attendee_popup(members=[{"username": f"m{n}"} for n in range(50)])
    page = grid_page([card()], popup=popup)

    result = asyncio.run(meetup.visit_card(page, 0, attendee_target=5))

    assert len(result["info"]["attendees"]) == 5


def test_a_zero_attendee_target_skips_the_attendee_step(attendee_stubs):
    popup = attendee_popup()
    page = grid_page([card()], popup=popup)

    result = asyncio.run(meetup.visit_card(page, 0, attendee_target=0))

    assert "attendees" not in result["info"]
    assert "see-all" not in attendee_stubs["clicks"]


def test_members_are_folded_onto_the_event_row(attendee_stubs):
    rows = [{"event_id": "1"}]
    page = grid_page([card()], popup=attendee_popup())

    asyncio.run(meetup.visit_cards(page, rows, 1))

    assert [m["username"] for m in rows[0]["members"]] == ["alice", "bob"]


def test_the_live_attendee_anchor_is_the_route_shape():
    """Label and testid both move; /attendees is a route and does not."""
    assert "a[href*='/attendees']" in meetup.SEL["attendees_link"]


def test_read_event_prefers_opengraph_over_the_heading(stub_bits):
    """og: tags are a contract with every link preview on the internet; the
    rendered classes are hashed tokens that move on a redeploy."""
    popup = FakePopup({
        meetup._sel("og_title"): [{"content": "From OpenGraph"}],
        meetup._sel("event_title"): [{"text": "From the h1"}],
    })
    assert asyncio.run(meetup.read_event(popup))["title"] == "From OpenGraph"


def test_read_event_falls_back_to_the_heading(stub_bits):
    popup = FakePopup({meetup._sel("event_title"): [{"text": "From the h1"}]})
    assert asyncio.run(meetup.read_event(popup))["title"] == "From the h1"


def test_the_sweep_opens_cards_in_every_category(stub_bits):
    """The sweep tests above pass visit_cards_per_category=0 because they are
    about walking the strip. This one is about the visiting."""
    page = find_page()
    page.context = FakeContext(event_popup(description="Opened blurb."))

    result = asyncio.run(meetup.sweep_categories(page, visit_cards_per_category=2))

    assert [v["visited_cards"] for v in result["visited"]] == [2, 2, 2]
    first = result["visited"][0]["events"]
    assert first[0]["description"] == "Opened blurb."
    assert first[0]["visited"] is True
    assert "visited" not in first[2]          # only the capped first two


def test_the_sweep_opens_nothing_when_the_cap_is_zero(stub_bits):
    page = find_page()
    page.context = FakeContext(event_popup())

    result = asyncio.run(meetup.sweep_categories(page, visit_cards_per_category=0))

    assert [v["visited_cards"] for v in result["visited"]] == [0, 0, 0]


def test_the_live_card_anchors_are_covered():
    """From the rendered grid: a data-testid, a data-element-name and the
    data-eventref that carries the id on the card itself."""
    assert "[data-testid='categoryResults-eventCard']" in meetup.SEL["event_card"]
    assert "[data-eventref]" in meetup.SEL["event_card"]
    assert "h3[title]" in meetup.SEL["card_title"]


def test_the_live_category_anchors_are_covered():
    """From the rendered strip: a `category-item` class and a data-testid whose
    suffix is the category id."""
    assert ".category-item" in meetup.SEL["category_item"]
    assert "[data-testid^='category-']" in meetup.SEL["category_item"]


# ── The loading shimmer ──────────────────────────────────────────────────
# THE complaint: a category plainly full of events came back "has no events
# right now". Clicking a tab swaps the grid to skeleton placeholders while the
# results are fetched — those carry no link and no title, so every card selector
# misses and the grid reads as empty. The old waits took that zero at face value.

class LoadingGrid(FakePage):
    """A grid that shows the shimmer for a few polls, then the real cards."""

    def __init__(self, *, polls: int, cards: int = 4, empty_copy: bool = False):
        super().__init__({"event_card": [], "grid_loading": [{"visible": True}],
                          "grid_empty": [{"visible": True}] if empty_copy else []},
                         url="https://www.meetup.com/find/")
        self.left = polls
        self.cards = cards
        self.card_reads = 0

    def locator(self, selector):
        key = self._key_for(selector)
        if key == "event_card":
            self.card_reads += 1
            # Each look at the grid is one poll. When the fetch lands, the
            # shimmer goes and the cards appear — in that order, together.
            if self.left > 0:
                self.left -= 1
                if self.left == 0:
                    base = 315700000
                    self.matches["event_card"] = [card(event_id=str(base + n))
                                                  for n in range(self.cards)]
                    self.matches["grid_loading"] = []
        return super().locator(selector)


def test_a_grid_still_loading_is_not_reported_as_empty():
    """The whole point: wait past the shimmer instead of answering zero."""
    page = LoadingGrid(polls=4, cards=4)
    assert asyncio.run(meetup.wait_for_cards(page)) == 4


def test_the_cards_are_read_once_the_shimmer_clears():
    page = LoadingGrid(polls=3, cards=6)
    rows = asyncio.run(meetup.cards_when_settled(page))
    assert len(rows) == 6


def test_an_empty_grid_is_believed_once_it_sits_still():
    """A category really can have nothing on it, and that answer must still
    come back — after the grace period, not after the whole budget."""
    page = FakePage({"event_card": [], "grid_loading": [], "grid_empty": []},
                    url="https://www.meetup.com/find/")
    assert asyncio.run(meetup.wait_for_cards(page)) == 0


def test_meetups_own_no_results_copy_ends_the_wait_at_once():
    page = LoadingGrid(polls=99, cards=0, empty_copy=True)
    assert asyncio.run(meetup.wait_for_cards(page)) == 0
    assert page.card_reads == 1              # asked once, answered, done


def test_the_shimmer_keeps_resetting_the_empty_grace():
    """A slow fetch is not an empty category however long it takes — as long as
    the shimmer is up, nothing is decided."""
    page = LoadingGrid(polls=20, cards=2)
    assert asyncio.run(meetup.wait_for_cards(page)) == 2


def test_a_category_whose_cards_arrive_late_still_reports_them(stub_bits):
    """End to end through the click: the count the panel shows comes from the
    settled grid, not from the instant the tab was pressed."""
    page = find_page(tabs=[("652", "Social Activities", 0)])
    tab = page.matches["category_item"][0]

    def _late_cards():
        page.matches["grid_loading"] = [{"visible": True}]
        page.matches["event_card"] = []

    tab["on_direct_click"] = _late_cards
    page.matches["grid_loading"] = []

    # The click swaps in the shimmer; the cards land two polls later.
    original = page.locator
    state = {"polls": 0}

    def _locator(selector):
        if page._key_for(selector) == "event_card" and page.matches["grid_loading"]:
            state["polls"] += 1
            if state["polls"] >= 2:
                page.matches["event_card"] = [card(event_id="315700001"),
                                              card(event_id="315700002")]
                page.matches["grid_loading"] = []
        return original(selector)

    page.locator = _locator
    select(page, "652")
    page.matches["grid_loading"] = [{"visible": True}]
    page.matches["event_card"] = []

    rows = asyncio.run(meetup.cards_when_settled(page))
    assert len(rows) == 2


def test_the_loading_selectors_do_not_match_a_settled_grid():
    """A selector that is always true would turn every empty category into a
    full wait — "placeholder" is left out of the list for exactly that reason."""
    assert not any("placeholder" in s for s in meetup.SEL["grid_loading"])
    assert "[aria-busy='true']" in meetup.SEL["grid_loading"]


# ── Clicking each attendee for the id their card does not carry ──────────
# An attendee's numeric id is only in the "View" link beside their card, and
# plenty of cards have none — a co-host's, and any the page renders on hover.
# Those people came back as a name and nothing else: unstorable, unmessageable,
# invisible to the batch buttons. The fix is what a person does — open the
# profile, take the id, close it again.

class ProfilePopup(FakePopup):
    """The tab an attendee card opens."""

    def __init__(self, handle, name=""):
        super().__init__({meetup._sel("profile_name"): [{"text": name}]},
                         url=f"https://www.meetup.com/members/{handle}/")


class QueueContext(FakeContext):
    """One tab per click, handed out in order."""

    def __init__(self, popups):
        super().__init__(None)
        self.queue = list(popups)
        self.opened = []

    def expect_page(self, timeout=None):
        self.popup = self.queue.pop(0) if self.queue else None
        if self.popup is not None:
            self.opened.append(self.popup)
        return super().expect_page(timeout)


def attendee_list(rows, popups):
    """An attendee list of `rows`, whose cards open `popups` when clicked."""
    page = FakePage({"attendee_card": [{"visible": True} for _ in rows]},
                    url="https://www.meetup.com/g/events/1/attendees/")
    page.members = rows
    page.context = QueueContext(popups)
    return page


@pytest.fixture
def click_stubs(monkeypatch, stub_bits):
    """_members_on_page reads the page's own rows; the pointer just clicks."""
    async def _members(page):
        return getattr(page, "members", [])

    async def _scroll(page, collect=None, ctl=None, target=0, **kwargs):
        return await collect(page)

    async def _beat(_span):
        return 0.0

    class _Pointer:
        def __init__(self, page):
            self.page = page

        async def click(self, locator, *, swift=False):
            stub_bits["clicks"].append("attendee")
            return True

    monkeypatch.setattr(meetup, "_members_on_page", _members)
    monkeypatch.setattr(meetup.visit, "scroll_to_end", _scroll)
    monkeypatch.setattr(meetup.visit, "dismiss_overlays",
                        lambda page, sel: _beat(None))
    monkeypatch.setattr(meetup.behavior, "beat", _beat)
    monkeypatch.setattr(meetup, "Pointer", _Pointer)
    return stub_bits


def test_a_member_with_no_id_is_clicked_and_their_id_stored(click_stubs):
    rows = [{"username": "", "name": "Alice"}]
    page = attendee_list(rows, [ProfilePopup("alice-99", "Alice")])

    filled = asyncio.run(meetup.ids_by_clicking(page, rows))

    assert filled == 1
    assert rows[0]["username"] == "alice-99"


def test_the_profile_tab_is_closed_the_moment_the_id_is_read(click_stubs):
    """Twenty attendees is twenty opens and twenty closes — never twenty tabs
    left sitting open on the session."""
    rows = [{"username": "", "name": f"P{n}"} for n in range(3)]
    popups = [ProfilePopup(f"p{n}", f"P{n}") for n in range(3)]
    page = attendee_list(rows, popups)

    asyncio.run(meetup.ids_by_clicking(page, rows))

    assert all(p.closed for p in popups)
    assert [r["username"] for r in rows] == ["p0", "p1", "p2"]


def test_every_member_without_an_id_is_clicked(click_stubs):
    rows = [{"username": "", "name": f"P{n}"} for n in range(20)]
    popups = [ProfilePopup(f"p{n}", f"P{n}") for n in range(20)]
    page = attendee_list(rows, popups)

    filled = asyncio.run(meetup.ids_by_clicking(page, rows))

    assert filled == 20
    assert len(page.context.opened) == 20
    assert all(r["username"] for r in rows)


def test_a_member_whose_id_the_card_already_had_is_not_opened(click_stubs):
    """A profile visit per attendee is a page load per attendee. With the id
    already in hand it buys nothing and doubles this account's footprint."""
    rows = [{"username": "alice", "name": "Alice"},
            {"username": "", "name": "Bob"}]
    page = attendee_list(rows, [ProfilePopup("bob-7", "Bob")])

    asyncio.run(meetup.ids_by_clicking(page, rows))

    assert len(page.context.opened) == 1
    assert rows[1]["username"] == "bob-7"


def test_force_opens_every_attendee_even_the_known_ones(click_stubs):
    rows = [{"username": "alice", "name": "Alice"},
            {"username": "bob", "name": "Bob"}]
    page = attendee_list(rows, [ProfilePopup("alice", "Alice"),
                                ProfilePopup("bob", "Bob")])

    asyncio.run(meetup.ids_by_clicking(page, rows, force=True))

    assert len(page.context.opened) == 2


def test_a_profile_that_carries_no_id_does_not_stop_the_rest(click_stubs):
    """One dead profile is one missing id, not a lost roster."""
    rows = [{"username": "", "name": "Alice"}, {"username": "", "name": "Bob"}]
    blank = FakePopup({}, url="https://www.meetup.com/")
    page = attendee_list(rows, [blank, ProfilePopup("bob-7", "Bob")])

    filled = asyncio.run(meetup.ids_by_clicking(page, rows))

    assert filled == 1
    assert rows[0]["username"] == ""
    assert rows[1]["username"] == "bob-7"
    assert blank.closed is True


def test_the_click_limit_stops_the_pass(click_stubs):
    rows = [{"username": "", "name": f"P{n}"} for n in range(10)]
    popups = [ProfilePopup(f"p{n}", f"P{n}") for n in range(10)]
    page = attendee_list(rows, popups)

    asyncio.run(meetup.ids_by_clicking(page, rows, limit=3))

    assert len(page.context.opened) == 3


def test_collect_attendees_clicks_through_the_missing_ids_when_asked(click_stubs):
    """End to end: the roster comes back with ids on people the list itself only
    gave a name for."""
    rows = [{"username": "alice", "name": "Alice"}, {"username": "", "name": "Bob"}]
    page = attendee_list(rows, [ProfilePopup("bob-7", "Bob")])

    members = asyncio.run(meetup.collect_attendees(page, target=0, click_ids=True))

    assert sorted(m["username"] for m in members) == ["alice", "bob-7"]


def test_collecting_does_not_click_by_default(click_stubs):
    """Collecting is what puts the roster on screen. A page load per attendee
    inside it meant a blank table for a minute before anyone appeared — so the
    clicking is its own step, run against the list already on screen."""
    rows = [{"username": "", "name": "Bob"}]
    page = attendee_list(rows, [ProfilePopup("bob-7", "Bob")])

    members = asyncio.run(meetup.collect_attendees(page, target=0))

    assert page.context.opened == []
    assert members[0]["username"] == ""


def test_a_clicked_id_is_never_filed_under_the_wrong_person(click_stubs):
    """Matched by name, not by position: the collected list is accumulated over
    a lazy scroll while the clicking walks the live DOM, and only the identity
    is guaranteed to agree."""
    rows = [{"username": "", "name": "Bob"}, {"username": "", "name": "Alice"}]
    page = attendee_list([{"username": "", "name": "Alice"},
                          {"username": "", "name": "Bob"}],
                         [ProfilePopup("alice-1", "Alice"),
                          ProfilePopup("bob-2", "Bob")])

    asyncio.run(meetup.ids_by_clicking(page, rows))

    assert {r["name"]: r["username"] for r in rows} == {"Alice": "alice-1",
                                                       "Bob": "bob-2"}


# ── Skimming pace on the read-only lists ─────────────────────────────────

def test_a_capped_attendee_read_scrolls_at_the_skimming_pace(monkeypatch):
    """A guest list being run down for names is read-only browsing. The careful
    pacing is for the actions with consequences, and here it was most of the
    wall-clock of a roll.

    The capped read is the one that still collects as it scrolls — an uncapped
    one loads the list to its end first and reads it once, so it can't stop
    half way down.
    """
    seen = {}

    async def _scroll(page, collect=None, ctl=None, target=0, **kwargs):
        seen.update(kwargs)
        return await collect(page)

    async def _members(page):
        return []

    monkeypatch.setattr(meetup.visit, "scroll_to_end", _scroll)
    monkeypatch.setattr(meetup, "_members_on_page", _members)
    asyncio.run(meetup.collect_attendees(FakePage({}), target=20))

    assert seen.get("swift") is True


def test_the_skim_step_is_bigger_and_its_pause_shorter():
    """The two halves of "faster": fewer steps, and less waiting between them."""
    from backend.automation import behavior

    assert behavior.SWIFT_SCROLL_PAUSE_S[1] < behavior.SCROLL_PAUSE_S[0]
    assert behavior.SWIFT_SCROLL_STEP_FRACTION[0] >= behavior.SCROLL_STEP_FRACTION[1]


def test_the_roll_reads_only_the_cards_it_may_open():
    """A roll opens the first card and works down from there. Reading all twelve
    cards' titles, times and attendee counts — several DOM reads each — for a
    list nobody picks from sat between the click and the event."""
    cards = [card(event_id=str(315700000 + n)) for n in range(12)]
    page = FakePage({"event_card": cards, "grid_loading": [], "grid_empty": []},
                    url="https://www.meetup.com/find/")

    rows = asyncio.run(meetup.cards_when_ready(page, limit=5))

    assert len(rows) == 5
    assert rows[0]["event_id"] == "315700000"      # the first card, in order


def test_the_ready_read_still_waits_past_the_shimmer():
    """The one wait that is not optional: it is what tells a loading grid apart
    from an empty category."""
    page = LoadingGrid(polls=4, cards=3)
    rows = asyncio.run(meetup.cards_when_ready(page, limit=5))
    assert len(rows) == 3


def test_an_empty_category_still_reads_as_empty():
    page = FakePage({"event_card": [], "grid_loading": [], "grid_empty": []},
                    url="https://www.meetup.com/find/")
    assert asyncio.run(meetup.cards_when_ready(page, limit=5)) == []


# ── The live attendee card: a button, not a link ─────────────────────────
# The rendered card is <button data-event-label="attendee-card"> with the name
# in an <h3> and NO href anywhere in it. So no attendee has an id until their
# card is clicked, and the click ROUTES this same page rather than opening a
# tab. Reading the URL straight after the click — which is what the first cut
# did — got the attendee list back every time and reported "click opened
# nothing" for a click that had worked.

class RoutingPage(FakePage):
    """An attendee list whose cards route this page to the profile."""

    def __init__(self, names):
        super().__init__({"attendee_card": [{"visible": True} for _ in names]},
                         url="https://www.meetup.com/g/events/1/attendees/")
        self.names = list(names)
        self.members = [{"username": "", "name": n} for n in names]
        self.routed = []
        self.pending = None
        self.context = None            # no context: nothing opens a tab

    def route_to(self, index):
        """What the click does: the URL changes a beat LATER, not at once."""
        self.pending = index

    def settle(self):
        """The beat afterwards — this is what _wait_for_url is waiting for."""
        if self.pending is not None:
            name = self.names[self.pending]
            self.url = f"https://www.meetup.com/members/{name.lower()}-1/"
            self.routed.append(name)
            self.pending = None

    async def go_back(self, **kwargs):
        self.url = "https://www.meetup.com/g/events/1/attendees/"
        # The card whose id we just took now has one, as the live list does not
        # — so it is the `done` set, not the DOM, that stops it being reclicked.


@pytest.fixture
def routing_stubs(monkeypatch, stub_bits):
    """A pointer that routes the page, and a URL wait that lets it settle."""
    async def _members(page):
        return list(getattr(page, "members", []))

    async def _beat(*args, **kwargs):
        return 0.0

    class _Pointer:
        def __init__(self, page):
            self.page = page

        async def click(self, locator, *, swift=False):
            # The index is not in the locator, so route to the next unrouted
            # card — which is the order ids_by_clicking walks them in.
            done = len(self.page.routed)
            self.page.route_to(done)
            return True

    async def _wait_for_url(page, fragment, seconds=8.0):
        page.settle()                  # the route lands while we wait
        return fragment in (page.url or "")

    monkeypatch.setattr(meetup, "_members_on_page", _members)
    monkeypatch.setattr(meetup, "_wait_for_url", _wait_for_url)
    monkeypatch.setattr(meetup.behavior, "beat", _beat)
    monkeypatch.setattr(meetup, "Pointer", _Pointer)
    monkeypatch.setattr(meetup.visit, "scroll_step",
                        lambda page, pointer=None, **kw: _beat())
    return stub_bits


def test_a_card_that_routes_in_place_still_yields_its_id(routing_stubs):
    """THE bug: the id was read before the route had happened."""
    page = RoutingPage(["Alice"])
    rows = list(page.members)

    filled = asyncio.run(meetup.ids_by_clicking(page, rows))

    assert filled == 1
    assert rows[0]["username"] == "alice-1"


def test_every_attendee_on_a_linkless_list_is_clicked(routing_stubs):
    """Fourteen cards, none with a link: every one has to be opened, because
    that is the only place their id exists."""
    names = [f"P{n}" for n in range(14)]
    page = RoutingPage(names)
    rows = list(page.members)

    filled = asyncio.run(meetup.ids_by_clicking(page, rows))

    assert filled == 14
    assert page.routed == names
    assert all(r["username"] for r in rows)


def test_the_list_is_returned_to_between_profiles(routing_stubs):
    """Each click leaves the list; the next index would otherwise be looked for
    on somebody's profile."""
    page = RoutingPage(["Alice", "Bob"])
    asyncio.run(meetup.ids_by_clicking(page, list(page.members)))
    assert page.url.endswith("/attendees/")


# ── Clicking the whole list without leaving it ───────────────────────────
# The cards open the profile in a tab of their own. The list is loaded ONCE and
# never left: one card is clicked, the tab it opens is found by comparing the
# context's tab list against the one taken before the click, its ADDRESS is read
# (the id is in it), and it is closed before the next card is touched.
#
# Not expect_popup, which is what this replaced: that waits a fixed moment and
# gives up, so a tab arriving a beat later belonged to nobody — its id was never
# read and the sweep closed it unread. "The id is right there in the URL and it
# did not take it", with the tab left open beside it.

class ProfileTab(FakePopup):
    def __init__(self, handle):
        super().__init__({}, url=f"https://www.meetup.com/members/{handle}/")


class ListContext:
    """The browser's tab list, which is how an opened profile is found."""

    def __init__(self):
        self.pages = []
        self.opened = []

    def add(self, tab):
        self.pages.append(tab)
        self.opened.append(tab)
        original = tab.close

        async def _close():
            if tab in self.pages:
                self.pages.remove(tab)
            await original()

        tab.close = _close
        return tab

    async def new_page(self):
        raise AssertionError("the list must not be reopened in another tab")


class ListPage(FakePage):
    """An attendee list. `opens` says what a card click does."""

    def __init__(self, names, *, opens="tab"):
        self.context = ListContext()
        self.clicks = 0
        self.loads = 0
        cards = [{"visible": True, "children": {"h3, h4": [{"text": name}]}}
                 for name in names]
        super().__init__({"attendee_card": cards},
                         url="https://www.meetup.com/g/events/1/attendees/")
        self.context.pages.append(self)
        self.names = list(names)
        self.members = [{"username": "", "name": n} for n in names]
        self.opens = opens

        page = self

        def clicker(index):
            def _go():
                page.clicks += 1
                if page.opens == "tab":
                    page.context.add(ProfileTab(f"id-{index}"))
                elif page.opens == "route":
                    page.url = f"https://www.meetup.com/members/id-{index}/"
                elif page.opens == "drawer":
                    page.matches["a[href*='/members/']"] = [
                        {"href": f"https://www.meetup.com/members/id-{index}/"}]
            return _go

        for index, card in enumerate(cards):
            card["on_direct_click"] = clicker(index)

    async def go_back(self, **kwargs):
        self.url = "https://www.meetup.com/g/events/1/attendees/"


@pytest.fixture
def list_click_stubs(monkeypatch):
    async def _beat(*args, **kwargs):
        return 0.0

    async def _members(page):
        return list(getattr(page, "members", []))

    async def _wait_for_url(page, fragment, seconds=8.0):
        return fragment in (page.url or "")

    async def _goto(page, url, **kwargs):
        page.loads = getattr(page, "loads", 0) + 1
        page.url = url
        return True

    monkeypatch.setattr(meetup, "_members_on_page", _members)
    monkeypatch.setattr(meetup, "_wait_for_url", _wait_for_url)
    monkeypatch.setattr(meetup.visit, "goto", _goto)
    monkeypatch.setattr(meetup.behavior, "beat", _beat)


def test_the_list_is_never_reloaded_while_its_cards_are_clicked(list_click_stubs):
    """THE waste this replaced: a tab per member, each loading the same event
    page first. ListContext.new_page raises if anything tries."""
    page = ListPage([f"P{n}" for n in range(12)])
    rows = list(page.members)

    filled = asyncio.run(meetup.collect_ids(page, rows))

    assert filled == 12
    assert page.loads == 0                     # the list was already open
    assert page.url.endswith("/attendees/")    # and never left


def test_every_profile_tab_is_read_and_closed(list_click_stubs):
    page = ListPage(["Colleen", "Aliya", "Bunny"])
    rows = list(page.members)

    asyncio.run(meetup.collect_ids(page, rows))

    assert all(tab.closed for tab in page.context.opened)
    assert page.context.pages == [page]        # nothing left open
    assert {r["name"]: r["username"] for r in rows} == {
        "Colleen": "id-0", "Aliya": "id-1", "Bunny": "id-2"}


def test_five_profiles_are_open_at_once(list_click_stubs, monkeypatch):
    """Five in flight, and all twelve ids.

    The burst that lost them used expect_popup — a fixed wait for "a" popup,
    which with several opening at once handed some clicks the wrong tab and
    some none, so the first few of every burst had ids and the rest read "no
    profile link". Each tab is now bound to its own click by set difference, so
    five in flight are five known people.

    Counted off the CONTEXT — the tabs actually open — rather than off a stub
    holding them open. An earlier version of this test supplied the hold
    itself, which is how it went on passing while the real walk closed every
    tab the instant its address committed and never had two profiles up at
    once. The dwell is the code's, not the test's.
    """
    monkeypatch.setattr(meetup.asyncio, "sleep", yielding_sleep)
    page = ListPage([f"P{n}" for n in range(12)])
    peak = {"n": 0}
    original = meetup._click_card

    async def _watched(p, index):
        row = await original(p, index)
        # AFTER the click, so the tab it just opened is counted: sampled before
        # it, the fill can only ever be seen to reach four.
        peak["n"] = max(peak["n"], len(page.context.pages) - 1)
        return row

    monkeypatch.setattr(meetup, "_click_card", _watched)
    rows = list(page.members)
    filled = asyncio.run(meetup.collect_ids(page, rows, width=5))

    assert filled == 12                        # every one, not the first few
    assert {r["name"]: r["username"] for r in rows}["P7"] == "id-7"
    assert peak["n"] == 5                      # five open together, for real


def test_it_is_the_dwell_that_keeps_the_window_full(list_click_stubs, monkeypatch):
    """THE fix, stated as its own absence.

    With no dwell, a tab is shut the instant its address commits — it has
    already given up the id — and five slots each held for that instant is ONE
    profile on screen, which is what the operator was watching. Same walk, same
    width, dwell taken away: the window collapses.
    """
    monkeypatch.setattr(meetup.asyncio, "sleep", yielding_sleep)
    monkeypatch.setattr(meetup, "_MEMBER_DWELL_S", 0)
    page = ListPage([f"P{n}" for n in range(12)])
    peak = {"n": 0}
    original = meetup._click_card

    async def _watched(p, index):
        row = await original(p, index)
        peak["n"] = max(peak["n"], len(page.context.pages) - 1)
        return row

    monkeypatch.setattr(meetup, "_click_card", _watched)
    filled = asyncio.run(meetup.collect_ids(page, list(page.members), width=5))

    assert filled == 12                        # the ids were never the problem
    assert peak["n"] == 1                      # one profile, one at a time


def test_the_next_card_is_clicked_while_others_are_still_open(list_click_stubs,
                                                              monkeypatch):
    """THE rolling window. The convoy it replaces — open five, wait for the
    slowest, close all five, then start the next five — spent most of its time
    with tabs idle behind one straggler.

    Measured as "how many profile tabs were open when this card was clicked".
    Under a convoy the sixth click finds ZERO open, because the whole tab-full
    was closed before it. Under a window it finds the others still there.

    The one test here that needs a REAL clock: five tabs opened moments apart
    must expire moments apart, and a sleep counted in yields expires all five
    together — which reads exactly like the convoy this is about. The list
    loader is stubbed out because it is the only other thing that waits, and
    the fake list is fully loaded from the start.
    """
    monkeypatch.setattr(meetup.asyncio, "sleep", REAL_SLEEP)
    # Many times longer than the five clicks below take to get in. At a dwell
    # near the fill time the first tab expires as the fifth click lands and the
    # fill reads [0, 1, 2, 3, 3] — which is a scheduling accident, not a
    # convoy, and the margin is what keeps the two apart on a loaded machine.
    monkeypatch.setattr(meetup, "_MEMBER_DWELL_S", 1.0)

    async def _already_loaded(p, ctl=None, cap=0):
        return len(p.names)

    monkeypatch.setattr(meetup, "load_whole_list", _already_loaded)
    page = ListPage([f"P{n}" for n in range(12)])
    open_at_click = []
    click = meetup._click_card

    async def _click(p, index):
        open_at_click.append(len(page.context.pages) - 1)
        # A click costs something — finding the card, the click itself, then
        # waiting for the tab to be identifiable. The fake's is free, and five
        # free clicks put all five tabs on the same clock, so they would expire
        # together and read exactly like the convoy this is about.
        await REAL_SLEEP(0.03)
        return await click(p, index)

    monkeypatch.setattr(meetup, "_click_card", _click)
    asyncio.run(meetup.collect_ids(page, list(page.members), width=5))

    assert open_at_click[:5] == [0, 1, 2, 3, 4]    # the window filling up
    # And staying full. Asked of the clicks AFTER the fill rather than of the
    # sixth alone: the claim is that cards go on being clicked while other
    # profiles are open, and under a convoy every one of these is zero —
    # the whole tab-full is closed before the next one opens.
    assert max(open_at_click[5:]) > 0


def test_the_tab_full_is_closed_before_the_next_one_opens(list_click_stubs):
    """A window with forty profile tabs open is neither usable nor the shape of
    anybody browsing."""
    page = ListPage([f"P{n}" for n in range(12)])
    peak = {"n": 0}
    original = meetup._click_card

    async def _watched(p, index):
        peak["n"] = max(peak["n"], len(page.context.pages) - 1)
        return await original(p, index)

    meetup._click_card = _watched
    try:
        asyncio.run(meetup.collect_ids(page, list(page.members), width=5))
    finally:
        meetup._click_card = original

    assert peak["n"] <= 5
    assert page.context.pages == [page]        # nothing left at the end


def test_a_tab_that_arrives_late_is_still_read(list_click_stubs, monkeypatch):
    """THE lost id. The tab is found by watching the context's tab list for the
    whole budget, so one that opens on the fourth poll counts exactly as much as
    one that opens on the first — under a fixed wait it belonged to nobody."""
    page = ListPage(["Slow"])
    original = meetup._click_card
    held = {}

    async def _slow_click(p, index):
        result = await original(p, index)
        # The click has already queued its tab; hold it back and let it turn up
        # a few polls later instead.
        held["tab"] = page.context.pages.pop()
        held["polls"] = 0
        return result

    async def _sleep(_seconds):
        if "tab" in held:
            held["polls"] += 1
            if held["polls"] >= 4:
                page.context.pages.append(held.pop("tab"))

    monkeypatch.setattr(meetup, "_click_card", _slow_click)
    monkeypatch.setattr(meetup.asyncio, "sleep", _sleep)
    rows = list(page.members)

    assert asyncio.run(meetup.collect_ids(page, rows)) == 1
    assert rows[0]["username"] == "id-0"


def test_a_stray_tab_is_read_before_it_is_closed(list_click_stubs):
    """Closing them blind threw away exactly the ids the run exists to collect,
    and did it silently."""
    page = ListPage(["Alice"])
    stray = page.context.add(ProfileTab("999"))

    found = asyncio.run(meetup._harvest_stray_tabs(page.context, keep=page))

    assert found == ["999"]
    assert stray.closed is True


def test_a_card_that_routes_the_list_is_walked_back(list_click_stubs):
    page = ListPage(["Alice", "Bob"], opens="route")
    rows = list(page.members)

    filled = asyncio.run(meetup.collect_ids(page, rows))

    assert filled == 2
    assert page.url.endswith("/attendees/")


def test_a_card_that_only_opens_a_drawer_is_read_from_its_link(list_click_stubs):
    page = ListPage(["Alice"], opens="drawer")
    rows = list(page.members)

    assert asyncio.run(meetup.collect_ids(page, rows)) == 1
    assert rows[0]["username"] == "id-0"


def test_the_id_is_taken_from_the_address_not_the_page(list_click_stubs, monkeypatch):
    """The id is in the URL — https://www.meetup.com/members/141880022/ — so the
    tab is shut the moment that is known, without waiting for the profile."""
    async def _never(page):
        raise AssertionError("the profile page must not be read for an id")

    monkeypatch.setattr(meetup, "_profile_name", _never)
    page = ListPage(["Colleen", "Aliya"])
    rows = list(page.members)

    assert asyncio.run(meetup.collect_ids(page, rows)) == 2
    assert [r["username"] for r in rows] == ["id-0", "id-1"]


def test_a_card_that_gives_nothing_is_tried_once_more(list_click_stubs, monkeypatch):
    """The misses were transient — a click that did not take — not members
    without an id. A member with no id cannot be messaged at all."""
    page = ListPage(["Flaky"])
    attempts = {"n": 0}
    original = meetup._click_card

    async def _flaky(p, index):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return "Flaky", False, []            # the click did not take
        return await original(p, index)

    monkeypatch.setattr(meetup, "_click_card", _flaky)
    rows = list(page.members)
    filled = asyncio.run(meetup.collect_ids(page, rows))

    assert attempts["n"] == 2
    assert filled == 1
    assert rows[0]["username"] == "id-0"


def test_each_id_is_published_as_it_lands(list_click_stubs):
    """Not at the end of the run: the panel fills in a row at a time."""
    page = ListPage(["A", "B", "C"])
    rows = list(page.members)
    seen = []

    asyncio.run(meetup.collect_ids(
        page, rows, on_found=lambda r: seen.append(
            sum(1 for row in r if row.get("username")))))

    assert seen == [1, 2, 3]



# ── Loading the whole list before touching it ────────────────────────────
# The roster arrives twenty at a time and the next twenty only come when the
# bottom is reached. What made a half-loaded list look finished was patience,
# not the page: three idle scroll rounds at the skimming pace is well under a
# second, which a list fetching its next page over the network loses every
# time. So forty attendees came back as twenty — and the table, the ids and the
# batches all worked from half a list without either half knowing.

class LazyList(FakePage):
    """A list that adds `step` more cards each time the bottom is reached, and
    takes `delay` polls to do it."""

    def __init__(self, total, *, step=20, delay=0):
        self.total = total
        self.step = step
        self.delay = delay
        self.pending = 0
        super().__init__({"attendee_card": [{"visible": True} for _ in range(step)]},
                         url="https://www.meetup.com/g/events/1/attendees/")

    def shown(self):
        return len(self.matches["attendee_card"])

    def reached_bottom(self):
        """A scroll to the end: the next batch is queued, not delivered."""
        if self.shown() < self.total and not self.pending:
            self.pending = self.delay + 1

    def tick(self):
        """One poll of the count. The batch lands when its delay runs out."""
        if self.pending:
            self.pending -= 1
            if not self.pending:
                room = min(self.step, self.total - self.shown())
                self.matches["attendee_card"] += [{"visible": True}
                                                  for _ in range(room)]


@pytest.fixture
def lazy_stubs(monkeypatch):
    """Scrolling reaches the bottom; every count poll advances the clock."""
    async def _scroll(page, pointer=None, **kwargs):
        page.reached_bottom()

    original = meetup._attendee_count

    async def _count(page):
        page.tick()
        return await original(page)

    monkeypatch.setattr(meetup.visit, "scroll_step", _scroll)
    monkeypatch.setattr(meetup, "_attendee_count", _count)


def test_the_list_is_scrolled_until_it_stops_growing(lazy_stubs):
    """THE half roster. Forty attendees, twenty at a time."""
    page = LazyList(40)
    assert asyncio.run(meetup.load_whole_list(page)) == 40


def test_a_slow_batch_is_waited_for(lazy_stubs):
    """A round that fetches nothing YET is not the end of the list. The old
    wait gave up in under a second and called it finished."""
    page = LazyList(40, delay=6)
    assert asyncio.run(meetup.load_whole_list(page)) == 40


def test_a_list_that_is_already_whole_stops_at_once(lazy_stubs):
    page = LazyList(12, step=12)
    assert asyncio.run(meetup.load_whole_list(page)) == 12


def test_the_load_stops_at_a_cap(lazy_stubs):
    """A capped read has a budget to protect — it is not the whole-roster case."""
    page = LazyList(200)
    assert asyncio.run(meetup.load_whole_list(page, cap=20)) == 20


def test_the_id_pass_picks_up_members_that_load_later(list_click_stubs, monkeypatch):
    """More can arrive while the last sweep was clicking, and those are members
    too — so the list is loaded to its end again before the pass gives up."""
    page = ListPage([f"P{n}" for n in range(5)])
    later = [f"L{n}" for n in range(4)]
    loads = {"n": 0}

    async def _load(p, ctl=None, cap=0):
        loads["n"] += 1
        if loads["n"] == 2 and later:          # the rest arrive after sweep one
            for name in later:
                p.names.append(name)
                p.members.append({"username": "", "name": name})
                index = len(p.names) - 1
                card = {"visible": True,
                        "children": {"h3, h4": [{"text": name}]}}
                card["on_direct_click"] = (
                    lambda i=index: p.context.add(ProfileTab(f"id-{i}")))
                p.matches["attendee_card"].append(card)
            later.clear()
        return len(p.names)

    monkeypatch.setattr(meetup, "load_whole_list", _load)
    rows = list(page.members)

    filled = asyncio.run(meetup.collect_ids(page, rows))

    assert filled == 9                          # five, then the four that came
    assert all(r["username"] for r in rows)


# ── The distance filter on /find/ ────────────────────────────────────────
# Meetup searches inside 30km by default, which is why a category comes back
# with four events in it. The roll widens that chip to its last range before it
# picks anything — the LAST one, because the list is ordered narrow to wide and
# what the widest is called ("Any distance", "Any", "100+ kilometers") has
# changed more than once.

@pytest.fixture
def filter_stubs(monkeypatch):
    """A pointer whose click fires the element's own hook, so the fake panel
    can open the way the page's does — the autouse one only records."""
    class _Pointer:
        def __init__(self, page):
            self.page = page
            self.x = self.y = 0.0

        async def click(self, locator, *, swift=False):
            element = locator._elements[0] if locator._elements else {}
            # Either hook: a pointer press and a direct click land on the same
            # element and the page cannot tell them apart, which is the whole
            # reason the code tries one and falls back to the other.
            hook = element.get("on_click") or element.get("on_direct_click")
            if hook:
                hook()
            return True

        async def move_to(self, x, y, steps=None, travel=None):
            """Where the drag starts from. The slider is DRAGGED rather than
            typed at — see meetup._drag_slider_to_max — so a pointer that cannot
            move is a pointer that cannot take a range."""
            self.x, self.y = x, y
            mouse = getattr(self.page, "mouse", None)
            if mouse is not None:
                await mouse.move(x, y)

    monkeypatch.setattr(meetup, "Pointer", _Pointer)


def filter_page(ranges, *, chip="Within 30 kilometers", slider=False):
    """A Find page whose distance chip opens a panel of `ranges`."""
    page = FakePage({"filter_distance": [], "filter_panel": [],
                     "filter_option": [], "filter_slider": [], "filter_apply": []},
                    url=meetup.FIND_URL)
    button = {"data-testid": "RevampedFilterButtonDistance", "text": chip,
              "visible": True}
    taken = []

    def _pick(label):
        taken.append(label)
        button["text"] = label                 # the chip says what it now is

    def _open():
        options = [{"visible": True, "text": label,
                    "on_direct_click": (lambda l=label: _pick(l))}
                   for label in ranges]
        sliders = ([{"visible": True, "on_press": taken.append}]
                   if slider else [])
        # Inside the panel, which is where the code looks for them — "the last
        # menu item on the page" is a different element on a page with a nav.
        # Keyed by the selectors the LIVE panel answers to: Radix renders each
        # range as div[role='menuitem'][data-radix-collection-item].
        page.matches["filter_panel"] = [{"visible": True, "children": {
            "[role='menuitem'][data-radix-collection-item]": options,
            "input[type='range']": sliders,
        }}]

    button["on_click"] = _open
    page.matches["filter_distance"] = [button]
    page.taken = taken
    return page


def test_the_widest_range_is_the_one_taken(filter_stubs):
    page = filter_page(["Within 2 kilometers", "Within 30 kilometers",
                        "Within 100 kilometers", "Any distance"])

    result = asyncio.run(meetup.widen_distance(page))

    assert page.taken == ["Any distance"]      # the last, not the first match
    assert result["ok"] is True
    assert result["label"] == "Any distance"


def test_the_live_panel_s_own_order_is_handled(filter_stubs):
    """THE panel, as Meetup renders it: the current setting is hoisted to the
    top and the rest follow ascending — "Within 30 kilometers, 5, 10, 25, 50,
    100, 150". The widest is the last one here, and taking it is the point."""
    page = filter_page(["Within 30 kilometers", "5 kilometers", "10 kilometers",
                        "25 kilometers", "50 kilometers", "100 kilometers",
                        "150 kilometers"])

    result = asyncio.run(meetup.widen_distance(page))

    assert page.taken == ["150 kilometers"]
    assert result["ok"] is True


def test_the_widest_is_read_off_the_numbers_not_the_position(filter_stubs):
    """The trap in that order: with 150 already selected the list reads
    "Within 150 kilometers, 5, 10, 25, 50, 100" and ENDS at 100. Taking the
    last would narrow the search on every roll while reporting a widening."""
    page = filter_page(["Within 150 kilometers", "5 kilometers", "10 kilometers",
                        "25 kilometers", "50 kilometers", "100 kilometers"],
                       chip="Within 150 kilometers")

    result = asyncio.run(meetup.widen_distance(page))

    assert page.taken == ["Within 150 kilometers"]   # the widest, not the last
    assert result["ok"] is True
    assert "already" in result["detail"]


def test_a_click_that_did_not_take_is_reported_as_one(filter_stubs):
    """A menu item takes a click and the menu closes whether or not anything
    was applied. The chip is what says which happened."""
    page = filter_page(["5 kilometers", "150 kilometers"])
    page.matches["filter_distance"][0]["on_click"] = lambda: page.matches.update(
        {"filter_panel": [{"visible": True, "children": {
            "[role='menuitem'][data-radix-collection-item]": [
                {"visible": True, "text": "5 kilometers"},
                {"visible": True, "text": "150 kilometers"}],   # no hook: inert
            "input[type='range']": []}}]})

    result = asyncio.run(meetup.widen_distance(page))

    assert result["ok"] is False
    assert result["detail"] == ("Picked 150 kilometers but the filter still "
                                "reads Within 30 kilometers")


def test_the_chip_is_read_back_rather_than_the_click_believed(filter_stubs):
    """The one thing that can tell "took the widest" from "clicked something":
    what the chip says afterwards."""
    page = filter_page(["Within 2 kilometers", "Any distance"])

    result = asyncio.run(meetup.widen_distance(page))

    assert "Any distance" in result["detail"]


def test_a_range_already_at_its_widest_is_not_reported_as_a_change(filter_stubs):
    page = filter_page(["Any distance"], chip="Any distance")

    result = asyncio.run(meetup.widen_distance(page))

    assert result["ok"] is True
    assert "already" in result["detail"]


def test_a_slider_is_taken_to_its_maximum(filter_stubs):
    """Some builds render the range as a slider. Its maximum is the same choice
    said differently.

    It is DRAGGED where it can be — a keypress on a React-controlled slider
    moves the DOM and is re-rendered away, which is the whole of
    test_distance_filter.py. This fake gives the control no box, so there is
    nothing to aim a pointer at and the keyboard fallback takes it: End first,
    then a walk on ArrowRight because the fake reports no value to confirm the
    End with.
    """
    page = filter_page(["ignored"], slider=True)

    result = asyncio.run(meetup.widen_distance(page))

    assert page.taken[0] == "End"              # the slider, not the list
    assert set(page.taken) == {"End", "ArrowRight"}
    assert result["ok"] is True


def test_a_page_with_no_distance_chip_is_not_a_failure(filter_stubs):
    """Best-effort by design: a roll that cannot find the chip is still a roll.
    It runs on whatever radius the page is set to and says which."""
    page = FakePage({}, url=meetup.FIND_URL)

    result = asyncio.run(meetup.widen_distance(page))

    assert result["ok"] is False
    assert "No distance filter" in result["detail"]


def test_a_chip_that_opens_nothing_leaves_the_range_alone(filter_stubs):
    page = filter_page([])                     # opens, offers nothing

    result = asyncio.run(meetup.widen_distance(page))

    assert result["ok"] is False
    assert result["label"] == "Within 30 kilometers"
    assert "nothing to pick" in result["detail"]


# ── Walking the list and opening as it goes ──────────────────────────────
# Reading the list IS opening the profiles. The order this replaced — collect
# every name, scroll to the end, then go back and click them all — made the
# operator watch the same list walked twice with a button press in between,
# for the same result.

def test_the_first_screenful_is_opened_before_the_list_is_scrolled(list_click_stubs,
                                                                   monkeypatch):
    """On the spot: the cards on screen are opened before anything scrolls for
    more, which is the whole difference from collecting first."""
    page = ListPage([f"P{n}" for n in range(5)])
    order = []

    original_click = meetup._click_card
    original_scroll = meetup.visit.scroll_step

    async def _click(p, index):
        order.append("click")
        return await original_click(p, index)

    async def _scroll(p, pointer=None, **kwargs):
        order.append("scroll")
        return await original_scroll(p, pointer, **kwargs)

    monkeypatch.setattr(meetup, "_click_card", _click)
    monkeypatch.setattr(meetup.visit, "scroll_step", _scroll)

    rows = asyncio.run(meetup.harvest_attendees(page, width=5))

    assert [r["username"] for r in rows] == [f"id-{n}" for n in range(5)]
    assert order[0] == "click"                 # opened before scrolled
    assert order.index("scroll") > order.index("click")


def test_members_that_load_later_are_walked_too(list_click_stubs, monkeypatch):
    """The list grows as it is scrolled; the walk keeps going until a scroll
    stops producing anyone new."""
    page = ListPage([f"P{n}" for n in range(5)])
    added = {"done": False}

    async def _scroll(p, pointer=None, **kwargs):
        if added["done"]:
            return
        added["done"] = True
        for n in range(5, 9):
            name = f"P{n}"
            p.names.append(name)
            p.members.append({"username": "", "name": name})
            card = {"visible": True, "children": {"h3, h4": [{"text": name}]}}
            card["on_direct_click"] = (
                lambda i=n: p.context.add(ProfileTab(f"id-{i}")))
            p.matches["attendee_card"].append(card)

    monkeypatch.setattr(meetup.visit, "scroll_step", _scroll)
    rows = asyncio.run(meetup.harvest_attendees(page, width=5))

    assert len(rows) == 9
    assert rows[-1]["username"] == "id-8"


def test_the_walk_publishes_each_member_as_they_land(list_click_stubs):
    """Per member, not per batch: with a rolling window there are no batches —
    people arrive one at a time as their tab closes, and that is how the table
    fills. Forty page loads with nothing on screen until the end is not
    something anybody can watch."""
    page = ListPage([f"P{n}" for n in range(10)])
    seen = []

    asyncio.run(meetup.harvest_attendees(
        page, width=5, on_found=lambda rows: seen.append(len(rows))))

    assert seen[0] == 1                        # the first one to close
    assert seen[-1] == 10
    assert len(seen) >= 10                     # published as each landed


def test_the_walk_stops_at_a_target(list_click_stubs):
    page = ListPage([f"P{n}" for n in range(20)])
    rows = asyncio.run(meetup.harvest_attendees(page, width=5, target=10))
    assert len(rows) == 10


def test_the_walk_keeps_the_list_order(list_click_stubs):
    """A tab-full is read concurrently, so the results come back in whatever
    order they finish — the roster must still read top to bottom."""
    page = ListPage([f"P{n}" for n in range(8)])
    rows = asyncio.run(meetup.harvest_attendees(page, width=4))
    assert [r["name"] for r in rows] == [f"P{n}" for n in range(8)]


# ── Sweeping ONE category ────────────────────────────────────────────────


from backend.automation import campaign  # noqa: E402  — used only below


class SweepCtl:
    """A control.Run stand-in: counts, never stops."""

    def __init__(self):
        self.total = 0
        self.done = 0
        self.meta: dict = {}

    async def checkpoint(self, label=None):
        return True

    def advance(self, n=1, label=None):
        self.done += n


# The pool used to be labels — the twenty tabs on screen and none of them
# choosable — and a sweep always worked down All events. Clicking a category now
# sweeps that tab instead.


def test_a_sweep_works_down_the_tab_it_is_given(monkeypatch):
    """The key reaches click_category, which is what puts the run on that tab."""
    clicked = []

    async def _current(profile=None):
        return FakePage({}, url=meetup.FIND_URL)

    async def _open_find(page):
        return {"ok": True, "detail": "on find"}

    async def _widen(page):
        return {"ok": True, "label": "Any distance", "detail": "widened"}

    async def _click(page, key):
        clicked.append(key)
        return {"ok": False, "detail": "stop here — the tab is what is tested"}

    monkeypatch.setattr(campaign.browser, "current_page", _current)
    monkeypatch.setattr(campaign.meetup, "open_find", _open_find)
    monkeypatch.setattr(campaign.meetup, "widen_distance", _widen)
    monkeypatch.setattr(campaign.meetup, "click_category", _click)

    asyncio.run(campaign.sweep_events(SweepCtl(), key="652"))

    assert clicked == ["652"]


def test_a_sweep_with_no_key_still_works_down_all_events(monkeypatch):
    """The older shape — the button in the panel head sends no category."""
    clicked = []

    async def _current(profile=None):
        return FakePage({}, url=meetup.FIND_URL)

    async def _open_find(page):
        return {"ok": True, "detail": "on find"}

    async def _widen(page):
        return {"ok": True, "label": "Any distance", "detail": "widened"}

    async def _click(page, key):
        clicked.append(key)
        return {"ok": False, "detail": "stop here"}

    monkeypatch.setattr(campaign.browser, "current_page", _current)
    monkeypatch.setattr(campaign.meetup, "open_find", _open_find)
    monkeypatch.setattr(campaign.meetup, "widen_distance", _widen)
    monkeypatch.setattr(campaign.meetup, "click_category", _click)

    asyncio.run(campaign.sweep_events(SweepCtl()))

    assert clicked == [campaign.SWEEP_TAB]
