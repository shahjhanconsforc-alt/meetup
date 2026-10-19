""""The distance filter, and why it kept snapping back to the narrowest range.

/find/ searches inside 30km by default, which is why a category comes back with
four events on it. widen_distance opens the chip and takes the widest range —
but on the SLIDER builds the operator watched it jump to the maximum and then go
straight back to the first value a moment later.

TWO causes, both fixed here and both modelled below.

  1. The pick was made with a KEYPRESS. End moves a native input's DOM value,
     but Meetup's filter is React-controlled: the component re-renders from its
     own state a beat later and puts the thumb back. The jump was real and so
     was the snap back. A drag fires the pointer events the component's own
     handlers listen for, so it changes the STATE and there is nothing to
     revert to.

  2. The panel was shut with ESCAPE, which on a Radix popover is CANCEL — so
     even a pick that had taken was thrown away on the way out.

And the slider path returned no label, which switched widen_distance's own
verification off entirely, so neither of those ever reported anything.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.automation import meetup
from tests.test_categories import filter_page, filter_stubs  # noqa: F401


@pytest.fixture(autouse=True)
def instant(monkeypatch):
    async def _zero(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(meetup.asyncio, "sleep", _zero)
    monkeypatch.setattr(meetup.behavior, "beat", _zero)


TRACK = {"x": 100.0, "y": 200.0, "width": 200.0, "height": 8.0}


class Mouse:
    """Enough of Playwright's mouse to model a drag on a range control."""

    def __init__(self, page):
        self.page = page
        self.x = 0.0
        self.y = 0.0
        self.down_at = None

    async def move(self, x, y):
        self.x, self.y = x, y
        if self.down_at is not None:
            self.page.on_drag(x)

    async def down(self):
        self.down_at = (self.x, self.y)
        self.page.on_drag(self.x)

    async def up(self):
        self.down_at = None

    async def click(self, x, y):
        # Off the popover: a dismiss, NOT a cancel. That is what separates it
        # from Escape, and why the pick survives it.
        self.page.matches["filter_panel"] = []


class Keyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        if key == "Escape":
            self.page.on_escape()


def react_slider_page(*, steps=4, controlled=True, apply_button=False,
                      escape_cancels=True, has_box=True):
    """A Find page whose distance filter is a React-controlled slider.

    `controlled` is the bug: a KEYPRESS moves the rendered value and then the
    component re-renders from its own state and puts it straight back. A DRAG
    goes through the component, so it sticks. Set it False to model the plain
    <input type=range> builds where the keyboard does work.
    """
    page = filter_page([], slider=True)
    state = {"value": 0, "committed": 0}      # committed = what React holds
    box = {"visible": True, "aria-valuenow": "0", "box": dict(TRACK) if has_box
           else None}

    def _render():
        box["aria-valuenow"] = str(state["value"])

    def _press(key):
        page.taken.append(key)
        if key == "End":
            state["value"] = steps
        elif key == "ArrowRight":
            state["value"] = min(steps, state["value"] + 1)
        _render()
        if controlled:
            # THE bug: React re-renders from its own state, which the keypress
            # never reached.
            state["value"] = state["committed"]
            _render()

    def _drag(x):
        fraction = (x - TRACK["x"]) / TRACK["width"]
        state["value"] = max(0, min(steps, round(fraction * steps)))
        # A drag goes through the component, so state and render agree.
        if not apply_button:
            state["committed"] = state["value"]
        _render()
        page.dragged = True
        if apply_button:
            page.matches["filter_apply"] = [{
                "visible": True,
                "on_direct_click": lambda: state.update(committed=state["value"]),
            }]

    def _open():
        page.matches["filter_panel"] = [{"visible": True, "children": {
            "[role='menuitem'][data-radix-collection-item]": [],
            "input[type='range']": [box],
        }}]

    def _escape():
        page.matches["filter_panel"] = []
        if escape_cancels:
            state["value"] = state["committed"]
            _render()

    box["on_press"] = _press
    page.matches["filter_distance"][0]["on_click"] = _open
    page.matches["filter_distance"][0]["on_direct_click"] = \
        lambda: page.matches.__setitem__("filter_panel", [])
    page.on_drag = _drag
    page.on_escape = _escape
    # Exposed so a test can model a page that rolls the value back: state alone
    # is not what the code reads — it reads the rendered attribute, and a fake
    # that changes one without the other tests nothing.
    page.render = _render
    page.state = state
    page.dragged = False
    page.keyboard = Keyboard(page)
    page.mouse = Mouse(page)
    return page


# ── THE bug the operator watched ─────────────────────────────────────────


def test_a_react_slider_keeps_the_widest_range(filter_stubs):
    """It took the last value and then went back to the first. Twice reported,
    because the first fix (not closing with Escape) only covered half of it."""
    page = react_slider_page(steps=4)

    result = asyncio.run(meetup.widen_distance(page))

    assert page.dragged is True                # dragged, not typed at
    assert page.state["committed"] == 4        # and it stuck
    assert result["ok"] is True


