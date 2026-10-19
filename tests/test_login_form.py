"""Filling and submitting Meetup's login form.

Every case here is one the live form actually produces, taken from its markup:
the password field is `#current-password`, the three social buttons only exist
on the login page, and the submit button ships `disabled` until React is happy
with the email, the password AND the captcha token.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.automation import meetup
from tests.test_first_chat import FakePage


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def _instant(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(meetup.asyncio, "sleep", _instant)
    monkeypatch.setattr(meetup.human, "think", _instant)
    monkeypatch.setattr(meetup.human, "field_gap", _instant)
    monkeypatch.setattr(meetup.human, "before_submit", _instant)
    monkeypatch.setattr(meetup, "_SUBMIT_ENABLE_S", 0.2)


class FakeButton:
    """A locator over one button whose `disabled` flips after N polls."""

    def __init__(self, enabled_after=0):
        self.polls = 0
        self.enabled_after = enabled_after

    async def is_enabled(self):
        self.polls += 1
        return self.polls > self.enabled_after


# ── is_logged_in ─────────────────────────────────────────────────────────

def test_the_login_page_is_never_logged_in():
    """The regression this whole file exists for.

    `a[href*='/account/']` matched an ordinary link on the logged-OUT login page,
    so is_logged_in() said true, login() returned "Already logged in" and the
    credentials were never typed — a browser that opened and did nothing.
    """
    page = FakePage({
        "login_form": [{"name": "password-field"}, {"name": "google-login"}],
        # Whatever else the page happens to carry must not outvote the form.
        "logged_in_marker": [{"name": "a stray /account/ link"}],
    })
    assert asyncio.run(meetup.is_logged_in(page)) is False


def test_a_signed_in_page_is_logged_in():
    page = FakePage({"logged_in_marker": [{"name": "avatar"}], "login_form": []})
    assert asyncio.run(meetup.is_logged_in(page)) is True


def test_a_page_with_neither_is_logged_out():
    assert asyncio.run(meetup.is_logged_in(FakePage({}))) is False


def test_the_account_link_is_no_longer_a_marker():
    """It is the selector that caused the false positive — a link, present on
    pages that have nothing to do with being signed in."""
    assert not any("/account/" in s for s in meetup.SEL["logged_in_marker"])


def test_the_live_form_ids_are_covered():
    """Straight off the rendered page: #current-password and #email carry both
    an id and a data-testid, and the social buttons mark the login page."""
    assert "#current-password" in meetup.SEL["login_password"]
    assert "#email" in meetup.SEL["login_email"]
    assert "#google-login" in meetup.SEL["login_form"]
    assert "button[data-testid='submit']" in meetup.SEL["login_submit"]


# ── The fields actually taking the text ──────────────────────────────────

def test_filled_fields_pass():
    page = FakePage({
        "login_email": [{"value": "a@b.c"}],
        "login_password": [{"value": "hunter2"}],
    })
    result = asyncio.run(meetup._fields_filled(page, "a@b.c", len("hunter2")))
    assert result["ok"] is True


def test_an_email_box_that_stayed_empty_is_caught():
    page = FakePage({"login_email": [{"value": ""}],
                     "login_password": [{"value": "hunter2"}]})
    result = asyncio.run(meetup._fields_filled(page, "a@b.c", 7))
    assert result["ok"] is False
    assert "email box did not take" in result["detail"]


def test_a_half_typed_password_is_caught():
    page = FakePage({"login_email": [{"value": "a@b.c"}],
                     "login_password": [{"value": "hunt"}]})
    result = asyncio.run(meetup._fields_filled(page, "a@b.c", 7))
    assert result["ok"] is False
    assert "password box did not take" in result["detail"]


def test_the_password_is_never_quoted_in_the_message():
    """This message reaches the activity feed and the log file."""
    page = FakePage({"login_email": [{"value": "a@b.c"}],
                     "login_password": [{"value": "wrong"}]})
    result = asyncio.run(meetup._fields_filled(page, "a@b.c", 9))
    assert "wrong" not in result["detail"]
    assert "5" in result["detail"] and "9" in result["detail"]


# ── Waiting for the submit button ────────────────────────────────────────

def test_waits_for_a_button_that_enables_late():
    button = FakeButton(enabled_after=2)
    assert asyncio.run(meetup._wait_enabled(button)) is True
    assert button.polls == 3


def test_a_button_that_never_enables_gives_up():
    class NeverEnabled:
        async def is_enabled(self):
            return False

    assert asyncio.run(meetup._wait_enabled(NeverEnabled())) is False


def test_an_already_enabled_button_is_not_waited_on():
    button = FakeButton(enabled_after=0)
    assert asyncio.run(meetup._wait_enabled(button)) is True
    assert button.polls == 1


# ── The hCaptcha token ───────────────────────────────────────────────────

def test_the_newest_token_wins_over_a_leftover_one():
    """The other half of the loop.

    hCaptcha writes one textarea per widget (`h-captcha-response-<id>`) and the
    old one survives when Meetup re-renders the form after a rejected submit.
    Reading the FIRST returned a spent token from the previous attempt, which
    reads exactly like success — so the new box was never ticked and the dead
    token went up again, and Meetup asked for the captcha again.
    """
    page = FakePage({"captcha_token": [{"value": "spent-from-last-attempt"},
                                       {"value": "fresh-token"}]})
    assert asyncio.run(meetup.captcha_token(page)) == "fresh-token"


def test_an_empty_newest_textarea_falls_back_to_a_filled_one():
    """A widget that has rendered but not passed yet writes an empty textarea;
    that must not hide a token the live widget already issued."""
    page = FakePage({"captcha_token": [{"value": "issued"}, {"value": ""}]})
    assert asyncio.run(meetup.captcha_token(page)) == "issued"


def test_no_token_at_all_is_empty():
    page = FakePage({"captcha_token": [{"value": ""}]})
    assert asyncio.run(meetup.captcha_token(page)) == ""


def test_no_captcha_widget_is_empty():
    assert asyncio.run(meetup.captcha_token(FakePage({}))) == ""


# ── Having left the login page ───────────────────────────────────────────

def test_still_on_the_form_is_not_success():
    page = FakePage({"login_form": [{"name": "password"}]},
                    url="https://www.meetup.com/login/")
    assert asyncio.run(meetup._left_login_page(page)) is False


def test_the_login_url_without_a_form_is_not_yet_success():
    """Mid-navigation the form can be gone while the URL still says /login."""
    page = FakePage({}, url="https://www.meetup.com/login/?returnUri=/")
    assert asyncio.run(meetup._left_login_page(page)) is False


def test_off_the_login_page_with_no_form_is_success():
    page = FakePage({}, url="https://www.meetup.com/home/")
    assert asyncio.run(meetup._left_login_page(page)) is True


# ── login(), end to end against a fake page ──────────────────────────────

@pytest.fixture
def login_page(monkeypatch):
    """A login page that behaves like the real one: the submit button stays
    disabled until both fields hold text and the captcha has passed."""
    state = {"typed": {}, "captcha": True, "clicked": False, "shots": []}
    email_box = {"value": ""}
    password_box = {"value": ""}

    page = FakePage({
        "login_form": [{"name": "google-login"}],
        "login_email": [email_box],
        "login_password": [password_box],
        "logged_in_marker": [],
    })

    class Submit:
        async def is_enabled(self):
            return bool(email_box["value"] and password_box["value"]
                        and state["captcha"])

    submit = Submit()
    page.matches["login_submit"] = [{"name": "submit"}]

    async def _current_page(profile=None):
        return page

    async def _goto(_page, url, **kwargs):
        page.visited.append(url)
        return True

    async def _dismiss(_page, _selectors):
        return 0

    async def _type(_page, selector, text, clear=False):
        state["typed"][selector] = text
        box = email_box if selector == meetup._sel("login_email") else password_box
        box["value"] = text
        return len(text)

    async def _solve(_page, **kwargs):
        return ({"ok": True, "state": "passed", "detail": "ok"} if state["captcha"]
                else {"ok": False, "state": "timeout", "detail": "Captcha not satisfied"})

    async def _real_click(_page, _locator):
        state["clicked"] = True
        return True

    async def _shot(name):
        state["shots"].append(name)
        return f"{name}.png"

    async def _logged_in_after_submit(_page):
        return state["clicked"]

    monkeypatch.setattr(meetup.browser, "current_page", _current_page)
    monkeypatch.setattr(meetup.browser, "screenshot", _shot)
    monkeypatch.setattr(meetup.visit, "goto", _goto)
    monkeypatch.setattr(meetup.visit, "dismiss_overlays", _dismiss)
    monkeypatch.setattr(meetup.human, "type_or_fill", _type)
    monkeypatch.setattr(meetup, "solve_checkbox", _solve)
    monkeypatch.setattr(meetup.cursor, "real_click", _real_click)
    monkeypatch.setattr(meetup, "is_logged_in", _logged_in_after_submit)
    monkeypatch.setattr(meetup, "_wait_enabled",
                        lambda locator, seconds=0.2: submit.is_enabled())
    return page, state


def test_a_form_already_on_screen_is_not_reloaded(login_page):
    """THE captcha loop.

    login() used to open with page.goto(LOGIN_URL) unconditionally. Solve the
    puzzle by hand, press Log in, and the first thing that happened was a reload
    — which destroyed the token just earned and drew a fresh captcha. Solve,
    press, reload, forever. An hCaptcha token belongs to the page that issued
    it, so reloading is not "starting clean", it is throwing the solve away.
    """
    page, state = login_page
    asyncio.run(meetup.login("profile", "tester@example.com", "hunter2"))
    assert page.visited == []                      # nothing was navigated


def test_a_browser_elsewhere_is_sent_to_the_login_page(login_page):
    page, state = login_page
    page.matches["login_form"] = []                # sitting on the home feed
    asyncio.run(meetup.login("profile", "tester@example.com", "hunter2"))
    assert page.visited == [meetup.config.LOGIN_URL]


def test_login_types_both_credentials_then_submits(login_page):
    page, state = login_page
    result = asyncio.run(meetup.login("profile", "tester@example.com", "hunter2"))

    assert result["ok"] is True
    assert state["typed"][meetup._sel("login_email")] == "tester@example.com"
    assert state["typed"][meetup._sel("login_password")] == "hunter2"
    assert state["clicked"] is True


def test_login_refuses_without_stored_credentials():
    result = asyncio.run(meetup.login("profile", "", ""))
    assert result == {"ok": False, "detail": "No credentials stored"}


def test_a_failed_captcha_stops_before_the_click(login_page):
    page, state = login_page
    state["captcha"] = False

    result = asyncio.run(meetup.login("profile", "tester@example.com", "hunter2"))

    assert result["ok"] is False
    assert state["clicked"] is False
    # The credentials were still typed — the captcha is checked after the form,
    # so a re-run doesn't have to retype into a page that is already filled.
    assert state["typed"][meetup._sel("login_email")] == "tester@example.com"


def test_a_disabled_submit_button_is_reported_not_clicked_into_the_void(
        login_page, monkeypatch):
    """Meetup's button has pointer-events off while disabled, so the old code's
    click landed nowhere and the run waited 30s for a navigation that could
    never come — then blamed a timeout."""
    page, state = login_page

    async def _never(locator, seconds=0.2):
        return False

    monkeypatch.setattr(meetup, "_wait_enabled", _never)

    result = asyncio.run(meetup.login("profile", "tester@example.com", "hunter2"))

    assert result["ok"] is False
    assert "stayed disabled" in result["detail"]
    assert state["clicked"] is False
    assert "login-disabled" in state["shots"]      # the operator gets the page


def test_fields_that_do_not_take_the_text_stop_before_the_captcha(
        login_page, monkeypatch):
    """A captcha token is not free — burning one on a form that isn't filled
    wastes the attempt and the account's goodwill both."""
    page, state = login_page
    solved = []

    async def _type_nothing(_page, selector, text, clear=False):
        return 0                       # React cleared the box under the keystrokes

    async def _solve(_page, **kwargs):
        solved.append(True)
        return {"ok": True, "state": "passed", "detail": "ok"}

    monkeypatch.setattr(meetup.human, "type_or_fill", _type_nothing)
    monkeypatch.setattr(meetup, "solve_checkbox", _solve)

    result = asyncio.run(meetup.login("profile", "tester@example.com", "hunter2"))

    assert result["ok"] is False
    assert "did not take" in result["detail"]
    assert solved == []
    assert state["clicked"] is False
