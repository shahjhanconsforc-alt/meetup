"""Several accounts open at once — one browser window per profile.

The runner used to hold ONE context: opening a second account closed the first.
It now holds a registry keyed by profile directory, so windows sit side by side.
Two rules survive that change and are what these tests pin:

  * never two Chrome processes on the SAME profile — that corrupts it, so a
    second ask for an open profile gets the window already up;
  * closing one account's window never touches another's.

Playwright itself is faked. What is under test is the bookkeeping — which window
exists, which one a profile-less call means, and what a hand-closed window does
to both — none of which needs a real browser.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.automation import playwright_runner as runner


class FakeBrowser:
    def __init__(self):
        self.connected = True

    def is_connected(self):
        return self.connected


class FakePage:
    def __init__(self):
        self.url = ""


class FakeContext:
    """Enough of a BrowserContext for the registry to be exercised."""

    def __init__(self, profile: str):
        self.profile = profile
        self.pages: list[FakePage] = []
        self.browser = FakeBrowser()
        self.closed = False
        self._on_close = []

    # ── what _launch calls ────────────────────────────────────────────
    async def add_init_script(self, _script):
        pass

    def set_default_timeout(self, _ms):
        pass

    def on(self, event, callback):
        if event == "close":
            self._on_close.append(callback)

    # ── what the rest of the module calls ─────────────────────────────
    async def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    async def close(self):
        self.closed = True
        self.browser.connected = False
        for callback in self._on_close:
            callback(self)

    # ── the operator's X, which tells nobody ──────────────────────────
    def die(self):
        """The window closed from outside, with no event delivered. This IS the
        case the registry has to notice by asking rather than being told."""
        self.browser.connected = False


class FakeChromium:
    def __init__(self):
        self.launched: list[str] = []

    async def launch_persistent_context(self, **kwargs):
        profile = kwargs["user_data_dir"]
        self.launched.append(profile)
        return FakeContext(profile)


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()

    async def stop(self):
        pass


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    """A fresh, empty registry over a fake Playwright."""
    fake = FakePlaywright()
    monkeypatch.setattr(runner, "_playwright", fake)
    monkeypatch.setattr(runner, "_contexts", {})
    monkeypatch.setattr(runner, "_active", None)
    monkeypatch.setattr(runner, "_closing", set())

    def profile(name: str):
        path = tmp_path / name
        path.mkdir(parents=True, exist_ok=True)   # so no MinIO restore is tried
        return path

    return {"pw": fake, "profile": profile}


def test_a_second_account_opens_beside_the_first(runtime):
    """THE change. Opening account two used to close account one's window."""
    one, two = runtime["profile"]("acct-1"), runtime["profile"]("acct-2")

    first = asyncio.run(runner.get_context(one))
    second = asyncio.run(runner.get_context(two))

    assert first is not second
    assert first.closed is False
    assert runner.open_profiles() == [one, two]


def test_the_same_profile_never_gets_a_second_browser(runtime):
    """Two Chrome processes on one profile corrupt it. The second ask must be
    handed the window that is already up."""
    one = runtime["profile"]("acct-1")

    first = asyncio.run(runner.get_context(one))
    again = asyncio.run(runner.get_context(one))

    assert again is first
    assert runtime["pw"].chromium.launched.count(str(one)) == 1


def test_closing_one_window_leaves_the_others_alone(runtime):
    one, two = runtime["profile"]("acct-1"), runtime["profile"]("acct-2")
    first = asyncio.run(runner.get_context(one))
    second = asyncio.run(runner.get_context(two))

    result = asyncio.run(runner.close_browser(profile=one))

    assert result["closed"] is True
    assert first.closed is True
    assert second.closed is False
    assert runner.open_profiles() == [two]


def test_closing_with_no_profile_closes_every_window(runtime):
    """What the panel's Close browser button and a session-wide clear mean."""
    one, two = runtime["profile"]("acct-1"), runtime["profile"]("acct-2")
    asyncio.run(runner.get_context(one))
    asyncio.run(runner.get_context(two))

    result = asyncio.run(runner.close_browser())

    assert result["count"] == 2
    assert runner.open_profiles() == []


def test_closing_a_profile_that_is_not_open_says_so(runtime):
    one = runtime["profile"]("acct-1")
    result = asyncio.run(runner.close_browser(profile=one))

    assert result["closed"] is False
    assert "not open" in result["detail"]


def test_a_hand_closed_window_is_dropped_and_relaunches(runtime):
    """The operator's X. The registry has to notice by asking, then open a NEW
    window on the next press rather than handing back the corpse."""
    one = runtime["profile"]("acct-1")
    first = asyncio.run(runner.get_context(one))
    first.die()

    again = asyncio.run(runner.get_context(one))

    assert again is not first
    assert runtime["pw"].chromium.launched.count(str(one)) == 2


def test_one_window_dying_does_not_take_the_others_out_of_the_registry(runtime):
    one, two = runtime["profile"]("acct-1"), runtime["profile"]("acct-2")
    first = asyncio.run(runner.get_context(one))
    asyncio.run(runner.get_context(two))

    first.die()

    assert runner.open_profiles() == [two]


def test_the_profile_less_call_means_the_window_in_use(runtime):
    """With several open, "the browser" is the active one — not a fresh default
    profile nobody asked to launch."""
    one, two = runtime["profile"]("acct-1"), runtime["profile"]("acct-2")
    asyncio.run(runner.get_context(one))
    second = asyncio.run(runner.get_context(two))

    assert asyncio.run(runner.get_context()) is second
    assert runner.current_profile() == two


def test_the_active_window_falls_back_when_it_is_closed(runtime):
    one, two = runtime["profile"]("acct-1"), runtime["profile"]("acct-2")
    asyncio.run(runner.get_context(one))
    asyncio.run(runner.get_context(two))          # two is active

    asyncio.run(runner.close_browser(profile=two))

    assert runner.current_profile() == one
    assert asyncio.run(runner.get_context()).profile == str(one)


def test_status_reports_every_open_window(runtime):
    one, two = runtime["profile"]("acct-1"), runtime["profile"]("acct-2")
    asyncio.run(runner.get_context(one))
    asyncio.run(runner.get_context(two))

    state = asyncio.run(runner.status())

    assert state["open"] is True
    assert state["count"] == 2
    assert set(state["profiles"]) == {str(one), str(two)}
    assert state["profile"] == str(two)            # the active one, as before
    assert {row["name"] for row in state["browsers"]} == {"acct-1", "acct-2"}


def test_status_reads_closed_once_the_last_window_is_gone(runtime):
    one = runtime["profile"]("acct-1")
    context = asyncio.run(runner.get_context(one))
    context.die()

    state = asyncio.run(runner.status())

    assert state["open"] is False
    assert state["profiles"] == []
    assert state["profile"] is None


def test_shutdown_closes_every_window(runtime):
    one, two = runtime["profile"]("acct-1"), runtime["profile"]("acct-2")
    first = asyncio.run(runner.get_context(one))
    second = asyncio.run(runner.get_context(two))

    asyncio.run(runner.shutdown())

    assert first.closed and second.closed
    assert runner.open_profiles() == []
