"""Where a scrape searches FROM.

Meetup's header carries a location combobox, and everything /find/ returns is
relative to it. A run that never touched it searched from wherever the profile
was last pointed — so every run of this bot looked like the same person in the
same place, which is the pattern the rest of this app spends its effort not
producing.

So a scrape picks one of the operator's cities at RANDOM, types it in, and takes
the FIRST suggestion. Taking the suggestion is the whole point: it is a
combobox, and typed text that was never chosen changes nothing.
"""
from __future__ import annotations

import asyncio

import pytest

from backend import settings as app_settings
from backend.automation import campaign, meetup
from tests.test_first_chat import FakePage


@pytest.fixture(autouse=True)
def instant(monkeypatch):
    async def _zero(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(meetup.asyncio, "sleep", _zero)
    monkeypatch.setattr(meetup.human, "think", _zero)
    monkeypatch.setattr(meetup.human, "pause", _zero)


@pytest.fixture
def clicks(monkeypatch):
    """A pointer that fires the element's own hook, and typing that fills the
    box the way the real one does."""
    seen: list[str] = []

    class _Pointer:
        def __init__(self, page):
            self.page = page

        async def click(self, locator, *, swift=False):
            element = locator._elements[0] if locator._elements else {}
            seen.append(element.get("text", ""))
            hook = element.get("on_click")
            if hook:
                hook()
            return True

    async def _type(page, selector, text, clear=False, ctl=None):
        page.typed = text
        page.on_type(text)
        return len(text)

    monkeypatch.setattr(meetup, "Pointer", _Pointer)
    monkeypatch.setattr(meetup.human, "type_or_fill", _type)
    return seen


def search_page(*, suggestions=("Tokyo, JP", "Tokyo, GA, US"), value="Delhi, IN",
                fills=True):
    """A Find page with a location box that drops suggestions as it is typed.

    `fills` False models the case that matters: the menu opens and a click lands,
    but the box is left holding exactly what was typed — nothing was chosen.
    """
    page = FakePage({}, url=meetup.FIND_URL)
    box = {"visible": True, "value": value}
    state = {"value": value}

    def _pick(label):
        if fills:
            state["value"] = label
            box["value"] = label
        page.matches["search_location_option"] = []

    def _on_type(text):
        state["typed"] = text
        box["value"] = text
        page.matches["search_location_option"] = [
            {"visible": True, "text": s, "on_click": (lambda l=s: _pick(l))}
            for s in suggestions]

    page.matches["search_location"] = [box]
    page.matches["search_location_option"] = []
    page.on_type = _on_type
    page.state = state
    return page


# ── Typing a city and taking a suggestion ────────────────────────────────


def test_the_first_suggestion_is_the_one_taken(clicks):
    """Not the closest match, not a guess — the first, which is what a person
    pressing Enter on that box gets."""
    page = search_page()

    result = asyncio.run(meetup.set_location(page, "Tokyo"))

    assert clicks == ["Tokyo, JP"]
    assert result["ok"] is True
    assert result["label"] == "Tokyo, JP"


def test_typed_text_with_nothing_chosen_is_not_a_success(clicks):
    """THE trap in a combobox. Typing "Tokyo" and carrying on leaves the search
    wherever the profile was last pointed, while the box reads Tokyo — a run
    that called that ok would search the wrong city and say it had moved."""
    page = search_page(fills=False)

    result = asyncio.run(meetup.set_location(page, "Tokyo"))

    assert result["ok"] is False
    assert "no suggestion was taken" in result["detail"]


def test_no_suggestions_at_all_is_reported(clicks):
    page = search_page(suggestions=())

    result = asyncio.run(meetup.set_location(page, "Nowhereville"))

    assert result["ok"] is False
    assert "No suggestions" in result["detail"]
    assert clicks == []


def test_a_page_with_no_location_box_is_not_fatal(clicks):
    """Best-effort, like the distance filter: the run searches from wherever the
    session already is and says so."""
    page = FakePage({}, url=meetup.FIND_URL)

    result = asyncio.run(meetup.set_location(page, "Tokyo"))

    assert result["ok"] is False
    assert "No location box" in result["detail"]


def test_an_empty_city_is_refused_before_anything_is_typed(clicks):
    page = search_page()

    result = asyncio.run(meetup.set_location(page, "   "))

    assert result["ok"] is False
    # `typed` on the fake is FakePage's own list of what was sent to a composer.
    # The marker for "this box was typed into" is the page's own state.
    assert "typed" not in page.state


# ── The list of cities ───────────────────────────────────────────────────


def test_the_default_list_is_ten_cities():
    assert len(app_settings.cities(app_settings.defaults())) == 10


def test_the_list_is_split_on_commas_and_trimmed():
    values = {"search_cities": " Tokyo ,Delhi,  Osaka "}
    assert app_settings.cities(values) == ["Tokyo", "Delhi", "Osaka"]


def test_blanks_and_duplicates_are_dropped():
    values = {"search_cities": "Tokyo,,Delhi, tokyo ,Delhi"}
    assert app_settings.cities(values) == ["Tokyo", "Delhi"]


def test_an_empty_box_means_no_cities():
    assert app_settings.cities({"search_cities": "  "}) == []


def test_the_panel_is_told_the_cities_are_a_list():
    """The Settings panel renders itself from the schema, and a list of ten
    cities as one 60-character line is a line nobody edits — so the type says
    `list` and the panel gives them a row each."""
    row = next(r for r in app_settings.schema() if r["key"] == "search_cities")

    assert row["type"] == "list"


def test_the_link_template_is_still_a_plain_text_setting():
    """Only settings named in LIST_SETTINGS get the row editor. A URL is one
    value, not several, and splitting it on its commas would be nonsense."""
    row = next(r for r in app_settings.schema() if r["key"] == "chat_link")

    assert row["type"] == "text"


def test_the_list_is_still_stored_as_one_string():
    """The editor is a screen, not a format. Everything that reads this setting
    — including cities() above — gets the same comma-separated string it always
    did, so nothing downstream had to learn about the change."""
    values = app_settings.update({"search_cities": "Tokyo, Delhi"})

    assert values["search_cities"] == "Tokyo, Delhi"
    assert app_settings.cities(values) == ["Tokyo", "Delhi"]


# ── Picking one, per run ─────────────────────────────────────────────────


class Ctl:
    def __init__(self):
        self.total = self.done = 0
        self.meta: dict = {}

    async def checkpoint(self, label=None):
        return True

    def advance(self, n=1, label=None):
        self.done += n


def test_the_city_is_picked_at_random_rather_than_first(monkeypatch):
    """A list whose first entry is used every time is a fixed home city with
    extra steps, and every run searching from the same place is one more thing
    that ties the runs together."""
    asked = []

    async def _set(page, city):
        asked.append(city)
        return {"ok": True, "city": city, "label": f"{city}, XX",
                "detail": f"Searching from {city}"}

    monkeypatch.setattr(meetup, "set_location", _set)
    monkeypatch.setattr(campaign.app_settings, "cities",
                        lambda *a, **k: ["A", "B", "C", "D", "E"])

    for _ in range(40):
        asyncio.run(campaign._search_from_a_city(None, Ctl()))

    assert len(set(asked)) > 1          # not always the same one
    assert set(asked) <= {"A", "B", "C", "D", "E"}


def test_the_chosen_city_rides_on_the_run(monkeypatch):
    """The panel shows where a scrape is searching from — a run whose events are
    all in a city nobody chose is otherwise unexplainable."""
    async def _set(page, city):
        return {"ok": True, "city": city, "label": "Tokyo, JP",
                "detail": "Searching from Tokyo, JP"}

    monkeypatch.setattr(meetup, "set_location", _set)
    monkeypatch.setattr(campaign.app_settings, "cities", lambda *a, **k: ["Tokyo"])

    ctl = Ctl()
    asyncio.run(campaign._search_from_a_city(None, ctl))

    assert ctl.meta["city"] == "Tokyo, JP"


def test_an_empty_city_list_leaves_the_search_alone(monkeypatch):
    """Not an error. The operator emptied the box, and the run searches from
    wherever the session is."""
    called = []

    async def _set(page, city):
        called.append(city)
        return {"ok": True}

    monkeypatch.setattr(meetup, "set_location", _set)
    monkeypatch.setattr(campaign.app_settings, "cities", lambda *a, **k: [])

    ctl = Ctl()
    asyncio.run(campaign._search_from_a_city(None, ctl))

    assert called == []
    assert ctl.meta["city"] == ""