def test_the_slider_is_dragged_rather_than_pressed(filter_stubs):
    """The mechanism. A keypress moves the DOM and gets re-rendered away; only
    a real pointer reaches the component's own handlers."""
    page = react_slider_page(steps=4)

    asyncio.run(meetup.widen_distance(page))

    assert page.taken == []                    # End was never needed


def test_escape_is_not_what_closes_a_panel_something_was_picked_in(filter_stubs):
    """The other half: Escape on a Radix popover is cancel."""
    page = react_slider_page(steps=4)
    pressed = []
    page.keyboard.press = lambda key: pressed.append(key) or _done()

    asyncio.run(meetup.widen_distance(page))

    assert "Escape" not in pressed


def _done():
    fut = asyncio.get_event_loop().create_future()
    fut.set_result(None)
    return fut


def test_a_value_that_reverts_after_the_panel_shuts_is_set_again(filter_stubs):
    """Checked AFTER the close, which is the only moment that answers it — a
    check made while the panel was open passed every time."""
    page = react_slider_page(steps=4)
    rolled = {"n": 0}
    real_click = page.mouse.click

    # A page that rolls the value back the FIRST time the panel closes, and
    # keeps it thereafter — which is only visible from outside the panel.
    async def _click(x, y):
        await real_click(x, y)
        rolled["n"] += 1
        if rolled["n"] == 1:
            page.state["value"] = page.state["committed"] = 0
            page.render()
    page.mouse.click = _click

    result = asyncio.run(meetup.widen_distance(page))

    assert result["ok"] is True
    assert page.state["committed"] == 4


def test_a_slider_that_will_not_move_at_all_is_reported(filter_stubs):
    """Nothing takes: not the drag, not the keyboard. It used to return ok
    regardless — the slider path handed back no label, and the label is what
    widen_distance checked."""
    page = react_slider_page(steps=4)
    page.on_drag = lambda x: setattr(page, "dragged", True)

    result = asyncio.run(meetup.widen_distance(page))

    assert result["ok"] is False
    assert "did not move" in result["detail"]


def test_a_value_that_will_not_hold_says_the_run_is_on_the_default(filter_stubs):
    """It takes, then the page rolls it back — every time, not just once. One
    retry, then the truth: a run quietly searching 30km is worse than one that
    says it is."""
    page = react_slider_page(steps=4)
    # Hooked on the CLOSE the code actually performs — clicking away from the
    # popover — not on Escape, which it deliberately no longer uses.
    real_click = page.mouse.click

    async def _click(x, y):
        await real_click(x, y)
        page.state["value"] = page.state["committed"] = 0
        page.render()
    page.mouse.click = _click

    result = asyncio.run(meetup.widen_distance(page))

    assert result["ok"] is False
    assert "default radius" in result["detail"]


# ── What must not change ─────────────────────────────────────────────────


def test_an_apply_button_rendered_by_the_pick_is_still_pressed(filter_stubs):
    """It is not in the DOM at the moment the range moves — looking once found
    nothing and the change was never committed."""
    page = react_slider_page(steps=4, apply_button=True)

    result = asyncio.run(meetup.widen_distance(page))

    assert page.state["committed"] == 4
    assert result["ok"] is True


def test_the_keyboard_still_works_where_it_works(filter_stubs):
    """A plain <input type=range> answers End. The drag is tried first and this
    is the fallback, not the other way round."""
    page = react_slider_page(steps=4, controlled=False, has_box=False)

    result = asyncio.run(meetup.widen_distance(page))

    assert page.dragged is False               # no box, so no drag was possible
    assert "End" in page.taken
    assert result["ok"] is True


def test_a_slider_that_publishes_no_value_says_it_is_unconfirmed(filter_stubs):
    """Neither a claim that it worked nor a failure — there is nothing to check
    against, and the run carries on either way."""
    page = react_slider_page(steps=4)

    def _open():
        page.matches["filter_panel"] = [{"visible": True, "children": {
            "[role='menuitem'][data-radix-collection-item]": [],
            # No aria-valuenow, no value: a control that says nothing.
            "input[type='range']": [{"visible": True, "box": dict(TRACK),
                                     "on_press": lambda k: None}],
        }}]

    page.matches["filter_distance"][0]["on_click"] = _open
    page.on_drag = lambda x: None

    result = asyncio.run(meetup.widen_distance(page))

    assert result["ok"] is True
    assert "not confirmed" in result["detail"]


def test_a_list_of_ranges_still_takes_the_widest(filter_stubs):
    """The other panel shape, untouched by any of this."""
    page = filter_page(["Within 2 kilometers", "Within 30 kilometers",
                        "Any distance"])
    page.keyboard = Keyboard(page)
    page.mouse = Mouse(page)
    page.on_escape = lambda: page.matches.__setitem__("filter_panel", [])

    result = asyncio.run(meetup.widen_distance(page))

    assert page.taken == ["Any distance"]
    assert result["ok"] is True
