"""Everything that knows what Meetup's DOM looks like.

This is the ONLY module with a Meetup selector in it. When their markup changes,
this file is what needs updating and nothing above or below it should have to.

    login(profile, login, password)         sign one account in
    scrape_group_members(page, urlname)     a group's roster
    scrape_event_attendees(page, url)       who RSVP'd to an event
    read_profile(page, handle)              one member's bio
    search_groups(page, query, location)    find groups to target
    send_message(page, handle, text)        message one member (cold, by handle)
    message_first_chat(page, text)          reply in the inbox's top conversation
    rsvp(page, event_url)                   RSVP to an event

── On the selectors ──────────────────────────────────────────────────────
Meetup ships a React app with hashed CSS class names, so class selectors would
break on their next deploy. Every selector here is therefore anchored to
something with a reason to be stable — a `data-testid`, an ARIA role, an href
shape — and each has fallbacks tried in order.

They still need checking against the live site before a real run: Meetup A/B
tests its group and profile pages, and no selector list survives that untested.
`probe(page)` exists for exactly this — it reports which selectors match on the
page in front of it, so a broken one is found in seconds instead of halfway
through a campaign.
"""
from __future__ import annotations

import asyncio
import random
import re
import urllib.parse

from backend import activity, config, settings as app_settings
from backend.automation import behavior, cursor
from backend.automation import typing as human
from backend.automation import visit
from backend.automation import playwright_runner as browser
from backend.automation.cursor import Pointer

# ── Selectors ─────────────────────────────────────────────────────────
# Each entry is tried in order; the first that matches wins. Ordered most- to
# least- specific, so a testid is preferred over a role and a role over a shape.

SEL = {
    # Confirmed against the live form: both fields carry an id AND a data-testid.
    "login_email": ["#email", "input[data-testid='email']",
                    "input[name='email']", "input[type='email']"],
    "login_password": ["#current-password", "input[data-testid='current-password']",
                       "input[name='current-password']", "input[type='password']"],
    "login_submit": ["button[data-testid='submit']", "button[type='submit']",
                     "button:has-text('Log in')", "button:has-text('Sign in')"],
    "remember_me": ["#rememberMe", "button[name='rememberMe']"],

    # ── hCaptcha (NOT reCAPTCHA) ──────────────────────────────────────
    # The "I am human" checkbox lives inside a cross-origin iframe, so it can
    # only be reached through a frame_locator — a normal page.locator will never
    # see it. The RESPONSE token, however, is written to a textarea in the main
    # document, which is what makes success detectable from outside the frame.
    "captcha_frame": ["iframe[src*='hcaptcha.com'][src*='frame=checkbox']",
                      "iframe[title*='checkbox for hCaptcha' i]",
                      "iframe[src*='hcaptcha.com']"],
    "captcha_box": ["#checkbox", "div[role='checkbox']", "#anchor", ".check"],
    "captcha_token": ["textarea[name='h-captcha-response']"],
    # The escalated image puzzle. Its own iframe, separate from the checkbox one.
    "captcha_challenge": ["iframe[src*='hcaptcha.com'][src*='frame=challenge']",
                          "iframe[title*='hCaptcha challenge' i]",
                          "iframe[title*='main content of the hCaptcha' i]"],

    # Present only when signed in — the avatar button that opens the account menu.
    # `a[href*='/account/']` is deliberately NOT in here: it matches ordinary
    # links on the logged-OUT login page, which made is_logged_in() answer true
    # on the very page whose whole purpose is that you aren't.
    "logged_in_marker": ["[data-testid='profile-menu']", "#profile-nav",
                         "img[alt*='profile' i]"],
    # The opposite signal, and the decisive one: these exist only on the login
    # page. A positive marker can be matched by accident; a password field and
    # three social-login buttons cannot be mistaken for a signed-in session.
    "login_form": ["#current-password", "input[data-testid='current-password']",
                   "#google-login", "[data-testid='google-login']",
                   "[data-testid='apple-login']", "[data-testid='facebook-login']"],
    # Meetup asks for an emailed code on an unfamiliar device. The captcha is
    # handled separately (see solve_checkbox) — this is the rest.
    "challenge": ["input[name='code']", "input[autocomplete='one-time-code']",
                  "text=/verify (your|it's) you/i", "text=/enter the code/i"],

    "member_card": ["[data-testid='member-card']", "li:has(a[href*='/members/'])"],
    "member_link": ["a[href*='/members/']"],
    "member_name": ["[data-testid='member-name']", "h4", "h3"],

    # One row on an event's attendee list. `data-event-label` is Meetup's own
    # analytics hook on the card's button, which makes it the steadiest anchor on
    # a page whose every class is a hashed design-system token.
    #
    # The name and the id do NOT live in the same element: the button holds the
    # <h3> name, while the numeric id is only in the "View" link BESIDE it (a
    # sibling inside the card, revealed on hover) — which is why _members_on_page
    # walks up from the card rather than scanning links. Anchoring on the link
    # alone read every attendee's name as "View".
    "attendee_card": ["[data-event-label='attendee-card']"],

    "profile_bio": ["[data-testid='member-bio']", "#member-bio",
                    "section:has(h2:text-matches('intro|about', 'i')) p"],
    "profile_name": ["[data-testid='member-name']", "h1"],
    "profile_location": ["[data-testid='member-location']",
                         "[data-testid='location']"],
    "profile_groups": ["a[href*='/members/'][href*='/groups/']",
                       "[data-testid='member-groups'] a"],

    "group_card": ["[data-testid='group-card']", "a[href^='/'][href*='/events/']"],
    "group_name": ["[data-testid='group-name']", "h2", "h3"],

    # The category strip along the top of /find/. Each tab is a div carrying
    # BOTH a `category-item` class and a `data-testid="category-<id>"`, where the
    # id is numeric for a real category and a word/negative for the three
    # specials (all, -999 New Groups, -998 From your Groups).
    "category_item": [".category-item", "[data-testid^='category-']"],

    # ── The distance filter on /find/ ─────────────────────────────────
    # The chip reading "Within 30 kilometers", and the panel it opens. Its
    # testid is the only steady thing on it — every class is a hashed
    # design-system token — with the accessible name as the fallback for a
    # redeploy that renames the testid but not what the control is called.
    # The location box in the header search. Anchored on its id first — it is
    # the one Meetup's own label points at (aria-labelledby) — then on the
    # data-element-name, which survives a redesign of the id, then on what the
    # box says it is for.
    "search_location": ["#location-bar-in-search",
                        "input[data-element-name='header-search']",
                        "input[data-event-label='Location search']",
                        "input[placeholder*='city or zip' i]",
                        "input[aria-label*='location' i]"],
    # The suggestions it drops. The input NAMES its own menu in aria-controls /
    # aria-owns, which is a firmer link than "a listbox somewhere on the page".
    "search_location_menu": ["#location-bar-in-search-menu",
                             "[aria-labelledby='location-bar-in-search-label'] [role='listbox']",
                             "[role='listbox']"],
    # One suggestion. Typed text with no suggestion picked leaves the search
    # where it was, so the FIRST of these is the whole point of typing.
    "search_location_option": ["#location-bar-in-search-menu [role='option']",
                               "#location-bar-in-search-menu li",
                               "[role='listbox'] [role='option']",
                               "[role='listbox'] li"],

    "filter_distance": ["[data-testid='RevampedFilterButtonDistance']",
                        "[data-testid*='FilterButtonDistance']",
                        "button[aria-label*='distance' i]"],
    # The panel the chip opens. It is a Radix dropdown — a div with
    # role="menu", data-radix-menu-content and data-state="open" — and the
    # OPEN state is part of the anchor: Radix leaves closed menus in the tree,
    # so a selector that ignores it can hand back a menu nobody opened.
    #
    # Tried in this order by _first_of, NOT comma-joined: a joined selector
    # takes whatever comes first in the document, which on /find/ is a nav menu
    # rather than the one that was just clicked.
    "filter_panel": ["[data-radix-menu-content][data-state='open']",
                     "[role='menu'][data-state='open']",
                     "[role='dialog'][data-state='open']",
                     "[role='menu']", "[role='listbox']", "[role='dialog']"],
    # One range inside that panel. The live markup is div[role='menuitem'] with
    # Radix's own collection marker on it — NOT a radio, which is what the
    # first version of this looked for and why the panel opened, matched
    # nothing and shut again. The radio/option shapes are kept after it for a
    # build that renders the ranges as a real listbox.
    "filter_option": ["[role='menuitem'][data-radix-collection-item]",
                      "[role='menuitem']", "[role='menuitemradio']",
                      "[role='radio']", "[role='option']",
                      "label:has(input[type='radio'])", "li button"],
    # Some builds render the range as a slider instead of a list. Its maximum is
    # the same choice as the last option — the widest search.
    "filter_slider": ["input[type='range']", "[role='slider']"],
    # The panel that needs its choice confirmed. Absent on the ones that apply
    # as you pick, which is why nothing waits on it.
    "filter_apply": ["button:has-text('Apply')", "button:has-text('Done')",
                     "button:has-text('Save')", "button:has-text('Update')"],
    # The tab's label. Its class changes between selected (ds2-m12) and not
    # (ds2-k12), so the class is exactly what NOT to match on.
    "category_name": ["span"],

    # One event card in a category's results. `data-eventref` carries the event
    # id on the card itself, which is steadier than parsing it back out of the
    # href — though the href is kept as the fallback because it holds the group
    # slug too.
    "event_card": ["[data-testid='categoryResults-eventCard']",
                   "[data-element-name='categoryResults-eventCard']",
                   "[data-eventref]"],
    # The shimmer that stands in for the cards WHILE a category loads. Its
    # placeholders carry no link and no title, so for as long as it is up the
    # grid reads as empty — which is exactly how a category full of events came
    # back as "has no events right now". Nothing is decided while any of these
    # are on screen.
    # Deliberately NOT matching "placeholder": avatars and inputs carry that word
    # permanently, and a selector that is always true would turn every empty
    # category into a full wait instead of an answer.
    "grid_loading": ["[data-testid*='skeleton' i]", "[data-testid*='loading' i]",
                     "[class*='skeleton' i]", "[class*='shimmer' i]",
                     "[class*='animate-pulse' i]",
                     "[aria-busy='true']", "[role='progressbar']"],
    # Meetup's own "there is nothing here" copy. The one answer that lets an
    # empty grid be believed immediately instead of waited out.
    "grid_empty": ["[data-testid='no-results']",
                   "text=/no (upcoming )?(events|results)/i",
                   "text=/nothing (here|found)/i"],
    "card_link": ["a[href*='/events/']"],
    # The h3 carries the FULL title in its `title` attribute; the text itself is
    # line-clamped to two lines and can be visually truncated.
    "card_title": ["h3[title]", "h3"],
    "card_time": ["time[datetime]"],

    # An opened event page. OpenGraph meta is deliberately preferred over the
    # rendered DOM: Meetup's classes are hashed design-system tokens that change
    # on a redeploy, while og: tags are a contract with every link preview on the
    # internet and so barely move.
    "og_title": ["meta[property='og:title']"],
    "og_description": ["meta[property='og:description']"],
    "event_title": ["h1"],
    # The "See all" beneath an event's attendee row. Anchored on the href shape
    # first: /<group>/events/<id>/attendees/ is a Meetup ROUTE, so it outlives
    # any label or testid the link is wearing this month.
    "attendees_link": ["a[href*='/attendees']",
                       "[data-testid='attendees-see-all']",
                       "a:has-text('See all')"],

    "event_attendee": ["[data-testid='attendee']", "li:has(a[href*='/members/'])"],
    "rsvp_button": ["button[data-testid='rsvp-button']", "button:has-text('Attend')",
                    "button:has-text('RSVP')"],

    # The live composer is a bare <textarea placeholder="Send a message..."> with
    # no name and no testid — every class on it is a design-system token, so the
    # placeholder is the one readable thing that identifies it. maxlength=2000 is
    # the message cap and sits behind it as a second anchor for the day the
    # placeholder text changes; the bare textarea is the final fallback.
    #
    # Tried IN ORDER, not comma-joined — every caller waits with ordered=True.
    # It has to be: joined into one selector, that trailing bare `textarea`
    # ranks equally with the candidates above it and Playwright returns whatever
    # comes first in the DOM. On the inbox that is a different conversation's
    # pane, which is how a chat opened and then never got written to.
    "message_box": ["textarea[name='message']", "[data-testid='message-input']",
                    "textarea[placeholder*='Send a message' i]",
                    "textarea[maxlength='2000']", "textarea"],
    # The live composer's Send is an ICON button: type="button" (not submit),
    # aria-label="Send a message...", a lucide-send-horizontal glyph, no text.
    # Every candidate here matches a send control specifically — a bare
    # button[type='submit'] is deliberately NOT among them, because these are
    # comma-joined into one selector and `.first` returns the first match in
    # DOM order, so any unrelated submit button higher up the page (a header
    # search form) would win and the click would land on nothing.
    "message_send": ["button[data-testid='send-message']", "button[data-testid='send-button']",
                     "button[aria-label='Send a message...']",
                     "button[aria-label*='Send a message' i]",
                     "button:has(svg.lucide-send-horizontal)",
                     "button:has(svg.lucide-send)",
                     "button:has-text('Send')"],
    "message_attach": ["input[type='file']"],

    # The Chat / Message control on a member's profile — the door the pipeline
    # goes through, because that is the one a person clicks. Tried IN ORDER
    # rather than comma-joined (see _first_of): the href shape is the anchor that
    # matters, since /messages/new/?member=<id> is a Meetup ROUTE and outlives
    # the label, which is "Chat" in some places and "Message" in others. The
    # text matches are last because `:has-text` matches any ancestor containing
    # the word, and a comma-joined "first in DOM order" would hand back a header
    # button long before the profile's own.
    "profile_chat": ["a[href*='/messages/new']",
                     "[data-testid='message-member']",
                     "[data-element-name='message-member']",
                     "button[aria-label*='message' i]",
                     "a[aria-label*='message' i]",
                     "button:has-text('Chat')", "a:has-text('Chat')",
                     "button:has-text('Message')", "a:has-text('Message')"],

    # The Messages icon in the signed-in header. Carries an id, a data-element-name
    # AND an aria-label, so three independent anchors survive a class rename; the
    # href shape is the last resort.
    "header_messages": ["#messages-links", "[data-element-name='header-messages']",
                        "a[aria-label='Messages']", "a[href$='/messages/']"],

    # ── The inbox at /messages/ ───────────────────────────────────────
    # One row per existing conversation. Anchored on a testid first, then on the
    # href shape: a row is the only thing on that page linking to another
    # /messages/<id> thread, which survives a class or testid rename.
    "chat_row": ["[data-testid='conversation-item']", "[data-testid='conversation']",
                 "li:has(a[href*='/messages/'])",
                 "[role='listitem']:has(a[href*='/messages/'])",
                 "a[href*='/messages/']:not([href$='/messages/'])"],
    # The other party's name on a row. On the live inbox it is a
    # <span class="ds2-m14 ...">, distinct from the preview (.ds2-r14) and the
    # timestamp (.ds2-r12), so the medium-weight class targets the name alone.
    "chat_name": ["[data-testid='conversation-name']", "[data-testid='member-name']",
                  "span.ds2-m14", "h3", "h4"],
    # Present only once a thread is actually open, which is what makes it the
    # signal that the click landed rather than a fixed sleep.
    "chat_thread": ["[data-testid='message-thread']", "[data-testid='messages-list']",
                    "[role='log']", "main:has(textarea)"],

    # Cookie banner + the "get the app" interstitial, both of which swallow clicks.
    "overlays": ["button:has-text('Accept')", "#onetrust-accept-btn-handler",
                 "[data-testid='close-button']", "button[aria-label='Close']"],
}


def _sel(key: str) -> str:
    """One CSS/Playwright selector matching any of `key`'s candidates.

    Comma-joining them makes Playwright's own "first match wins" do the fallback,
    which is both faster and less code than trying each in a loop.
    """
    return ", ".join(SEL[key])


def member_handle(url: str) -> str:
    """The member id/slug out of a /members/<handle>/ URL.

    Meetup uses a numeric member id in most links and a vanity slug in some. Both
    are stable identifiers for the same person, so whichever the link carries is
    what gets stored — the store dedupes case-insensitively on it.
    """
    match = re.search(r"/members/([^/?#]+)", url or "")
    return (match.group(1) if match else "").strip()


def member_url(handle: str) -> str:
    return f"{config.BASE_URL}/members/{str(handle).strip().lstrip('@')}/"


# Meetup's new-conversation composer. Also settings.chat_link's default, and what
# an emptied or unusable template falls back to — a run must always have an
# address to go to, and a blank Settings box is not a reason to send nothing.
DEFAULT_CHAT_LINK = (f"{config.BASE_URL}/messages/"
                     "?new_convo=true&member_id={id}&name={name}")


def chat_link(handle: str, name: str = "", template: str | None = None) -> str:
    """The address to open for ONE member's chat, off the saved template.

    Settings → chat_link holds the line; this fills it in per recipient. The
    placeholders are the two things a member is known by here:

        {id} / {username}   their Meetup id or vanity slug — the handle the
                            store is keyed on, and what identifies them in a URL
        {name}              their display name, form-encoded: "Nick w" goes in
                            as "Nick+w", which is what Meetup's own composer
                            links carry and not what %-encoding would produce

    A template carrying NO placeholder is treated as a base to append the id to,
    because "https://www.meetup.com/messages/new/?member=" pasted out of the
    address bar is the shape an operator is most likely to save, and silently
    opening the same link for all two hundred recipients is the worst possible
    reading of it.

    `template` is for tests and callers that already hold the value; leave it off
    and the stored setting is read.
    """
    handle = str(handle or "").strip().lstrip("@")
    line = template if template is not None else \
        str(app_settings.current().get("chat_link") or "")
    line = line.strip() or DEFAULT_CHAT_LINK
    if not handle:
        return line

    # The id with plain %-encoding, since a template is free to put it in the
    # PATH — where a "+" is a literal plus and not a space. Handles have no
    # spaces in them, so the two encodings only ever differ for the name.
    quoted = urllib.parse.quote(handle, safe="")
    filled = (line.replace("{id}", quoted)
                  .replace("{username}", quoted)
                  .replace("{handle}", quoted)
                  .replace("{name}", urllib.parse.quote_plus(str(name or ""))))
    # Nothing was substituted, so the id is not in the link yet — see above.
    if filled == line:
        filled = line + ("" if line.endswith(("=", "/", "?", "&")) else "/") + quoted
    return filled


def group_url(urlname: str) -> str:
    return f"{config.BASE_URL}/{str(urlname).strip().strip('/')}/"


def event_ref(url: str) -> tuple[str, str]:
    """(group urlname, event id) out of a /<urlname>/events/<id>/ URL.

    The group slug is the valuable half: it is exactly what the scraper takes as
    a target, so a browse of the Find page turns straight into scrapeable groups.
    """
    match = re.search(r"meetup\.com/([^/?#]+)/events/(\d+)", url or "")
    if not match:
        return "", ""
    return match.group(1), match.group(2)


# ── Session state ─────────────────────────────────────────────────────


async def is_logged_in(page) -> bool:
    """Whether the page in front of us is a signed-in session.

    A DOM check, unlike backend/sessions.py's cookie read — this one is for use
    mid-run, when what matters is whether the CURRENT page can act, not what the
    cookie file said at boot.

    The login form is checked FIRST and wins. A false positive here is not a
    cosmetic wrong answer: login() returns "Already logged in" on it and never
    types the credentials, which looks from the outside like a browser that
    opened and then did nothing at all.
    """
    try:
        if await page.locator(_sel("login_form")).first.count() > 0:
            return False
        return await page.locator(_sel("logged_in_marker")).first.count() > 0
    except Exception:  # noqa: BLE001
        return False


async def hit_challenge(page) -> bool:
    """True when Meetup is asking for a code or a captcha.

    Worth its own check because a challenge page has none of the selectors a run
    is looking for, so without this a challenge looks exactly like "the markup
    changed" — and the operator gets sent selector-hunting for a problem that
    just needs a code typed in.
    """
    try:
        return await page.locator(_sel("challenge")).first.count() > 0
    except Exception:  # noqa: BLE001
        return False


async def guard_challenge(page, where: str) -> None:
    """Stop the run if a challenge is on screen and the setting says to.

    Continuing through a challenge means submitting actions that all silently
    fail, which is both useless and exactly the behaviour that escalates a
    challenge into a block.
    """
    if not await hit_challenge(page):
        return
    shot = await browser.screenshot("challenge")
    detail = f"Meetup challenge at {where}" + (f" — see {shot}" if shot else "")
    activity.log("browser", detail, "error")
    if app_settings.current()["stop_on_challenge"]:
        raise RuntimeError(detail)


# ── hCaptcha ──────────────────────────────────────────────────────────


async def captcha_token(page) -> str:
    """The CURRENT hCaptcha response token, or "" if no widget has passed.

    This is the honest success signal. The checkbox turning green is cosmetic;
    what the form actually submits is this token, so polling for it is the only
    way to know the widget is satisfied rather than merely clicked.

    Reads the NEWEST textarea, not the first. hCaptcha writes one textarea per
    widget instance (`h-captcha-response-<id>`) and leaves the old one in the
    DOM when Meetup re-renders the form after a rejected submit. Taking the
    first meant reading a spent token from the previous attempt — which looks
    exactly like success, so the box was never re-ticked and the dead token was
    submitted again.
    """
    try:
        boxes = page.locator(_sel("captcha_token"))
        count = await boxes.count()
        for index in range(count - 1, -1, -1):
            value = (await boxes.nth(index).input_value()) or ""
            if value:
                return value
        return ""
    except Exception:  # noqa: BLE001
        return ""


async def captcha_present(page) -> bool:
    try:
        return await page.locator(_sel("captcha_frame")).first.count() > 0
    except Exception:  # noqa: BLE001
        return False


async def captcha_challenged(page) -> bool:
    """True when hCaptcha escalated to the image puzzle."""
    try:
        frame = page.locator(_sel("captcha_challenge")).first
        return await frame.count() > 0 and await frame.is_visible()
    except Exception:  # noqa: BLE001
        return False


async def solve_checkbox(page, *, wait_s: float | None = None) -> dict:
    """Tick the "I am human" box and wait for the widget to actually pass.

    Returns {"ok", "state", "detail"} where `state` is one of:

        absent     no captcha on this page — nothing to do
        passed     a token was issued; the form can be submitted
        challenge  hCaptcha escalated to an image puzzle
        timeout    clicked, but no token arrived

    **On the image puzzle: this does not solve it, and will not.** Defeating a
    challenge designed to tell humans from machines is a different problem from
    pacing a browser like a person, and any code claiming to do it would be
    guesswork that fails silently and gets the account flagged for trying. What
    happens instead is honest: with a visible browser the run WAITS, so the
    operator can click the tiles themselves and the automation carries on with
    the token they earned. Headless — where nobody can click — it reports the
    challenge and stops.

    The checkbox itself is reached through a frame_locator: it lives in a
    cross-origin iframe that a normal page.locator can never see.
    """
    if not await captcha_present(page):
        return {"ok": True, "state": "absent", "detail": "No captcha on this page"}

    # It may already be satisfied — a trusted session often passes on load.
    if await captcha_token(page):
        return {"ok": True, "state": "passed", "detail": "Captcha already satisfied"}

    values = app_settings.current()
    deadline = wait_s if wait_s is not None else values["captcha_wait_s"]

    try:
        frame = page.frame_locator(_sel("captcha_frame")).first
        box = frame.locator(_sel("captcha_box")).first
        await behavior.beat(behavior.PRE_CLICK_S)
        await box.click(timeout=8_000)
        activity.log("accounts", "Ticked the 'I am human' box", "info")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "state": "error",
                "detail": f"Could not reach the captcha checkbox ({type(exc).__name__})"}

    # Poll for the token. hCaptcha issues it a beat after the click when it is
    # satisfied, and never when it isn't — so this doubles as the pass/fail test.
    waited = 0.0
    announced = False
    while waited < deadline:
        token = await captcha_token(page)
        if token:
            activity.log("accounts", "Captcha passed", "ok")
            return {"ok": True, "state": "passed",
                    "detail": f"Captcha passed after {waited:.0f}s"}

        if await captcha_challenged(page):
            if config.PLAYWRIGHT_HEADLESS:
                shot = await browser.screenshot("captcha-challenge")
                return {"ok": False, "state": "challenge",
                        "detail": "hCaptcha asked for the image puzzle and this "
                                  "run is headless — nobody can solve it"
                                  + (f" (see {shot})" if shot else "")}
            if not announced:
                announced = True
                activity.log("accounts",
                             f"hCaptcha image puzzle — solve it in the browser "
                             f"window, waiting up to {deadline:.0f}s", "warn")

        await asyncio.sleep(1.0)
        waited += 1.0

    return {"ok": False, "state": "timeout",
            "detail": f"Captcha not satisfied within {deadline:.0f}s"}


# ── Login ─────────────────────────────────────────────────────────────

# How long Meetup's submit button may stay disabled after the form is filled and
# the captcha has passed. It flips as soon as React revalidates, so this is a
# ceiling for a slow render, not an expected wait.
_SUBMIT_ENABLE_S = 10.0


async def _wait_enabled(locator, seconds: float = _SUBMIT_ENABLE_S) -> bool:
    """Poll until `locator` is enabled. False if it never is."""
    left = seconds
    while left > 0:
        try:
            if await locator.is_enabled():
                return True
        except Exception:  # noqa: BLE001
            return False
        step = random.uniform(0.25, 0.5)
        await asyncio.sleep(step)
        left -= step
    return False


async def _on_login_page(page) -> bool:
    """True when the login form is on screen right now."""
    try:
        return await page.locator(_sel("login_form")).first.count() > 0
    except Exception:  # noqa: BLE001
        return False


async def _left_login_page(page) -> bool:
    """True once the login form is gone AND the URL is no longer /login.

    The second success signal after a submit. Only meaningful once a challenge
    has been ruled out — a challenge page satisfies both halves of this too.
    """
    try:
        if await page.locator(_sel("login_form")).first.count() > 0:
            return False
        return "/login" not in (page.url or "")
    except Exception:  # noqa: BLE001
        return False


async def _fields_filled(page, email: str, password_len: int) -> dict:
    """Confirm the credentials are really in the boxes. Returns {ok, detail}.

    The password is compared by LENGTH, never by value — this runs on every
    login, and a mismatch message that quoted the password would put it in the
    activity feed and the log file.
    """
    try:
        got_email = (await page.locator(_sel("login_email")).first.input_value()) or ""
        got_password = (await page.locator(_sel("login_password")).first.input_value()) or ""
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"Could not read the login form ({type(exc).__name__})"}

    if got_email.strip() != (email or "").strip():
        return {"ok": False,
                "detail": f"The email box did not take the address "
                          f"(holds {len(got_email)} characters, expected {len(email)})"}
    if len(got_password) != password_len:
        return {"ok": False,
                "detail": f"The password box did not take the password "
                          f"(holds {len(got_password)} characters, expected {password_len})"}
    return {"ok": True, "detail": "Both fields filled"}


async def login(profile, login: str, password: str) -> dict:
    """Sign in on `profile`. Returns {ok, detail}.

    Never raises on a bad password — a failed login is a result the accounts
    panel needs to show, not an exception the run should die on.
    """
    if not login or not password:
        return {"ok": False, "detail": "No credentials stored"}

    # Do NOT re-navigate when the browser is already sitting on the login form.
    #
    # This is the captcha loop: you solve the puzzle by hand in the window, press
    # Log in, and the first thing that used to happen was page.goto(LOGIN_URL) —
    # which reloads the form, destroys the token you just earned and draws a
    # fresh captcha. Solve, press, reload, solve, press, reload, forever. An
    # hCaptcha token belongs to the page instance that issued it; reloading is
    # not a cheap way to "start clean", it is throwing the solve away.
    page = await browser.current_page(profile)
    if not await _on_login_page(page):
        await visit.goto(page, config.LOGIN_URL)
    await visit.dismiss_overlays(page, SEL["overlays"])
    await human.think()

    # Already signed in — the login URL redirects to the home feed.
    if await is_logged_in(page):
        return {"ok": True, "detail": "Already logged in"}

    try:
        # 1 · Email
        await human.type_or_fill(page, _sel("login_email"), login, clear=True)
        # Between fields, then again before submitting. Filling both boxes and
        # submitting inside the same tick is the clearest form-automation tell
        # there is — a person's hand takes a moment to move.
        await human.field_gap()

        # 2 · Password
        await human.type_or_fill(page, _sel("login_password"), password, clear=True)
        await human.field_gap()

        # 3 · Did the text actually land? React re-renders these inputs while
        # they are typed into, and a field that got cleared out from under the
        # keystrokes leaves the form looking filled to nobody but us. Checked
        # before the captcha is spent, because a captcha token is not free.
        landed = await _fields_filled(page, login, len(password))
        if not landed["ok"]:
            shot = await browser.screenshot("login-fields")
            return {"ok": False,
                    "detail": landed["detail"] + (f" — see {shot}" if shot else "")}

        # 4 · "I am human". Must come BEFORE submit: the form posts the captcha
        # token alongside the credentials, so submitting first just fails with a
        # generic error and burns an attempt.
        captcha = await solve_checkbox(page)
        if not captcha["ok"]:
            return {"ok": False, "detail": captcha["detail"],
                    "captcha": captcha["state"]}

        await human.before_submit()

        # 5 · Log in. Meetup ships this button `disabled` with pointer-events
        # switched off, and only enables it once the email parses, the password
        # is non-empty AND the captcha token exists. Clicking it while it is
        # still disabled is silently nothing — no error, no navigation — so the
        # run has to wait for it rather than assume it is clickable.
        submit = page.locator(_sel("login_submit")).first
        if not await _wait_enabled(submit):
            shot = await browser.screenshot("login-disabled")
            # Name the actual blocker. An hCaptcha token dies after a couple of
            # minutes, so a long hand-solve followed by slow typing can arrive at
            # a form Meetup has already invalidated — and "stayed disabled" sends
            # the operator hunting the password instead of the captcha.
            why = ("the captcha token has expired — tick the I-am-human box again"
                   if await captcha_present(page) and not await captcha_token(page)
                   else "Meetup had not accepted the email, the password or the "
                        "I-am-human box")
            return {"ok": False,
                    "detail": f"Log in stayed disabled — {why}"
                              + (f" — see {shot}" if shot else "")}

        # This button specifically gets a REAL OS-level click where one is
        # available: it is the most scrutinised control on the site, and a
        # renderer-dispatched click is exactly the sort of synthetic event a bot
        # check can tell apart from a person. Falls back automatically.
        used_os = await cursor.real_click(page, submit)
        activity.log("accounts",
                     f"Submitted login with {'OS-level' if used_os else 'in-page'} click",
                     "info")
    except Exception as exc:  # noqa: BLE001
        shot = await browser.screenshot("login-form")
        return {"ok": False,
                "detail": f"Login form not usable ({type(exc).__name__})"
                          + (f" — see {shot}" if shot else "")}

    # Wait for an outcome rather than a fixed sleep: a fixed wait is either too
    # short on a slow login or wasted time on a fast one.
    #
    # The challenge check comes FIRST. A challenge page has also left /login and
    # also has no login form, so testing for success first would report a
    # captcha as a successful login.
    for _ in range(30):
        await asyncio.sleep(1.0)
        if await hit_challenge(page):
            shot = await browser.screenshot("challenge")
            return {"ok": False, "detail": "Meetup asked for a code or captcha"
                                           + (f" — see {shot}" if shot else "")}
        # Two independent signals, because one selector list should not be the
        # only thing standing between a real login and a reported timeout: an
        # avatar/profile marker, or simply having left the login page with the
        # form gone.
        if await is_logged_in(page) or await _left_login_page(page):
            return {"ok": True, "detail": "Logged in"}

    # Still on the form 30s after submitting means Meetup rejected it and drew
    # the page again. Say so, and say the captcha is the thing being re-asked
    # when it is — "Login did not complete" is the one message that sends an
    # operator round the loop a fourth time instead of looking at the password.
    shot = await browser.screenshot("login-timeout")
    if await _on_login_page(page):
        again = (" and is asking for the captcha again"
                 if await captcha_present(page) and not await captcha_token(page) else "")
        return {"ok": False,
                "detail": f"Meetup rejected the login{again} — check the password, "
                          f"or use Manual login and sign in by hand once"
                          + (f" — see {shot}" if shot else "")}
    return {"ok": False, "detail": "Login did not complete"
                                   + (f" — see {shot}" if shot else "")}


# ── Scraping ──────────────────────────────────────────────────────────


async def _members_on_page(page) -> list[dict]:
    """Every member currently in the DOM, as {username, name}.

    TWO shapes, tried in that order. An event's attendee list draws a card per
    person carrying the NAME, with the numeric id only in a "View" link beside
    it; group rosters and search results are plain links whose text is the name.
    The old reader knew only the second shape, so on an attendee list it took the
    href from the View link and the text from the same anchor — and collected a
    hundred people all called "View".

    Names come off the <h3> including any blurred span: Meetup blurs a surname
    with CSS for non-members, so "Muhammad <span class=blur>Tufail</span>" is the
    whole name in the DOM and only half of it on screen.

    A card with no View link (co-hosts have none) still comes back — with the
    name and an empty username. It is a real attendee, it belongs in the count,
    and the panel is what decides that nobody without an id can be messaged.

    ONE evaluate rather than a locator query per card: a roster holds hundreds,
    and a round-trip each turns a fast collect into a minute of IPC.
    """
    return await page.evaluate(
        """(sel) => {
            const clean = t => (t || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
            const idFrom = href => {
                const m = (href || '').match(/\\/members\\/([^/?#]+)/);
                return m ? m[1] : '';
            };

            // Shape 1 — an attendee card. Walk up from the card for the View
            // link: it is a SIBLING of the button holding the name, not a child.
            const cards = [...document.querySelectorAll(sel.card)];
            if (cards.length) {
                return cards.map(card => {
                    const heading = card.querySelector('h3, h4');
                    let name = clean(heading && heading.textContent);
                    if (!name) {
                        const photo = card.querySelector('img[alt]');
                        const alt = photo ? photo.getAttribute('alt') : '';
                        name = clean((alt || '').replace(/^Photo of the user\\s*/i, ''));
                    }
                    let id = '', node = card, up = 0;
                    while (node && up++ < 6) {
                        const link = node.querySelector
                            && node.querySelector('a[href*="/members/"]');
                        if (link) { id = idFrom(link.getAttribute('href')); break; }
                        const parent = node.parentElement;
                        // Stop BELOW any ancestor holding a second attendee
                        // card. Climbing into one finds the next person's View
                        // link, and a card with no link of its own — a co-host
                        // has none — would come back wearing their neighbour's
                        // id. That is not a missing member, it is a message
                        // sent to the wrong one.
                        if (!parent || parent.querySelectorAll(sel.card).length > 1) break;
                        node = parent;
                    }
                    return (name || id) ? {username: id, name} : null;
                }).filter(Boolean);
            }

            // Shape 2 — a plain list of member links.
            return [...document.querySelectorAll(sel.link)].map(a => {
                const id = idFrom(a.getAttribute('href'));
                if (!id) return null;
                const card = a.closest('li, [data-testid="member-card"]') || a;
                const named = card.querySelector('h3, h4, [data-testid="member-name"]');
                return {username: id, name: clean((named || a).textContent)};
            }).filter(Boolean);
        }""",
        {"card": _sel("attendee_card"), "link": _sel("member_link")},
    )


async def scrape_group_members(page, urlname: str, *, ctl=None,
                               target: int = 0) -> list[dict]:
    """Every member listed on a group's Members tab.

    Returns [{username, name, groups}] — bios are NOT read here. Opening each
    profile is a page load per member, so it is a separate, optional pass
    (`read_profile`), driven by the `visit_profiles` setting.
    """
    url = f"{group_url(urlname)}members/"
    if not await visit.goto(page, url):
        return []
    await visit.dismiss_overlays(page, SEL["overlays"])
    await guard_challenge(page, f"group {urlname}")

    found = await visit.scroll_to_end(page, collect=_members_on_page,
                                      ctl=ctl, target=target)
    for row in found:
        row["groups"] = urlname
    activity.log("scrape", f"{urlname}: {len(found)} member(s)", "ok")
    return found


async def scrape_event_attendees(page, event_url: str, *, ctl=None,
                                 target: int = 0) -> list[dict]:
    """Who RSVP'd to one event.

    A narrower and much warmer pool than a whole group roster: an attendee has
    shown up for a specific topic on a specific date, which is a far stronger
    signal about them than membership of a group they joined years ago.
    """
    url = event_url.rstrip("/") + "/attendees/"
    if not await visit.goto(page, url):
        return []
    await visit.dismiss_overlays(page, SEL["overlays"])
    await guard_challenge(page, "event attendees")

    found = await visit.scroll_to_end(page, collect=_members_on_page,
                                      ctl=ctl, target=target)
    # Same second pass as collect_attendees: a name with no id is not a member
    # anything downstream can use, and the id is behind a click.
    if any(not (row or {}).get("username") for row in found or []):
        try:
            await ids_by_clicking(page, found, ctl=ctl)
        except Exception as exc:  # noqa: BLE001
            activity.log("scrape", f"Could not click through the attendees "
                                   f"({type(exc).__name__})", "warn")
    group = visit.path_segment(event_url, 0)
    for row in found:
        row["groups"] = group
    activity.log("scrape", f"{url}: {len(found)} attendee(s)", "ok")
    return found


async def read_profile(page, handle: str) -> dict:
    """One member's profile: bio, display name, location.

    Returns a row the member store can take directly. A profile that is private,
    deleted or simply blank yields a row with no bio rather than an error — the
    store's "unknown" sentinel is the right answer for all three, and a missing
    bio is the normal case, not a failure.
    """
    if not await visit.goto(page, member_url(handle)):
        return {"username": handle}
    await visit.dismiss_overlays(page, SEL["overlays"])
    await guard_challenge(page, f"profile {handle}")
    await human.think()

    bio = await visit.text_of(page, _sel("profile_bio"))
    groups = await page.eval_on_selector_all(
        _sel("profile_groups"),
        "els => els.map(a => (a.getAttribute('href')||'').split('/').filter(Boolean)[0])",
    )
    return {
        "username": handle,
        "name": await visit.text_of(page, _sel("profile_name")),
        "bio": bio,
        "location": await visit.text_of(page, _sel("profile_location")),
        "groups": ",".join(sorted({g for g in (groups or []) if g})),
    }


async def _groups_on_page(pg) -> list[dict]:
    """Group links currently rendered, as {urlname, url}."""
    links = await visit.links_on_page(pg)
    out = []
    for href in links:
        slug = visit.path_segment(href, 0)
        # A group link is /<urlname>/ — filter out Meetup's own sections, which
        # have the same shape and would otherwise be scraped as groups.
        if slug and slug not in _NOT_A_GROUP and "/events/" not in href:
            out.append({"urlname": slug, "url": f"{config.BASE_URL}/{slug}/"})
    return out


# ── The Find page's category strip ────────────────────────────────────

FIND_URL = f"{config.BASE_URL}/find/"

# Category browsing is paced faster than the rest of this module ON PURPOSE.
# Everything else here is an action with a consequence — a message, an RSVP, a
# login — where looking like a person is worth the seconds. Clicking tabs is
# read-only browsing that produces no artefact on anyone else's screen, and a
# real person flicking through a category strip does it quickly, so the slow
# prose-typing pacing was both wrong and unusably long over twenty tabs.
_CATEGORY_WAIT_S = 3.0            # ceiling for one category's list to settle
_CATEGORY_SETTLE_S = (0.12, 0.25)  # poll step while it settles
_CATEGORY_GAP_S = (0.10, 0.28)     # between one tab and the next
# Ceiling for the grid to actually SWAP to the new category. Longer than the
# settle budget above and deliberately so: settling is "has it stopped moving",
# swapping is "is this the new category at all", and the second needs a network
# round trip. It is only ever waited out in full when the swap never happens.
_CATEGORY_SWAP_S = 6.0
# Ceiling for the loading shimmer to give way to real cards. Generous, because
# it is only ever spent on a grid that IS loading — the wait ends the moment the
# first card lands, and an idle grid never enters it at all.
_GRID_LOAD_S = 15.0
# How long a grid has to sit still — no shimmer, no cards, no empty-state copy —
# before "there is nothing here" is believed. Meetup renders the skeleton a beat
# AFTER the click, so the instant right after a tab is pressed looks identical
# to an empty result and must not be read as one.
_GRID_EMPTY_GRACE_S = 2.5
# The same grace straight after a category click, where a fetch is known to be
# in flight. Longer, because "nothing yet" and "nothing at all" look identical
# and only one of them is worth reporting.
_GRID_CLICK_GRACE_S = 6.0
_GRID_STEP = 0.25                  # poll step while waiting on the shimmer


async def open_find(page) -> dict:
    """Land on /find/, with the category strip actually rendered. {ok, detail}.

    The wait is the point. /find/ is a React page and its category strip arrives
    well after domcontentloaded — listing the tabs the instant navigation
    returned found nothing at all, so the sweep reported "no categories" and
    clicked none of them on a page that visibly had twenty.
    """
    if "/find" not in (page.url or ""):
        if not await visit.goto(page, FIND_URL):
            return {"ok": False, "detail": "Find page would not load"}
    await visit.dismiss_overlays(page, SEL["overlays"])
    await guard_challenge(page, "find")

    if await _wait_for(page, "category_item") is None:
        return {"ok": False, "detail": "Find page opened but no category strip "
                                       "appeared"}
    return {"ok": True, "detail": "Find page open"}


# How long the filter panel gets to render after the chip is clicked. Short: it
# is a popover already in the page's tree, not a fetch.
_FILTER_PANEL_S = 3.0


# How long the suggestion menu has to appear after a city is typed. It is a
# network round trip to Meetup's geocoder, and typing happens key-by-key, so
# this is waited from the LAST keystroke rather than the first.
_LOCATION_MENU_S = 8.0
# And how long the box has to actually read the picked city afterwards. Short:
# the value is set by the same click that closes the menu.
_LOCATION_SETTLE_S = 4.0


async def set_location(page, city: str) -> dict:
    """Type a city into the header search and take its FIRST suggestion.

    Returns {ok, city, label, detail} — `label` being what the box reads
    afterwards, which is the only honest confirmation that a suggestion was
    taken rather than text merely typed.

    Taking the suggestion is the entire point. Meetup's location box is a
    combobox: typing into it changes nothing until one of the options under it
    is chosen, so a run that typed "Tokyo" and carried on would search wherever
    the profile was last pointed and look like it had moved.

    Best-effort, like the distance filter: a run that cannot find the box is
    still a run — it searches from wherever the session already is, and says so.
    """
    city = str(city or "").strip()
    if not city:
        return {"ok": False, "city": "", "label": "", "detail": "No city given"}

    box = await _wait_for(page, "search_location", seconds=_LOCATION_MENU_S,
                          ordered=True)
    if box is None:
        return {"ok": False, "city": city, "label": "",
                "detail": "No location box on this page"}

    was = ""
    try:
        was = (await box.input_value()) or ""
    except Exception:  # noqa: BLE001
        pass

    # Clicked, then cleared, then typed — the order a person does it in, and the
    # order the combobox listens for: it opens its menu on focus and re-queries
    # on each key.
    await human.type_or_fill(page, box, city, clear=True)

    option = await _wait_for(page, "search_location_option",
                             seconds=_LOCATION_MENU_S, ordered=True)
    if option is None:
        return {"ok": False, "city": city, "label": was,
                "detail": f"No suggestions for {city} — the search is still on "
                          f"{was or 'wherever it was'}"}

    try:
        await Pointer(page).click(option, swift=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "city": city, "label": was,
                "detail": f"Could not take the suggestion: {type(exc).__name__}"}

    # Read the box back. A menu item takes a click and closes whether or not
    # anything was applied, so the value is what tells "took the suggestion"
    # from "clicked something".
    picked = await _location_value(box, city, seconds=_LOCATION_SETTLE_S)
    if not picked or picked == was:
        return {"ok": False, "city": city, "label": picked or was,
                "detail": f"The location still reads "
                          f"{picked or was or 'nothing'} after picking {city}"}
    # The box holding EXACTLY what was typed means the click landed on nothing:
    # a suggestion fills the name out — "Tokyo" becomes "Tokyo, JP" — and a
    # combobox left on raw text searches from wherever it was before.
    #
    # This reads a city typed in its full "Tokyo, JP" form as a failure, which
    # is the safe way round: the cities list holds plain names, and calling a
    # real move a failure costs a warn line while the reverse costs a run in the
    # wrong city that says it moved.
    if picked.strip().lower() == city.lower():
        return {"ok": False, "city": city, "label": picked,
                "detail": f"{city} was typed but no suggestion was taken — the "
                          f"search has not moved"}
    return {"ok": True, "city": city, "label": picked,
            "detail": f"Searching from {picked}"}


async def _location_value(box, city: str, seconds: float) -> str:
    """What the location box reads, once it has settled on something.

    Polled: the value is written by the same click that closes the menu, and
    reading it in the same tick gets the half-typed text back.
    """
    left, last = seconds, ""
    while left > 0:
        try:
            last = ((await box.input_value()) or "").strip()
        except Exception:  # noqa: BLE001
            return last
        # A suggestion fills the box out — "Delhi" becomes "Delhi, IN" — so a
        # value LONGER than what was typed is the signal one was taken.
        if last and last.lower() != city.lower():
            return last
        step = random.uniform(0.1, 0.25)
        await asyncio.sleep(step)
        left -= step
    return last


async def widen_distance(page) -> dict:
    """Open the distance chip on /find/ and take its WIDEST range.

    Returns {ok, label, detail} — `label` being what the chip reads afterwards,
    which is the one honest confirmation that the choice took.

    Widest is the whole point: /find/ searches inside 30km by default, and that
    is why a category comes back with four events on it.

    Best-effort by design. A roll whose filter would not open is still a roll:
    it goes ahead on whatever range the page is already set to, and says so.
    """
    chip = await _wait_for(page, "filter_distance", seconds=_FILTER_PANEL_S)
    if chip is None:
        return {"ok": False, "label": "", "detail": "No distance filter on this page"}

    was = (await _text_of(chip)).strip()
    if not await _open_filter_panel(page, chip):
        return {"ok": False, "label": was,
                "detail": f"The distance filter would not open (still {was})"}

    picked = await _take_widest(page)
    if not picked["taken"]:
        # Nothing to pick means nothing was changed — leave the panel shut
        # rather than hanging open over the grid the next step has to read.
        await _close_filter_panel(page)
        return {"ok": False, "label": was,
                "detail": f"The distance filter opened but offered nothing to "
                          f"pick (still {was})"}

    await _apply_filter(page)
    # `keep`, because something WAS picked — see _close_filter_panel. Closing
    # this with Escape is what used to put the range straight back to where it
    # started, a beat after the panel visibly jumped to the widest.
    await _close_filter_panel(page, chip, keep=True)
    # Read the chip again rather than trusting the click. A menu item takes a
    # click and closes whether or not anything was applied, so the chip is the
    # only thing that can tell "took the widest" from "clicked something".
    now = (await _text_of(chip)).strip() or was
    if picked["label"] and not _same_range(now, picked["label"]):
        return {"ok": False, "label": now,
                "detail": f"Picked {picked['label']} but the filter still "
                          f"reads {now}"}
    # A SLIDER has no label to compare, so it is checked on its own terms — and
    # checked AFTER the panel is shut, which is the only moment that answers the
    # question that matters. The range visibly took the maximum and then went
    # back to the first value a beat later, so a check made while the panel was
    # still open would have passed every time.
    if picked.get("slider") and now == was:
        return await _settled_slider(page, chip, picked, was)
    return {"ok": True, "label": now,
            "detail": f"Distance widened to {now}" if now != was
                      else f"Distance already at {now}"}


async def _settled_slider(page, chip, picked: dict, was: str) -> dict:
    """Re-open the filter and read the slider back, once the panel has closed.

    Costs one extra open per RUN — widen_distance is called once at the start of
    a sweep, not once per event — and it is the only way to catch a value that
    the page accepts and then quietly rolls back. One retry, because a second
    failure means this build genuinely will not keep it and the run should say
    so and get on with the events.
    """
    value, before = picked.get("value"), picked.get("was")
    if value is None or before is None:
        # The control publishes nothing to read. Neither claimed nor denied.
        return {"ok": True, "label": was,
                "detail": f"Distance slider taken to its maximum ({was}) — the "
                          f"control reports no value, so this is not confirmed"}
    if value <= before:
        # It never moved at all, so there is nothing to re-open and confirm.
        # Checking "does it still hold" here would compare the value against
        # itself and call a filter that never budged a success.
        return {"ok": False, "label": was,
                "detail": f"The distance slider did not move (still {was})"}

    for attempt in (0, 1):
        if not await _open_filter_panel(page, chip):
            return {"ok": True, "label": was,
                    "detail": f"Distance set to its maximum ({was}) — the panel "
                              f"would not re-open to confirm it"}
        panel = await _first_of(page, "filter_panel")
        slider = await _first_of(panel if panel is not None else page,
                                 "filter_slider")
        held = await _slider_now(slider) if slider is not None else None

        if held is not None and held >= value:
            await _close_filter_panel(page, chip, keep=True)
            return {"ok": True, "label": was,
                    "detail": f"Distance widened and it held ({held:g})"}
        if attempt == 0 and slider is not None:
            activity.log("scraping", f"The distance went back to {held:g} after "
                                     f"the panel closed — setting it again",
                         "warn")
            await _drag_slider_to_max(page, slider)
            await _apply_filter(page)
            await _close_filter_panel(page, chip, keep=True)
            continue
        await _close_filter_panel(page, chip, keep=True)
        return {"ok": False, "label": was,
                "detail": f"The distance keeps reverting to {held:g} — this run "
                          f"is searching the default radius"}
    return {"ok": False, "label": was, "detail": "Distance could not be widened"}


def _same_range(chip_text: str, option_text: str) -> bool:
    """Does the chip now say what was picked?

    Compared loosely on purpose: the chip wears the range as a sentence
    ("Within 150 kilometers") while the menu item is the range alone ("150
    kilometers"), so one contains the other rather than equalling it.
    """
    chip, option = chip_text.strip().lower(), option_text.strip().lower()
    if not chip or not option:
        return False
    return option in chip or chip in option


async def _text_of(locator) -> str:
    try:
        return (await locator.inner_text()) or ""
    except Exception:  # noqa: BLE001
        return ""


async def _open_filter_panel(page, chip) -> bool:
    """Click the chip and wait for its panel. True once something opened.

    Two ways of clicking, like the category tabs: the human pointer aims at
    coordinates and anything floating over the chip swallows the press, so a
    panel that did not appear is retried with Playwright's own click, which
    waits for the element to actually receive the event.
    """
    for attempt in (0, 1):
        try:
            if attempt == 0:
                await Pointer(page).click(chip, swift=True)
            else:
                await chip.click(timeout=5_000)
        except Exception:  # noqa: BLE001
            continue
        if await _wait_for_panel(page) is not None:
            return True
        # A panel that renders as none of the known shapes still counts if the
        # chip now says it is expanded — the options are found from the page.
        try:
            if (await chip.get_attribute("aria-expanded")) == "true":
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


# How many notches to walk a slider up by hand when End does nothing. Meetup's
# distance slider has a handful of stops; this is comfortably past the last one
# and an ArrowRight at the maximum is a no-op.
_SLIDER_STEPS = 40


def _moved(before: float | None, after: float | None) -> bool:
    """Did the range actually go UP? Unknown values are not a yes."""
    return before is not None and after is not None and after > before


async def _drag_slider_to_max(page, slider) -> bool:
    """Drag the range to its right-hand end. True if the pointer got to do it.

    A DRAG, not a keypress, and that distinction is the whole of this function.
    Pressing End moves a native input's DOM value, but Meetup's filter is a
    React-controlled control: the component re-renders from its own state a beat
    later and puts the thumb straight back where it was. That is precisely the
    "it takes the last value and then returns to the first" the operator watched
    — the jump was real, and so was the snap back.

    Real pointer events are what the component's own handlers listen for, so a
    drag changes the STATE rather than just the DOM, and there is nothing to
    revert to.
    """
    box = await slider.bounding_box()
    if not box or box["width"] < 8:
        return False
    y = box["y"] + box["height"] / 2
    # Grab at the left end and pull right, rather than aiming for the thumb: on
    # a native range a press anywhere on the track takes the value there
    # already, and on a custom one the thumb is a child element this has no
    # reliable handle on.
    start = box["x"] + 2
    end = box["x"] + box["width"] - 2

    pointer = Pointer(page)
    await pointer.move_to(start, y)
    await page.mouse.down()
    # In steps. One jump from end to end is not a drag anybody's hand makes, and
    # a component that samples pointermove needs the moves to sample.
    steps = random.randint(6, 11)
    for index in range(1, steps + 1):
        await page.mouse.move(start + (end - start) * index / steps,
                              y + random.uniform(-1.5, 1.5))
        await asyncio.sleep(random.uniform(0.01, 0.04))
    await page.mouse.up()
    pointer.x, pointer.y = end, y
    # The commit is often a frame behind the release.
    await asyncio.sleep(random.uniform(0.15, 0.35))
    return True


async def _slider_now(slider) -> float | None:
    """A slider's current value, or None when it will not say.

    `aria-valuenow` first — it is what a range control is REQUIRED to publish
    and it survives the markup underneath changing.
    """
    for attr in ("aria-valuenow", "value"):
        try:
            raw = await slider.get_attribute(attr)
        except Exception:  # noqa: BLE001
            continue
        if raw not in (None, ""):
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
    try:
        return float(await slider.input_value())
    except Exception:  # noqa: BLE001
        return None


async def _take_widest(page) -> dict:
    """Choose the widest range in the open panel.

    Returns {taken, label} — `label` empty when what was taken has no name to
    check it by, which is the slider's case and not a failure.

    The panel comes in two shapes and this handles both: a list of ranges, and
    a slider whose maximum is the same choice said differently.
    """
    nothing = {"taken": False, "label": ""}
    panel = await _first_of(page, "filter_panel")
    root = panel if panel is not None else page

    slider = await _first_of(root, "filter_slider")
    if slider is not None:
        try:
            # End is the keyboard's "maximum" and it is what a slider is built
            # to answer — dragging one by coordinates lands on whichever notch
            # the pixel maths rounds to.
            before = await _slider_now(slider)
            # DRAGGED first — see _drag_slider_to_max. The keyboard is the
            # fallback, not the other way round: a keypress on a
            # React-controlled slider moves the DOM and gets re-rendered away.
            dragged = await _drag_slider_to_max(page, slider)
            after = await _slider_now(slider)

            # CHECKED, not taken on trust. A slider that did not move is the
            # difference between a run over the whole country and one over
            # 30km, and the old code could not tell those apart — it returned
            # `label: ""`, which switched the verification in widen_distance
            # off entirely.
            if not _moved(before, after):
                # Nothing to lose by asking the keyboard too: on the builds that
                # render a plain <input type=range> it works, and on the ones
                # that do not it is a no-op rather than a revert.
                await slider.focus()
                await slider.press("End")
                if not _moved(before, await _slider_now(slider)):
                    for _ in range(_SLIDER_STEPS):
                        await slider.press("ArrowRight")
                after = await _slider_now(slider)
            return {"taken": True, "label": "", "slider": True,
                    "value": after, "was": before, "dragged": dragged}
        except Exception:  # noqa: BLE001
            pass

    options = await _options_in(root)
    if options is None:
        return nothing
    try:
        total = await options.count()
    except Exception:  # noqa: BLE001
        return nothing

    rows = []
    for index in range(total):
        option = options.nth(index)
        try:
            if not await option.is_visible():
                continue
            rows.append((index, (await option.inner_text() or "").strip()))
        except Exception:  # noqa: BLE001
            continue

    wanted = _widest_of([text for _, text in rows])
    if wanted is None:
        return nothing
    index, label = rows[wanted]
    if not await _click_option(page, options.nth(index)):
        return nothing
    return {"taken": True, "label": label}


async def _options_in(root):
    """The panel's ranges, by the first candidate SHAPE that matches any.

    One candidate at a time rather than comma-joined, like _first_of and for
    the same reason: joined, Playwright returns every shape's matches in
    document order, so a fallback meant for a different build can interleave
    with the real list and "which one is which" stops meaning anything.
    """
    for candidate in SEL["filter_option"]:
        try:
            found = root.locator(candidate)
            if await found.count():
                return found
        except Exception:  # noqa: BLE001 — a bad selector must not stop the rest
            continue
    return None


# The number in a range's label — "150 kilometers", "Within 30 km", "10 miles".
_RANGE_NUMBER = re.compile(r"(\d[\d,]*)")


def _widest_of(labels: list[str]) -> int | None:
    """Which label is the widest range, as an index into `labels`.

    By the NUMBER in it, not by position. The menu hoists whatever is currently
    selected to the top and lists the rest ascending — 30km selected reads
    "Within 30 kilometers, 5, 10, 25, 50, 100, 150" — so "the last one" is the
    widest only while the current setting is a narrow one. Set to 150, that
    list ends at 100, and taking the last would NARROW the search on every
    roll while reporting that it had widened it.

    A label saying "any" is the widest there is, whatever numbers sit beside
    it. With no numbers anywhere, the last is the only guess left.
    """
    best, found = None, None
    for index, label in enumerate(labels):
        if not label:
            continue
        if re.search(r"\bany\b", label, re.I):
            return index
        match = _RANGE_NUMBER.search(label.replace(",", ""))
        if match is None:
            continue
        value = int(match.group(1))
        if best is None or value > best:
            best, found = value, index
    if found is not None:
        return found
    return len(labels) - 1 if labels else None


async def _click_option(page, option) -> bool:
    """Take one range. The human pointer, then Playwright's own click.

    Same two-step as everything else clicked on this site: the pointer aims at
    coordinates, and a menu item that has scrolled inside its own panel is not
    where those coordinates say it is.
    """
    try:
        # The panel is `max-h-[50vh] overflow-y-auto` and the widest range is
        # at the bottom of it — on a short window that is below the fold of the
        # menu itself, not of the page.
        await option.scroll_into_view_if_needed(timeout=3_000)
    except Exception:  # noqa: BLE001
        pass
    try:
        await Pointer(page).click(option, swift=True)
        return True
    except Exception:  # noqa: BLE001
        pass
    try:
        await option.click(timeout=5_000)
        return True
    except Exception:  # noqa: BLE001
        return False


# How long an Apply button has to appear after the range is changed. Several
# builds render it only ONCE something has been picked, so looking for it the
# instant the slider moved found nothing and the change was never committed.
_FILTER_APPLY_S = 2.5


async def _apply_filter(page) -> bool:
    """Press Apply, on the panels that have one. True if it was pressed.

    WAITED for rather than looked for once: the button is commonly rendered by
    the same state change that the pick just caused, so it is not in the DOM at
    the moment the pick lands.
    """
    left = _FILTER_APPLY_S
    while left > 0:
        button = await _first_visible(page.locator(_sel("filter_apply")))
        if button is not None:
            try:
                await button.click(timeout=5_000)
                return True
            except Exception:  # noqa: BLE001
                return False
        step = random.uniform(0.12, 0.3)
        await asyncio.sleep(step)
        left -= step
    return False


async def _wait_for_panel(page, seconds: float = _FILTER_PANEL_S):
    """Poll until the filter panel is up. Returns it, or None.

    _wait_for's comma-joined form is not usable here: Radix leaves closed menus
    in the tree, so `[role='menu']` matches one that nobody opened, and the
    open-state candidates have to be tried in the order they are written.
    """
    left = seconds
    while left > 0:
        found = await _first_of(page, "filter_panel")
        if found is not None:
            return found
        step = random.uniform(0.12, 0.3)
        await asyncio.sleep(step)
        left -= step
    return None


async def _close_filter_panel(page, chip=None, *, keep: bool = False) -> None:
    """Shut the panel, so it is not sitting over the grid the roll then reads.

    `keep` says a CHOICE HAS BEEN MADE inside it, and it changes how the panel
    is closed — which turned out to be the whole bug behind "the filter jumps to
    the widest range and then snaps back to the narrowest".

    Escape on a Radix popover is CANCEL. The old code moved the slider to its
    maximum, found no Apply button (they render only once something is picked,
    and it was looked for a single time at the instant of the pick), and then
    pressed Escape — which discarded the change and put the range back where it
    started. The slider path returned no label, so widen_distance's own
    verification was switched off and nothing noticed.

    So when something was picked, the panel is closed the way a person closes
    one they mean to keep: click the chip again to toggle it shut, or click off
    it. Escape is only for a panel nothing was done in.
    """
    try:
        if await _first_of(page, "filter_panel") is None:
            return                       # applying it already closed the panel
        if keep:
            # Clicking AWAY first, chip second. Both dismiss without cancelling,
            # but empty space cannot do anything else — a chip is a control, and
            # on some builds pressing an active one clears its filter, which is
            # the very outcome being avoided here.
            try:
                await page.mouse.click(5, 5)
                if await _first_of(page, "filter_panel") is None:
                    return
            except Exception:  # noqa: BLE001
                pass
            if chip is not None:
                try:
                    await chip.click(timeout=3_000)
                    if await _first_of(page, "filter_panel") is None:
                        return
                except Exception:  # noqa: BLE001
                    pass
            # Still open. Escape is the last resort and it may undo the pick, so
            # say so rather than leaving the panel over the grid silently.
            activity.log("scraping", "The distance panel would not close without "
                                     "Escape — the range may not have stuck",
                         "warn")
        await page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        pass


async def list_categories(page) -> list[dict]:
    """The category tabs, left to right, as [{key, name}].

    `key` is the data-testid's suffix — "all", "-999", "652" — which is what
    identifies a tab across renders; the name is only for reading.
    """
    tabs = page.locator(_sel("category_item"))
    try:
        total = await tabs.count()
    except Exception:  # noqa: BLE001
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for index in range(total):
        tab = tabs.nth(index)
        try:
            testid = (await tab.get_attribute("data-testid")) or ""
        except Exception:  # noqa: BLE001
            continue
        key = testid.replace("category-", "", 1) if testid else ""
        # The two selectors are comma-joined, so the same tab matches twice.
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "name": await _tab_label(tab) or key})
    return out


# How many of a tab's spans to look at before giving up on finding its label.
_LABEL_SCAN = 4


async def _tab_label(tab) -> str:
    """A category tab's visible name, or "" if it has none.

    The FIRST span is not the label. A live tab wraps an icon in one span and
    the name in another, and which comes first varies down the strip — so
    reading `span.first` returned an empty string for most tabs, every one of
    them fell back to its numeric key, and the panel grouped events under "652"
    and "-999" instead of "Technology" and "New Groups". Scan the spans for the
    first one that actually has text, then fall back to the tab's own.
    """
    try:
        labels = tab.locator(_sel("category_name"))
        for index in range(min(await labels.count(), _LABEL_SCAN)):
            text = _first_line(await labels.nth(index).inner_text())
            if text:
                return text
    except Exception:  # noqa: BLE001
        pass
    try:
        return _first_line(await tab.inner_text())
    except Exception:  # noqa: BLE001
        return ""


def _first_line(text: str | None) -> str:
    lines = [line.strip() for line in (text or "").splitlines()]
    return next((line for line in lines if line), "")


async def _category_selected(tab) -> bool:
    """Whether this tab is the one currently showing.

    The selected tab's label carries `ds2-m12`; every other one carries
    `ds2-k12`. That is the page's own answer to "did the click land", and it is
    available immediately — unlike the card count, which needs a network round
    trip and reads the same for two categories that happen to have equal
    numbers of events.

    Every span is checked, not just the first, for the same reason _tab_label
    scans them: the label is not reliably the first one, and reading the icon's
    span instead reported "not selected" for a tab that plainly was — which sent
    every click down the retry path.
    """
    try:
        labels = tab.locator(_sel("category_name"))
        total = min(await labels.count(), _LABEL_SCAN)
        for index in range(total):
            if "ds2-m12" in ((await labels.nth(index).get_attribute("class")) or ""):
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


async def click_category(page, key: str) -> dict:
    """Click one category tab by its key. Returns {ok, detail, cards}.

    The strip scrolls horizontally and most tabs start outside it, so each one
    is scrolled into view first. A `z-20` "Scroll right" button also floats over
    the strip's right edge — a pointer click aimed at a tab underneath it hits
    the button instead and merely scrolls — so the click is VERIFIED against the
    tab's own selected state and retried with a direct click if it didn't take.
    """
    tab = page.locator(f"[data-testid='category-{key}']").first
    try:
        if await tab.count() == 0:
            return {"ok": False, "detail": f"No category tab {key}", "cards": 0}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"Category {key} unreachable "
                                       f"({type(exc).__name__})", "cards": 0}

    before = await _card_count(page)
    was = await _grid_fingerprint(page)
    try:
        await tab.scroll_into_view_if_needed(timeout=5_000)
    except Exception:  # noqa: BLE001 — already in view, or no scroll parent
        pass

    await behavior.beat(_CATEGORY_GAP_S)
    try:
        await Pointer(page).click(tab, swift=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"Could not click {key} "
                                       f"({type(exc).__name__})", "cards": before}

    # The human pointer aims at coordinates, so anything floating above the tab
    # swallows the press. Playwright's own click waits for the element to
    # actually receive pointer events, which is exactly the case being rescued.
    if not await _category_selected(tab):
        await asyncio.sleep(random.uniform(0.12, 0.3))    # React may still be swapping
        if not await _category_selected(tab):
            try:
                await tab.click(timeout=5_000)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False,
                        "detail": f"Category {key} would not take a click "
                                  f"({type(exc).__name__})", "cards": before}

    # Poll for the list to settle rather than sleeping a fixed amount: a
    # category with few events renders in a blink, a busy one does not.
    cards, stale = await _settled_grid(page, before, was)
    await guard_challenge(page, f"category {key}")
    detail = (f"Category {key} opened but its results never changed"
              if stale else f"Opened category {key}")
    return {"ok": True, "detail": detail, "cards": cards, "stale": stale}


async def _card_count(page) -> int:
    try:
        return await page.locator(_sel("event_card")).count()
    except Exception:  # noqa: BLE001
        return 0


async def _grid_loading(page) -> bool:
    """Is the grid still showing its loading shimmer?"""
    try:
        return await page.locator(_sel("grid_loading")).count() > 0
    except Exception:  # noqa: BLE001
        return False


async def _grid_says_empty(page) -> bool:
    """Has Meetup itself said there is nothing here?"""
    try:
        return await page.locator(_sel("grid_empty")).count() > 0
    except Exception:  # noqa: BLE001
        return False


async def wait_for_cards(page, *, budget: float = _GRID_LOAD_S,
                         grace: float = _GRID_EMPTY_GRACE_S) -> int:
    """Wait past the shimmer for real event cards. Returns the card count.

    THE fix for "<category> has no events right now" on a category that plainly
    has them. A click swaps the grid to skeleton placeholders while the results
    are fetched; those carry no link and no title, so every card selector misses
    and the grid reads as empty. Reading it at that moment — which is what the
    old waits did, since they treated a count of zero as a settled answer —
    reported an empty category roughly as often as a slow network.

    Zero is only believed when the page has actually said so: either its own
    "no results" copy is up, or the grid has sat still with no shimmer and no
    cards for `grace` seconds. Otherwise this keeps waiting, and returns the
    moment the first real card lands.

    `grace` is longer straight after a click (see _settled_grid): there we KNOW
    a fetch is in flight, so a still, empty grid is far more likely to be one
    that has not started painting than one with nothing to paint. It also has to
    cover the case where Meetup's skeleton markup carries none of the shapes in
    `grid_loading` — the wait cannot see the shimmer then, and the quiet run is
    all that stands between a slow fetch and a false "no events".
    """
    left, quiet = budget, 0.0
    while left > 0:
        count = await _card_count(page)
        if count:
            return count
        if await _grid_says_empty(page):
            return 0
        # The shimmer resets the quiet run: a grid that is painting placeholders
        # has not finished answering, however long it has been at it.
        quiet = 0.0 if await _grid_loading(page) else quiet + _GRID_STEP
        if quiet >= grace:
            return 0
        await asyncio.sleep(_GRID_STEP)
        left -= _GRID_STEP
    return await _card_count(page)


# Pulled out of the card's own text rather than off a class name, because the
# classes here are hashed design-system tokens and these three shapes are not.
_BY_RE = re.compile(r"^by (.+)$", re.MULTILINE)
_ATTENDEES_RE = re.compile(r"([\d,]+)\s+attendee", re.IGNORECASE)
_RATING_RE = re.compile(r"\b([0-5]\.\d)\b")


async def _read_card(card) -> dict | None:
    """One event card as a row, or None if it carries no usable link."""
    href = ""
    try:
        link = card.locator(_sel("card_link")).first
        if await link.count():
            href = (await link.get_attribute("href")) or ""
    except Exception:  # noqa: BLE001
        return None

    urlname, from_href = event_ref(href)
    try:
        event_id = (await card.get_attribute("data-eventref")) or from_href
    except Exception:  # noqa: BLE001
        event_id = from_href
    if not event_id and not urlname:
        return None

    title = ""
    try:
        heading = card.locator(_sel("card_title")).first
        if await heading.count():
            # The `title` attribute holds the untruncated text; the visible node
            # is line-clamped to two lines.
            title = ((await heading.get_attribute("title"))
                     or (await heading.inner_text()) or "").strip()
    except Exception:  # noqa: BLE001
        pass

    when = ""
    try:
        stamp = card.locator(_sel("card_time")).first
        if await stamp.count():
            when = ((await stamp.get_attribute("datetime")) or "").strip()
    except Exception:  # noqa: BLE001
        pass

    text = ""
    try:
        text = (await card.inner_text()) or ""
    except Exception:  # noqa: BLE001
        pass

    host = _BY_RE.search(text)
    attendees = _ATTENDEES_RE.search(text)
    rating = _RATING_RE.search(text)
    return {
        "event_id": str(event_id),
        "urlname": urlname,
        "title": title,
        "group": host.group(1).strip() if host else "",
        "when": when,
        "attendees": int(attendees.group(1).replace(",", "")) if attendees else 0,
        "rating": rating.group(1) if rating else "",
        "online": "Online" in text,
        "going": "Going" in text,
        "url": (href or "").split("?")[0],     # drop the recId/searchId tracking
    }


async def _meta(page, key: str) -> str:
    try:
        tag = page.locator(_sel(key)).first
        if await tag.count() == 0:
            return ""
        return ((await tag.get_attribute("content")) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


async def read_event(page) -> dict:
    """What an opened event page says about itself, as {title, description}.

    Read off OpenGraph meta first — see the selector note. The h1 is the
    fallback for a page that somehow ships without it.
    """
    title = await _meta(page, "og_title")
    if not title:
        try:
            heading = page.locator(_sel("event_title")).first
            if await heading.count():
                title = ((await heading.inner_text()) or "").strip()
        except Exception:  # noqa: BLE001
            pass
    return {"title": title, "description": await _meta(page, "og_description")}


# How long "See all" has to appear on a freshly-opened event before the route is
# taken instead. It is not on the page at domcontentloaded — the event page is a
# React app and the attendee section arrives well after the navigation returns,
# which is why looking for it the instant the page loaded found nothing on every
# cold event and quietly typed the URL instead.
#
# Only ever spent in full on an event that genuinely hides its attendees, and the
# fallback right after it is one fast navigation.
_SEE_ALL_WAIT_S = 8.0
# How much of that budget is spent before scrolling once to look further down
# the page — see open_attendees. Most of the wait is AFTER the nudge, because a
# link that was going to render on its own has usually done so within this.
_SEE_ALL_FIRST_S = 3.0


async def open_attendees(page, event_url: str) -> bool:
    """Get to an event's attendee list. True if we landed on it.

    CLICKS the "See all" link, because that is the control a person uses and it
    carries the referrer a typed URL doesn't. The click happens as soon as the
    link EXISTS — polled, not looked for once — see _SEE_ALL_WAIT_S.

    Falls back to the /attendees/ route, which is the same place by a different
    door. An event with its attendee list hidden has neither, and returns False
    rather than leaving the caller on the event page thinking it worked.
    """
    if "/attendees" in (page.url or ""):
        return True

    # Tried in the order the candidates are written: the href shape is a Meetup
    # ROUTE and outlives any label, so it must win over the `:has-text('See
    # all')` fallback, which a comma-joined selector would let match first
    # wherever it happened to sit in the document.
    link = await _wait_for(page, "attendees_link", seconds=_SEE_ALL_FIRST_S,
                           ordered=True)
    if link is None:
        # The attendee section is well down the event page, and a section that
        # is mounted on scroll never arrives to something that only waits. One
        # nudge, then the rest of the budget — cheap, and it costs nothing on
        # the pages where the link was already there.
        try:
            await visit.scroll_step(page, swift=True)
        except Exception:  # noqa: BLE001 — a page that will not scroll is not fatal
            pass
        link = await _wait_for(page, "attendees_link",
                               seconds=_SEE_ALL_WAIT_S - _SEE_ALL_FIRST_S,
                               ordered=True)
    if link is not None:
        try:
            await Pointer(page).click(link, swift=True)
            if await _wait_for_url(page, "/attendees", seconds=8.0):
                return True
        except Exception:  # noqa: BLE001 — fall through to the route
            pass
        activity.log("scraping", "See all was there but the click did not land "
                                 "— taking the attendees route", "warn")
    if not event_url:
        return False
    if not await visit.goto(page, event_url.rstrip("/") + "/attendees/"):
        return False
    return "/attendees" in (page.url or "")


async def collect_attendees(page, target: int = 40, *, ctl=None,
                            click_ids: bool = False, click_limit: int = 0,
                            click_all: bool = False) -> list[dict]:
    """The attendees rendered on an attendee list, as [{username, name}].

    Bounded by `target`. An event with 800 RSVPs would otherwise scroll for
    minutes inside what is supposed to be a browse — the full roster is what
    `scrape_event_attendees` is for, once you know which events are worth it.

    `click_ids` is the second pass: anyone the list handed back without an id
    gets their card CLICKED, their id read off the profile that opens, and that
    profile closed again. OFF by default, and that is the point — collecting is
    what puts the roster on screen, and a page load per attendee inside it meant
    the operator watched a blank table for a minute before seeing anyone. The
    clicking is its own step, run against the list they are already looking at
    (see campaign.open_all_profiles). `click_all` covers every attendee rather
    than only the ones missing an id.
    """
    try:
        await visit.dismiss_overlays(page, SEL["overlays"])
        if target:
            # A capped read: stop at `target`, so the incremental collect is
            # the right shape — it can stop mid-list.
            rows = await visit.scroll_to_end(page, collect=_members_on_page,
                                             target=target, swift=True)
        else:
            # The WHOLE roster. Load it to the end first and read it once:
            # the list fetches twenty at a time, and collecting as it goes
            # stopped as soon as one round of scrolling did not outrun the
            # network — which is how forty attendees came back as twenty.
            await load_whole_list(page, ctl=ctl)
            rows = await _members_on_page(page)
    except Exception:  # noqa: BLE001
        try:
            rows = await _members_on_page(page)
        except Exception:  # noqa: BLE001
            return []

    # BEFORE the dedupe below, which drops rows and would leave the click pass
    # walking a list that no longer lines up with the cards on screen.
    if click_ids and (click_all or any(not (r or {}).get("username")
                                       for r in rows or [])):
        try:
            await ids_by_clicking(page, rows, ctl=ctl, limit=click_limit,
                                  force=click_all)
        except Exception as exc:  # noqa: BLE001 — a failed pass must not lose
            activity.log("scraping", f"Could not click through the attendees "
                                     f"({type(exc).__name__})", "warn")
    # Deduped on the id where there is one, on the name where there is not — a
    # co-host's card carries no View link, and dropping those would under-report
    # the roster the page plainly shows.
    seen, out = set(), []
    for row in rows or []:
        row = row or {}
        key = row.get("username") or f"name:{(row.get('name') or '').lower()}"
        if key and key != "name:" and key not in seen:
            seen.add(key)
            out.append(row)
    return out[:target] if target else out


def member_ref(url: str) -> str:
    """The member id out of a profile URL — /members/<id>/ → "<id>"."""
    match = re.search(r"/members/([^/?#]+)", str(url or ""))
    return match.group(1) if match else ""


# One attendee opened, read and closed. The pause is between PEOPLE, not inside
# the open: the tab is shut the instant the id is off it, and the beat is what
# keeps twenty of them from being twenty page loads in twenty frames.
_CLICK_MEMBER_GAP_S = (0.3, 0.8)
# How long to give a card to open a TAB before assuming it routes in place.
# Short on purpose: it is spent once per card that does NOT open one, and used
# to be eight seconds of waiting for nothing — two minutes on a roster of
# fifteen.
_MEMBER_TAB_WAIT_MS = 1_500
# How long ONE card gets to do something after it is clicked. Whatever it does
# — open a tab, route the page, draw a panel — is watched for the whole of it,
# rather than each shape being waited on for a slice in turn. A tab that shows
# up at the four-second mark still counts; under the old fixed wait it was
# nobody's, so its id was lost and the tab was left open.
_MEMBER_OPEN_S = 6.0
# The address of a just-opened profile tab. Not the page — the ADDRESS, which is
# where the id is, and which exists as soon as the navigation commits.
_MEMBER_URL_S = 5.0
_URL_POLL_S = 0.05
# How long the route to a profile (or back to the list) gets. This one is a real
# navigation, so it is the page's clock, not ours.
_MEMBER_ROUTE_S = 10.0
# Tabs clicking attendees at the same time. The same ceiling the chat runner
# keeps and for the same reason — a burst of parallel tabs on one session is the
# shape Meetup reads as scripted, and past a handful the browser is the limit
# anyway.
_ID_TAB_MAX = 8
# How many profiles are opened at once by default — the same five the batch
# buttons use, so what the operator sees in the window matches what they picked.
_ID_TAB_WIDTH = 5
# How long a profile tab STAYS UP once its id has been read.
#
# The id is in the address and is known the instant the navigation commits, so
# a tab could be shut immediately — and that is what this used to do. It made
# the window of five a window over WORKERS rather than over open tabs: each tab
# lived for milliseconds, so five workers taking turns at the list still only
# ever had one profile on screen. Holding the tab while it loads is what makes
# five profiles genuinely open at once, the sixth clicked as the first closes.
#
# It is also the more honest thing to be doing: a person who opens a profile
# looks at it. The dwell is capped rather than waiting the page out, because
# what this needs off the profile is already in the address bar.
_MEMBER_DWELL_S = 2.5
# How long ONE scroll to the bottom gets to produce the next batch of cards.
# This is a network fetch, so it is the site's clock: too short and half a
# roster is silently missing.
_LIST_GROW_S = 3.0
_LIST_POLL_S = 0.15
# Rounds that fetch nothing before the list is called finished. Three, each
# after a real wait — the end of a list looks exactly like a slow one.
_LIST_IDLE_ROUNDS = 3


async def click_attendee(page, index: int) -> dict:
    """Click one attendee card, take their id off the profile, close it again.

    Returns {ok, username, name, detail}.

    THE reason this exists: on the current attendee list a card is a <button>
    with the name in an <h3> and NO link anywhere in it — the id is not in the
    DOM at all. Every attendee therefore arrives as a name with an empty
    username: unmessageable, unstorable, invisible to the batches. Clicking the
    card is how a person reaches that id, so it is how this reaches it too.

    Both shapes are handled. A card that opens a TAB is read and the tab closed
    at once — twenty attendees is twenty opens and twenty closes, never twenty
    tabs sitting open. A card that routes this same page (what the live list
    does) is waited for, read, and walked back with the browser's own Back.
    """
    card = page.locator(_sel("attendee_card")).nth(index)
    try:
        if await card.count() == 0:
            return {"ok": False, "username": "", "name": "",
                    "detail": f"No attendee card {index}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "username": "", "name": "",
                "detail": f"Card {index} unreachable ({type(exc).__name__})"}

    context = getattr(page, "context", None)
    popup = None
    if context is not None:
        try:
            async with context.expect_page(timeout=_MEMBER_TAB_WAIT_MS) as caught:
                await Pointer(page).click(card, swift=True)
            popup = await caught.value
        except Exception:  # noqa: BLE001 — no tab opened; fall through
            popup = None
    else:
        # No context to catch a tab with. The click still has to HAPPEN — it
        # used to live inside the branch above, so a page without one was never
        # clicked at all and every attendee came back with no id.
        try:
            await Pointer(page).click(card, swift=True)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "username": "", "name": "",
                    "detail": f"Card {index} would not take a click "
                              f"({type(exc).__name__})"}

    if popup is not None:
        handle, name = "", ""
        try:
            await popup.wait_for_load_state("domcontentloaded", timeout=15_000)
            handle = member_ref(popup.url)
            name = await _profile_name(popup)
        except Exception as exc:  # noqa: BLE001
            return_detail = f"{type(exc).__name__}"
        else:
            return_detail = "read" if handle else "profile carried no id"
        finally:
            # Immediately, as the tab has nothing left to give. In `finally` so
            # a profile that failed to load is closed too — the alternative is a
            # tab leaking per failure until the window is unusable.
            try:
                await popup.close()
            except Exception:  # noqa: BLE001
                pass
        return {"ok": bool(handle), "username": handle, "name": name,
                "detail": return_detail}

    # No tab appeared, which is the NORMAL case on the current attendee list:
    # the card is a <button> that routes this same page to the profile. The
    # route is asynchronous, so the URL a beat after the click is still the
    # attendee list — reading it immediately (which is what this used to do) got
    # an empty id every single time and reported "click opened nothing" for a
    # click that had worked perfectly.
    if not await _wait_for_url(page, "/members/", seconds=_MEMBER_ROUTE_S):
        return {"ok": False, "username": "", "name": "",
                "detail": "the card did not lead to a profile"}

    handle = member_ref(page.url)
    name = await _profile_name(page)
    # Straight back, or the next index would be looked for on a profile page
    # rather than on the attendee list.
    try:
        await page.go_back(wait_until="domcontentloaded", timeout=15_000)
    except Exception:  # noqa: BLE001
        pass
    # And WAIT for the list to be back: go_back returns when the document is
    # ready, not when the React route has re-rendered the cards under it.
    await _wait_for_url(page, "/attendees", seconds=_MEMBER_ROUTE_S)
    return {"ok": bool(handle), "username": handle, "name": name,
            "detail": "read" if handle else "profile carried no id"}


async def _profile_name(page) -> str:
    """The display name on an opened profile, or "" — best-effort."""
    try:
        heading = page.locator(_sel("profile_name")).first
        if await heading.count():
            return ((await heading.inner_text()) or "").strip()[:120]
    except Exception:  # noqa: BLE001
        pass
    return ""


async def _attendee_count(page) -> int:
    try:
        return await page.locator(_sel("attendee_card")).count()
    except Exception:  # noqa: BLE001
        return 0


async def _grew_within(page, before: int, seconds: float) -> int:
    """The card count once it exceeds `before`, or whatever it is at the end.

    A poll rather than a sleep: a list that loads its next twenty in 200ms moves
    on in 200ms, and one that takes two seconds is still waited for.
    """
    left = seconds
    while left > 0:
        now = await _attendee_count(page)
        if now > before:
            return now
        await asyncio.sleep(_LIST_POLL_S)
        left -= _LIST_POLL_S
    return await _attendee_count(page)


async def load_whole_list(page, *, ctl=None, cap: int = 0) -> int:
    """Scroll an attendee list to its end until it stops growing. Returns the
    count.

    THE missing half of a roster. The list loads twenty or so at a time and the
    next batch only arrives when the bottom is reached — so an event with forty
    attendees had twenty in the DOM, and everything downstream (the table, the
    ids, the batches) was working from half a list without either half knowing.

    What made it look settled was patience, not the page: three idle scroll
    rounds at the skimming pace is well under a second, which a list fetching
    its next page over the network loses every time. So each round here scrolls
    to the bottom and then WAITS for the count to move, and only a round that
    gets nothing after a real wait counts as idle.
    """
    seen = await _attendee_count(page)
    idle = 0
    while idle < _LIST_IDLE_ROUNDS:
        if ctl is not None and not await ctl.checkpoint(f"Loading the list "
                                                        f"({seen} so far)"):
            break
        if cap and seen >= cap:
            break
        # Two steps, not one: a single flick often stops short of the sentinel
        # the loader watches, and then nothing is fetched at all.
        for _ in range(2):
            try:
                await visit.scroll_step(page, swift=True)
            except Exception:  # noqa: BLE001 — navigated away mid-scroll
                return seen
        grown = await _grew_within(page, seen, _LIST_GROW_S)
        if grown > seen:
            seen, idle = grown, 0
        else:
            idle += 1
    return seen


async def _card_name(card) -> str:
    """The name on an attendee card, read before it is clicked."""
    try:
        heading = card.locator("h3, h4").first
        if await heading.count():
            return ((await heading.inner_text()) or "").strip()[:120]
    except Exception:  # noqa: BLE001
        pass
    return ""


async def _member_link_on(page) -> str:
    """A /members/ id from a link on the page, or "".

    The third shape a card can take: the click opens a drawer beside the list
    rather than going anywhere, and the id is in the "View profile" link inside
    it. The URL never changes, so nothing else here would find it.
    """
    try:
        link = page.locator("a[href*='/members/']").first
        if await link.count():
            return member_ref((await link.get_attribute("href")) or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _pages_of(context) -> list:
    """The context's live tab list, or [] for a context that has none."""
    try:
        return list(getattr(context, "pages", []) or [])
    except Exception:  # noqa: BLE001
        return []


async def _click_card(page, index: int) -> tuple:
    """Click card `index` on THIS list. Returns (name, clicked, tabs_before).

    No navigation of the list itself: the card opens the profile in a tab of its
    own, so the list stays where it is and the next card is still where it was.

    The tabs open BEFORE the click are handed back because that is how the one
    it opens is identified afterwards. `expect_page`/`expect_popup` used to do
    that job and is what this stopped using: it waits a fixed moment and gives
    up, and a tab that arrived a beat later was then nobody's — never read, and
    closed unread by the sweep at the end. That is the "id is right there in the
    URL and it did not take it" case, and the tab left open with it.
    """
    card = page.locator(_sel("attendee_card")).nth(index)
    context = getattr(page, "context", None)
    try:
        if not await card.count():
            return "", False, []
        name = await _card_name(card)
        # A card below the fold takes the click on its scrolled position, not
        # on where it was when the list was read.
        try:
            await card.scroll_into_view_if_needed(timeout=3_000)
        except Exception:  # noqa: BLE001 — already in view, or no scroll parent
            pass
    except Exception:  # noqa: BLE001
        return "", False, []

    before = _pages_of(context)
    try:
        await card.click(timeout=10_000)
    except Exception:  # noqa: BLE001
        return name, False, before
    return name, True, before


async def _member_url_now(page, seconds: float = _MEMBER_URL_S) -> str:
    """The /members/ URL a just-opened tab is pointed at, as soon as it has one.

    Polled tightly and NOT waited on beyond that: the id is in the address, so
    the answer exists the instant the navigation commits — long before the
    profile has rendered anything. A tab opened at window.open time starts on
    about:blank, which is the only reason this is a loop at all.
    """
    left = seconds
    while left > 0:
        url = page.url or ""
        if "/members/" in url:
            return url
        await asyncio.sleep(_URL_POLL_S)
        left -= _URL_POLL_S
    return page.url or ""


async def _read_and_close(popup, name: str, index: int,
                          dwell: float = 0.0) -> dict:
    """Take the id off an opened profile tab, then close it.

    The READ waits for nothing: the id is in the URL, so it is known the moment
    the navigation commits, long before the profile has rendered anything.

    `dwell` is how long the tab is then left up. At 0 it is shut at once, which
    is what a one-off click wants — the tab has already given up everything
    being asked of it. The walk passes a real dwell instead, because a window
    of five open profiles cannot hold five if each one closes on arrival; see
    `_MEMBER_DWELL_S`.
    """
    row = {"username": "", "name": name, "index": index}
    try:
        row["username"] = member_ref(await _member_url_now(popup))
        if dwell > 0:
            started = asyncio.get_running_loop().time()
            try:
                await popup.wait_for_load_state("domcontentloaded",
                                                timeout=int(dwell * 1000))
            except Exception:  # noqa: BLE001 — a slow profile is not a failure
                pass
            # The slot is held for the WHOLE dwell, however fast the profile
            # rendered. Closing early is what empties the window out: five
            # slots each held for an instant is one profile on screen, which
            # is the thing being fixed here.
            rest = dwell - (asyncio.get_running_loop().time() - started)
            if rest > 0:
                await asyncio.sleep(rest)
        # Only when the card gave no name — this one DOES touch the DOM, so it
        # is the one thing here worth not doing.
        if row["username"] and not row["name"]:
            row["name"] = await _profile_name(popup)
    except Exception:  # noqa: BLE001 — one dead tab is one missing id
        pass
    finally:
        try:
            await popup.close()
        except Exception:  # noqa: BLE001
            pass
    return row


async def _harvest_stray_tabs(context, keep) -> list[str]:
    """Close every tab except `keep`, taking any member id off it on the way.

    Read, not just closed. A stray here is a profile tab whose id is sitting in
    its address bar; the version that closed them blind threw away exactly the
    ids the run existed to collect, and did it silently.
    """
    found = []
    for other in _pages_of(context):
        if other is keep:
            continue
        try:
            handle = member_ref(other.url or "")
            if handle:
                found.append(handle)
        except Exception:  # noqa: BLE001
            pass
        try:
            await other.close()
        except Exception:  # noqa: BLE001 — a tab already gone is a tab closed
            pass
    return found


async def _after_click(page, context, before: list) -> tuple:
    """What the click did, as (what, tab). Waits only until it knows.

    `what` is "tab" (it opened one — `tab` is that page), "routed" (it took this
    page to the profile) or "" (neither, within the budget). The tab is
    identified by set difference against the tab list taken before the click,
    which is what makes it THIS click's tab rather than whichever one happened
    to open — the thing a fixed-timeout expect_popup could not promise, and the
    reason clicking several at once used to lose ids.
    """
    left = _MEMBER_OPEN_S
    while left > 0:
        fresh = [tab for tab in _pages_of(context) if tab not in before]
        if fresh:
            return "tab", fresh[0]
        if "/members/" in (page.url or ""):
            return "routed", None
        await asyncio.sleep(_URL_POLL_S)
        left -= _URL_POLL_S
    return "", None


async def _read_routed(page, index: int, name: str) -> dict:
    """The id off a page the card took THERE, then back to the list."""
    row = {"username": member_ref(page.url or ""), "name": name, "index": index}
    if not row["name"]:
        row["name"] = await _profile_name(page)
    try:
        await page.go_back(wait_until="domcontentloaded", timeout=15_000)
    except Exception:  # noqa: BLE001
        pass
    await _wait_for_url(page, "/attendees", seconds=_MEMBER_ROUTE_S)
    return row


async def _one_id(page, index: int, fallback_name: str) -> dict:
    """Click card `index`, take the id off whatever opens, close it. One card.

    Watches for ALL THREE outcomes at once, for as long as the budget allows,
    rather than waiting a fixed moment for each in turn:

      * a tab appeared — read its address, close it. The tab is found by
        comparing the context's tab list against the one taken before the
        click, so it counts however late it arrives;
      * this page routed to the profile — read the address, walk back;
      * a drawer opened beside the list — read its /members/ link. Checked last
        and only once the other two have failed, because a link is the weakest
        of the three signals and the list itself may hold one.

    The whole cost is a click and an address read, so this is quick enough not
    to need a burst — and a burst is what was losing ids: several clicks fired
    back to back each raced the tab the last one had opened.
    """
    context = getattr(page, "context", None)
    name, clicked, before = await _click_card(page, index)
    row = {"username": "", "name": name or fallback_name, "index": index}
    if not clicked:
        return row

    left = _MEMBER_OPEN_S
    while left > 0:
        # 1 — a tab it opened. Everything new is read and closed, so a click
        # that opened two leaves neither behind.
        fresh = [tab for tab in _pages_of(context) if tab not in before]
        if fresh:
            for tab in fresh:
                found = await _read_and_close(tab, row["name"], index)
                row["username"] = row["username"] or found["username"]
                row["name"] = row["name"] or found["name"]
            if row["username"]:
                return row
            break

        # 2 — it took this page there instead.
        if "/members/" in (page.url or ""):
            row["username"] = member_ref(page.url)
            if not row["name"]:
                row["name"] = await _profile_name(page)
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=15_000)
            except Exception:  # noqa: BLE001
                pass
            await _wait_for_url(page, "/attendees", seconds=_MEMBER_ROUTE_S)
            return row

        await asyncio.sleep(_URL_POLL_S)
        left -= _URL_POLL_S

    # 3 — nothing opened and nothing moved: a drawer is the remaining shape.
    if not row["username"]:
        row["username"] = await _member_link_on(page)
    return row


async def _walk_window(page, context, indices: list[int], live: list[dict], *,
                       width: int, ctl=None, on_row=None,
                       dwell: float | None = None) -> list[dict]:
    """Run `indices` through a ROLLING window of `width` OPEN PROFILES.

    Five profiles are up at once and five stay up: the moment one has given up
    its id and closed, the next card is clicked into the space it left — the
    sixth when the first closes, the seventh when the next does, whichever of
    the five that turns out to be. The shape it replaces was a convoy — open
    five, wait for the slowest, close all five, start the next five — which
    spent most of its time with three tabs idle waiting on one straggler.

    `width` counts TABS, not turns. A slot is taken before the card is clicked
    and given back only once that card's tab has closed, so what is open is
    what the operator sees: five profiles, replaced one at a time. This is the
    part that was missing — the id is in the address bar and the tab used to be
    shut the instant it committed, which made five slots hold one profile.

    The CLICKS are still serialised, and that is what keeps the ids right: a
    click is followed only as far as identifying the tab it opened, by set
    difference against the tabs that existed a moment before it, and two clicks
    racing that window is precisely how ids used to be handed to the wrong
    person. The waiting, reading and closing happen outside the lock, which is
    where the overlap actually is — so the five open tabs cost almost nothing
    against a walk that is bound by how fast the list can be clicked.

    Returns one row per card, each {username, name, index}.
    """
    results: list[dict] = []
    pending = iter(indices)
    take_lock = asyncio.Lock()          # whose turn it is to be clicked next
    click_lock = asyncio.Lock()         # the LIST, which only one may touch
    # The window itself: `width` profiles may be open at any moment. Held from
    # before the click to after the close, so a slot means a tab.
    slots = asyncio.Semaphore(max(1, width))
    # Read here rather than defaulted in the signature: the constant is the one
    # place the dwell is set, and a default bound at import time would ignore it
    # being changed.
    dwell = _MEMBER_DWELL_S if dwell is None else dwell
    stopped = False

    def land(row: dict) -> None:
        results.append(row)
        if on_row is not None:
            on_row(row)

    async def worker() -> None:
        nonlocal stopped
        while True:
            async with take_lock:
                if stopped:
                    return
                index = next(pending, None)
                if index is None:
                    return
                if ctl is not None and not await ctl.checkpoint(
                        f"Opening attendee {index + 1}"):
                    stopped = True
                    return

            fallback = ((live[index] or {}).get("name", "")
                        if index < len(live) else "")
            # One slot, one open profile. Taken before the click so a sixth is
            # never clicked while five are still up, and given back below only
            # once this one's tab is gone.
            await slots.acquire()
            try:
                async with click_lock:
                    name, clicked, before = await _click_card(page, index)
                    if not clicked:
                        land({"username": "", "name": fallback, "index": index})
                        continue
                    what, tab = await _after_click(page, context, before)
                    if what == "routed":
                        # It took the LIST there, so nothing else can be clicked
                        # until it is back — this one stays inside the lock.
                        land(await _read_routed(page, index, name or fallback))
                        continue
                    if what != "tab":
                        land({"username": await _member_link_on(page),
                              "name": name or fallback, "index": index})
                        continue

                # Outside the lock: this tab stays open, loading and being read,
                # while the next card is already being clicked in another worker.
                land(await _read_and_close(tab, name or fallback, index,
                                           dwell=dwell))
            finally:
                slots.release()
            await behavior.beat(_CLICK_MEMBER_GAP_S)

    # One worker more than the window is wide. The spare is what keeps the
    # window FULL: with exactly five, the worker whose tab just closed is also
    # the one that has to click the replacement, so the window sits at four for
    # as long as that click takes. The spare is already waiting on the slot and
    # clicks the moment it frees, which is what "as one closes, open the next"
    # means. It can never make a sixth tab — the semaphore is the cap.
    await asyncio.gather(*[worker() for _ in range(max(1, width) + 1)],
                         return_exceptions=True)
    return sorted(results, key=lambda row: row.get("index", 0))


async def harvest_attendees(page, *, width: int = _ID_TAB_WIDTH, ctl=None,
                            target: int = 0, on_found=None) -> list[dict]:
    """Walk an attendee list from the top, opening each card AS IT APPEARS.

    The other order — read every name, scroll to the end, then go back and click
    them all — meant the operator watched a list being collected, then watched
    it being walked a second time, and had to press a button between the two.
    This does the two passes as one: the first screenful is opened the moment it
    is on screen, and the list is scrolled for more only once there is nothing
    left on it to open.

    Returns [{username, name}] in list order, ids included. `on_found(rows)` is
    called each time a member lands, so the panel fills in as it goes.
    """
    context = getattr(page, "context", None)
    rows: list[dict] = []
    done: set[int] = set()
    results_seen: dict = {}
    width = max(1, min(int(width or _ID_TAB_WIDTH), _ID_TAB_MAX))
    dry = 0

    while dry < _LIST_IDLE_ROUNDS:
        if ctl is not None and not await ctl.checkpoint(f"{len(rows)} member(s)"):
            break
        try:
            live = await _members_on_page(page)
        except Exception:  # noqa: BLE001
            break

        fresh = [i for i in range(len(live)) if i not in done]
        if not fresh:
            # Nothing new on screen: scroll, and wait for the next batch. A list
            # that has genuinely ended looks exactly like a slow one, which is
            # why this waits rather than counting a quick round as the end.
            dry += 1
            before = await _attendee_count(page)
            for _ in range(2):
                try:
                    await visit.scroll_step(page, swift=True)
                except Exception:  # noqa: BLE001
                    return rows
            if await _grew_within(page, before, _LIST_GROW_S) > before:
                dry = 0
            continue

        dry = 0
        for index in fresh:
            done.add(index)
        if target:
            fresh = fresh[:max(0, target - len(rows))]

        if context is None:
            # No browser to open tabs in: keep the names, skip the opening.
            results = [{"username": (live[i] or {}).get("username", ""),
                        "name": (live[i] or {}).get("name", ""), "index": i}
                       for i in fresh]
        else:
            # A rolling window over the whole screenful — five profiles OPEN at
            # once, and the next card clicked the moment one of them closes,
            # whichever of the five that is. Publishing per ROW rather than per
            # group, because with a window there is no group: there is a steady
            # stream of people arriving.
            results = await _walk_window(
                page, context, fresh, live, width=width, ctl=ctl,
                on_row=lambda row: _publish(rows, live, results_seen, row,
                                            on_found))

        for result in sorted(results, key=lambda r: r.get("index", 0)):
            _keep_row(rows, live, results_seen, result)
        if on_found is not None:
            on_found(rows)
        if ctl is not None:
            ctl.advance(len(fresh), f"{len(rows)} member(s)")
        if target and len(rows) >= target:
            return rows[:target]

    return rows


def _keep_row(rows: list[dict], live: list[dict], seen: dict,
              result: dict) -> bool:
    """Fold one walked card into the roster, in list order. True if it is new."""
    index = result.get("index", -1)
    if index in seen:
        return False
    name = result.get("name") or ((live[index] or {}).get("name", "")
                                  if 0 <= index < len(live) else "")
    handle = result.get("username") or ((live[index] or {}).get("username", "")
                                        if 0 <= index < len(live) else "")
    if not (name or handle):
        return False
    seen[index] = True
    rows.append({"username": handle, "name": name})
    return True


def _publish(rows, live, seen, result, on_found) -> None:
    """One walked card, straight onto the panel's list.

    Per row, not per batch: a rolling window has no batches — people arrive one
    at a time as their tab closes, and that is how the table should fill.
    """
    if _keep_row(rows, live, seen, result) and on_found is not None:
        on_found(rows)


async def collect_ids(page, rows: list[dict], *, width: int = _ID_TAB_WIDTH,
                      ctl=None, limit: int = 0, on_found=None) -> int:
    """Recover the missing ids by clicking the cards on THIS list, `width` at a
    time.

    The list is loaded ONCE and never left: a card opens the profile in a tab of
    its own, the tab's ADDRESS carries the id, and it is shut the moment that
    address is known — no page load waited for, no list reloaded.

    A tab-full at a time, and the reason that is safe now is the binding: each
    click's tab is identified by set difference against the tabs open a moment
    before it, so five in flight are five known people. The burst that lost ids
    used expect_popup, which waits a fixed moment for "a" popup and hands back
    whichever turns up — with several opening at once, some clicks got the
    wrong tab and some got none.

    A card that gives nothing is tried once more before it is given up on: the
    misses were transient, not structural.

    The list is loaded to its END first, and again whenever this has worked
    through everything it can see. A roster arrives twenty at a time and the
    next twenty only come when the bottom is reached, so a pass that read the
    cards once and stopped there did forty people's work on twenty of them.

    `on_found(rows)` is called after each id lands, so the panel can fill in as
    the run goes rather than at the end of it.
    """
    context = getattr(page, "context", None)
    if context is None or not rows:
        return 0

    if "/attendees" not in (page.url or ""):
        activity.log("scraping", "Not on an attendee list — cannot click the "
                                 "cards for their ids", "warn")
        return 0

    width = max(1, min(int(width or _ID_TAB_WIDTH), _ID_TAB_MAX))
    filled, missed, done, stop = 0, [], set(), False
    while not stop:
        # To the end of the list before each sweep: more may have loaded while
        # the last sweep was clicking, and those are members too.
        await load_whole_list(page, ctl=ctl)
        try:
            live = await _members_on_page(page)
        except Exception:  # noqa: BLE001
            break
        wanted = [i for i, row in enumerate(live)
                  if i not in done and not (row or {}).get("username")]
        if limit and limit > 0:
            wanted = wanted[:max(0, limit - len(done))]
        if not wanted:
            break

        for index in wanted:
            done.add(index)
        # The same rolling window the walk uses: `width` profiles open at once,
        # the next clicked as soon as one closes.
        results = await _walk_window(page, context, wanted, live,
                                     width=width, ctl=ctl)

        for result in results:
            index = result.get("index", -1)
            fallback = ((live[index] or {}).get("name", "")
                        if 0 <= index < len(live) else "")
            if result.get("username"):
                if _fill_id(rows, result.get("name") or fallback,
                            result["username"]):
                    filled += 1
                    if on_found is not None:
                        on_found(rows)
            else:
                missed.append(index)
        if ctl is not None:
            ctl.advance(len(wanted), f"{filled} id(s)")

    # One retry for the ones that gave nothing. They are a click that did not
    # take, not a member without an id — and a member with no id is one the
    # operator cannot message at all, which is worth a second attempt.
    try:
        live = await _members_on_page(page)
    except Exception:  # noqa: BLE001
        live = []
    for index in list(missed):
        if stop or (ctl is not None
                    and not await ctl.checkpoint(f"Retrying {index + 1}")):
            break
        fallback = (live[index] or {}).get("name", "") if index < len(live) else ""
        result = await _one_id(page, index, fallback)
        if result.get("username") and _fill_id(
                rows, result["name"] or fallback, result["username"]):
            filled += 1
            missed.remove(index)
            if on_found is not None:
                on_found(rows)
        await behavior.beat(_CLICK_MEMBER_GAP_S)

    # Nothing this pass opened should still be up — and anything that is gets
    # read before it goes, because its address holds an id.
    stray = await _harvest_stray_tabs(context, keep=page)
    if stray:
        activity.log("scraping", f"Closed {len(stray)} tab(s) a card opened "
                                 f"behind us", "info")

    detail = f"Clicked {len(done)} attendee(s) — {filled} id(s) stored"
    if missed:
        detail += f", {len(missed)} still without one"
    activity.log("scraping", detail, "ok" if filled else "warn")
    return filled


async def ids_by_clicking(page, rows: list[dict], *, ctl=None, limit: int = 0,
                          force: bool = False) -> int:
    """Fill in the ids the attendee list did not carry, by clicking each card.

    Walks `rows` in the order _members_on_page produced them — the same DOM
    order the cards are in — and clicks the ones with no id. Mutates `rows` in
    place and returns how many ids were recovered.

    `force` clicks every card, including those whose id was already in the DOM.
    Off by default because a profile visit per attendee is a page load per
    attendee: with the id already in hand it buys nothing and costs twenty
    visits an event, which is twenty times more of this account on Meetup's
    logs than reading the list did.
    """
    if rows is None:
        return 0

    filled, opened, done, dry = 0, 0, set(), 0
    # The DOM is re-read each time round rather than trusted from the top: the
    # click routes this page to the profile and back, which re-renders the list,
    # and card positions taken before that would point at the wrong people.
    while True:
        try:
            live = await _members_on_page(page)
        except Exception:  # noqa: BLE001
            break
        index = next((i for i, row in enumerate(live)
                      if i not in done
                      and (force or not (row or {}).get("username"))), None)
        if index is None:
            # Everyone on screen is done — which is not the same as everyone.
            # The list lazy-loads and comes back scrolled to the top after each
            # profile, so the rest are below the fold rather than absent.
            if dry >= 2:
                break
            dry += 1
            try:
                await visit.scroll_step(page, swift=True)
            except Exception:  # noqa: BLE001
                break
            continue
        dry = 0
        if limit and opened >= limit:
            activity.log("scraping", f"Stopped clicking at {limit} attendee(s) — "
                                     f"the rest keep whatever the list gave them",
                         "info")
            break
        if ctl is not None and not await ctl.checkpoint(
                f"Opening attendee {index + 1}"):
            break

        done.add(index)
        opened += 1
        result = await click_attendee(page, index)
        if result["username"]:
            if _fill_id(rows, live[index].get("name") or result["name"],
                        result["username"]):
                filled += 1
        else:
            activity.log("scraping", f"Attendee {index + 1}: {result['detail']}",
                         "warn")
        # Between people. Twenty profiles opened in twenty consecutive frames is
        # not someone looking through a guest list.
        await behavior.beat(_CLICK_MEMBER_GAP_S)

    if opened:
        activity.log("scraping", f"Opened {opened} attendee(s) by clicking — "
                                 f"{filled} id(s) recovered", "ok" if filled else "warn")
    return filled


def _fill_id(rows: list[dict], name: str, handle: str) -> bool:
    """Put a clicked-through id on the row it belongs to. True if it landed.

    Matched by NAME rather than by position: the collected list is accumulated
    over a lazy scroll while the clicking walks the live DOM, and the two are
    only guaranteed to agree on who each person is, not on where they sit.
    """
    if not handle:
        return False
    if any((row or {}).get("username") == handle for row in rows):
        return False                       # already known under some other row
    wanted = (name or "").strip().lower()
    for row in rows:
        if not (row or {}).get("username") and \
                (row.get("name") or "").strip().lower() == wanted:
            row["username"] = handle
            # Their profile has BEEN opened — that is where this id came from.
            # The caller uses this to not open it a second time for nothing.
            row["clicked"] = True
            if name and not row.get("name"):
                row["name"] = name
            return True
    # A card the scroll never handed back — the click found a real person, so
    # they belong on the roster rather than being dropped for arriving late.
    rows.append({"username": handle, "name": name or "", "clicked": True})
    return True


async def visit_card(page, index: int, *, attendee_target: int = 40) -> dict:
    """Open one card, read the event page, come back. Returns {ok, detail, info}.

    The cards are `target="_blank"` links, so a click opens a NEW TAB rather
    than navigating. That is handled rather than worked around: the tab is
    caught, read and closed — which is exactly the shape of a person opening a
    listing, glancing at it and closing it, and it leaves the grid behind
    untouched so the next card is still where it was.

    Falls back to navigating this same page and going back, for the case where
    no popup appears (a card rendered without target=_blank, or a blocked
    window).
    """
    card = page.locator(_sel("event_card")).nth(index)
    try:
        link = card.locator(_sel("card_link")).first
        if await link.count() == 0:
            return {"ok": False, "detail": "Card has no link", "info": {}}
        href = ((await link.get_attribute("href")) or "").split("?")[0]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"Card unreadable ({type(exc).__name__})",
                "info": {}}

    context = getattr(page, "context", None)
    popup = None
    if context is not None:
        try:
            async with context.expect_page(timeout=8_000) as caught:
                await Pointer(page).click(link, swift=True)
            popup = await caught.value
        except Exception:  # noqa: BLE001 — no tab opened; fall through
            popup = None

    if popup is not None:
        try:
            await popup.wait_for_load_state("domcontentloaded", timeout=15_000)
            info = await read_event(popup)
            # Then "See all" — the attendees are the point of opening an event
            # at all, and they are only reachable from inside it.
            if attendee_target and await open_attendees(popup, href):
                info["attendees"] = await collect_attendees(popup, attendee_target)
        except Exception as exc:  # noqa: BLE001
            info = {"title": "", "description": "",
                    "error": f"{type(exc).__name__}"}
        finally:
            try:
                await popup.close()
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "detail": f"Visited {href}", "info": {**info, "url": href}}

    # No tab appeared. Go there directly, then back — the grid is restored by
    # the history move rather than by a reload, so its scroll position and
    # selected category survive.
    if not href:
        return {"ok": False, "detail": "Card has no href", "info": {}}
    if not await visit.goto(page, href):
        return {"ok": False, "detail": f"{href} would not load", "info": {}}
    info = await read_event(page)
    if attendee_target and await open_attendees(page, href):
        info["attendees"] = await collect_attendees(page, attendee_target)
    try:
        await page.go_back(wait_until="domcontentloaded", timeout=15_000)
    except Exception:  # noqa: BLE001
        await visit.goto(page, FIND_URL)
    return {"ok": True, "detail": f"Visited {href}", "info": {**info, "url": href}}


async def visit_cards(page, rows: list[dict], limit: int, *, ctl=None,
                      attendee_target: int = 40) -> int:
    """Open the first `limit` cards in the grid, folding what they say into
    `rows` in place. Returns how many were visited.

    Capped on purpose. Twenty categories times every card is hundreds of page
    loads; the cap is what keeps a browse a browse.
    """
    if limit <= 0 or not rows:
        return 0
    visited = 0
    for index in range(min(limit, len(rows))):
        if ctl is not None and not await ctl.checkpoint():
            break
        result = await visit_card(page, index, attendee_target=attendee_target)
        if result["ok"]:
            info = result["info"]
            rows[index]["description"] = info.get("description", "")
            if info.get("title"):
                rows[index]["title"] = info["title"] or rows[index].get("title", "")
            rows[index]["members"] = info.get("attendees", [])
            rows[index]["visited"] = True
            visited += 1
        else:
            rows[index]["visited"] = False
            activity.log("scraping", f"Card {index + 1}: {result['detail']}", "warn")
        await behavior.beat(_CATEGORY_GAP_S)
    return visited


async def cards_when_ready(page, limit: int = 5) -> list[dict]:
    """The first `limit` cards, as soon as the grid has ANY.

    For the roll, which opens the first card and works down from there. The
    settled read it replaces did two things that sat between the click and the
    event: it waited for the grid to stop GROWING — a list nobody is going to
    pick from — and then read every card on it, and each card is several DOM
    reads for a title, a time, a group and an attendee count. Twelve cards of
    that is a visible pause before anything happens.

    The wait for the first card is kept, because that one is not optional: it is
    what tells a loading shimmer apart from an empty category.
    """
    if not await wait_for_cards(page, grace=_GRID_CLICK_GRACE_S):
        return []
    return await cards_on_page(page, limit=max(1, limit))


async def cards_on_page(page, limit: int = 200) -> list[dict]:
    """Every event card currently rendered, as rows.

    Read-only, and only what is already in the DOM: the grid lazy-loads on
    scroll, so this is "what this category showed us", not "everything Meetup
    has". Scrolling each of twenty categories to exhaustion would turn a browse
    into a crawl.
    """
    cards = page.locator(_sel("event_card"))
    try:
        total = await cards.count()
    except Exception:  # noqa: BLE001
        return []

    rows, seen = [], set()
    for index in range(min(total, limit)):
        row = await _read_card(cards.nth(index))
        if not row:
            continue
        key = row["event_id"] or row["url"]
        if key in seen:            # the card selectors are comma-joined
            continue
        seen.add(key)
        rows.append(row)
    return rows


# How many cards identify the grid. The count alone does not: two categories
# with twelve events each look identical by count, which is how a grid that had
# not swapped yet passed for the new one.
_GRID_SAMPLE = 8


async def cards_when_settled(page, *, budget: float = 5.0,
                            limit: int = 200) -> list[dict]:
    """The grid's cards once it has stopped GROWING.

    cards_on_page reads whatever is in the DOM at that instant, and the results
    arrive in stages — the first card or two render, then the rest. Reading the
    moment the swap was detected therefore handed back a category's first event
    and nothing else, which is not a list anyone can pick from: it looks like the
    app chose the event for you.

    The click's own wait cannot do this job. That one answers "is this the new
    category yet", which the FIRST new card already settles; this one answers
    "is all of it here", which only stops being false once the count holds.

    Neither can start until there is anything to count, hence the shimmer wait
    first: a grid still painting placeholders is stable at zero cards, and two
    stable polls of nothing used to be reported as a category with no events.
    """
    await wait_for_cards(page)
    rows = await cards_on_page(page, limit=limit)
    left, stable = budget, 0
    while left > 0:
        step = random.uniform(*_CATEGORY_SETTLE_S)
        await asyncio.sleep(step)
        left -= step
        now = await cards_on_page(page, limit=limit)
        if len(now) == len(rows):
            stable += 1
            if stable >= 2:
                return now
        else:
            stable = 0
        rows = now
    return rows


async def _grid_fingerprint(page, sample: int = _GRID_SAMPLE) -> tuple:
    """WHICH cards the grid is showing — (total, the first few event ids).

    Two different categories sharing a total AND their first eight event ids is
    not a thing that happens; a grid that has not swapped yet is.
    """
    cards = page.locator(_sel("event_card"))
    try:
        total = await cards.count()
    except Exception:  # noqa: BLE001
        return ()
    ids = []
    for index in range(min(total, sample)):
        card = cards.nth(index)
        ref = ""
        try:
            ref = (await card.get_attribute("data-eventref")) or ""
        except Exception:  # noqa: BLE001
            ref = ""
        if not ref:
            try:
                link = card.locator(_sel("card_link")).first
                if await link.count():
                    ref = ((await link.get_attribute("href")) or "").split("?")[0]
            except Exception:  # noqa: BLE001
                ref = ""
        ids.append(ref or f"#{index}")
    return (total, tuple(ids))


async def _settled_grid(page, before: int, was: tuple) -> tuple[int, bool]:
    """Wait for the new category's results, as (card count, stale).

    TWO different waits, and conflating them is what put every category's events
    under the wrong heading. Settling is "the list stopped changing"; swapping is
    "this is the new category's list at all". The old code only did the first, so
    a click was followed ~0.3s later by a still-unswapped grid that had been
    stable the whole time — and every card of category A was harvested again and
    stored tagged as category B, C, D…, which the event-id upsert then collapsed
    into one heading holding everything.

    `stale` says the grid never became anything new. The caller does NOT store
    those cards: attributing them to this category is precisely the bug, and an
    empty category is a smaller lie than a wrong one.

    A count of ZERO is only a settled answer once wait_for_cards has said so —
    it used to be taken at face value, which read a loading shimmer as an empty
    category. An empty grid is never stale: there is nothing there to mislabel.
    """
    # First, past the shimmer. Until this returns, "how many cards" has no
    # meaningful answer and neither does "is this the new category". The longer
    # grace is because a click just happened: the results ARE coming, so a still
    # empty grid here is far more often one that has not started than one with
    # nothing on it.
    await wait_for_cards(page, grace=_GRID_CLICK_GRACE_S)

    left = _CATEGORY_SWAP_S
    now = was
    while left > 0:
        step = random.uniform(*_CATEGORY_SETTLE_S)
        await asyncio.sleep(step)
        left -= step
        now = await _grid_fingerprint(page)
        # Empty is an answer; anything different is the swap.
        if not _grid_has_cards(now) or now != was:
            break

    stale = _grid_has_cards(now) and now == was
    return await _settled_count(page, before), stale


def _grid_has_cards(fingerprint: tuple) -> bool:
    return bool(fingerprint) and fingerprint[0] > 0


async def _settled_count(page, before: int) -> int:
    """The grid's size once it stops changing.

    Informational only; whether the click landed is settled by
    _category_selected, which reads the page's own state.
    """
    left = _CATEGORY_WAIT_S
    last, stable = before, 0
    while left > 0:
        step = random.uniform(*_CATEGORY_SETTLE_S)
        await asyncio.sleep(step)
        left -= step
        now = await _card_count(page)
        if now == last:
            stable += 1
            if stable >= 2:
                return now
        else:
            last, stable = now, 0
    return last


async def sweep_categories(page, *, ctl=None, keys: list[str] | None = None,
                           limit: int = 0, visit_cards_per_category: int = 3,
                           attendee_target: int = 40) -> dict:
    """Click every category tab on /find/, one at a time.

    One at a time with a pause between, for the same reason everything else here
    is paced: a strip of twenty tabs clicked in twenty consecutive frames is not
    a person browsing, it is a script walking a list.

    Returns {ok, visited, detail} where `visited` is [{key, name, cards, ok}].
    """
    opened = await open_find(page)
    if not opened["ok"]:
        return {**opened, "visited": []}

    categories = await list_categories(page)
    if not categories:
        return {"ok": False, "detail": "No category tabs on the Find page",
                "visited": []}
    if keys:
        wanted = set(keys)
        categories = [c for c in categories if c["key"] in wanted]
    if limit and limit > 0:
        categories = categories[:limit]

    if ctl is not None:
        ctl.total = len(categories)

    visited = []
    for index, category in enumerate(categories):
        if ctl is not None and not await ctl.checkpoint(f"Category {category['name']}"):
            break
        result = await click_category(page, category["key"])
        # A grid that never changed is the PREVIOUS category's, and harvesting it
        # here is how its events end up filed under this name. Report the miss
        # and take nothing.
        stale = bool(result.get("stale"))
        # cards_when_settled, not cards_on_page: the sweep was reading the grid
        # while it was still filling in, so a category reported the two cards
        # that had rendered so far — or none at all, if the shimmer was still up.
        events = (await cards_when_settled(page)
                  if result["ok"] and not stale else [])
        opened = 0
        if events:
            opened = await visit_cards(page, events, visit_cards_per_category,
                                       ctl=ctl, attendee_target=attendee_target)
        members = sum(len(row.get("members") or []) for row in events)
        visited.append({**category, "cards": result["cards"], "ok": result["ok"],
                        "stale": stale,
                        "detail": result["detail"], "events": events,
                        "visited_cards": opened, "members": members})
        activity.log("scraping",
                     (f"{category['name']}: results never changed — skipped "
                      f"(would have re-filed the previous category's events)")
                     if stale else
                     (f"{category['name']}: {len(events)} event(s), "
                      f"{opened} opened, {members} member(s)"),
                     "ok" if result["ok"] and not stale else "warn")
        if ctl is not None:
            ctl.advance(1, f"{len(visited)}/{len(categories)}")
        if index < len(categories) - 1:
            # Not human.pause(): that is the between-ACTIONS delay, sized for
            # sending messages, and twenty of them is minutes of waiting to look
            # careful about something nobody can see.
            await behavior.beat(_CATEGORY_GAP_S)

    # A stale category was opened but yielded nothing usable, so it is not one of
    # the ones that worked — counting it would report a clean sweep over a run
    # that filed most of its tabs under nothing.
    done = sum(1 for v in visited if v["ok"] and not v.get("stale"))
    skipped = sum(1 for v in visited if v.get("stale"))
    detail = f"Visited {done}/{len(categories)} categories"
    if skipped:
        detail += f" — {skipped} showed no new results"
    return {"ok": done > 0, "visited": visited, "detail": detail}


async def search_groups(page, query: str, location: str = "", *,
                        limit: int = 40) -> list[dict]:
    """Find groups by keyword, for picking scrape targets.

    Returns [{urlname, name, url}]. This is the front of the funnel: an operator
    searches a topic, picks the groups worth targeting, and those urlnames become
    what the scraper walks.
    """
    params = f"?keywords={query.replace(' ', '%20')}"
    if location:
        params += f"&location={location.replace(' ', '%20')}"
    if not await visit.goto(page, f"{config.BASE_URL}/find/groups/{params}"):
        return []
    await visit.dismiss_overlays(page, SEL["overlays"])
    await guard_challenge(page, "group search")

    found = await visit.scroll_to_end(page, collect=_groups_on_page, target=limit)
    activity.log("scrape", f"Search {query!r}: {len(found)} group(s)", "ok")
    return found[:limit]


async def sweep_search(page, *, ctl=None, rounds: int = 12,
                       location: str = "") -> list[dict]:
    """Discover groups by typing short random seeds, one after another.

    Nine seed SHAPES (behavior.seed) rather than one template: a search log of
    forty two-letter lowercase queries is a fingerprint on its own, even when
    each individual query is unremarkable.

    The pacing here is the point, and it is deliberately slow. An earlier flat
    ~40ms-per-search version produced five or six searches a second — which is
    not a fast human, it is obviously not a human at all. Each round now waits
    for *its own* results before moving on, dwells over them, and only then
    types the next seed.
    """
    found: dict[str, dict] = {}
    backoff = behavior.Backoff()

    for index in range(rounds):
        if ctl is not None and not await ctl.checkpoint(f"Search round {index + 1}"):
            break

        term = behavior.seed()
        url = f"{config.BASE_URL}/find/groups/?keywords={term}"
        if location:
            url += f"&location={location.replace(' ', '%20')}"
        if not await visit.goto(page, url):
            await backoff.sleep()
            continue

        await visit.dismiss_overlays(page, SEL["overlays"])
        await guard_challenge(page, "search sweep")

        # The correctness gate: wait for THIS seed's results rather than reading
        # whatever the previous round left on screen. Without it the run reports
        # results it never actually saw, and fires the next search immediately.
        await behavior.beat(behavior.PRE_TYPE_BEAT_S)
        ready = await visit.wait_ready(page, _sel("group_card"), timeout_s=8.0)

        fresh = 0
        if ready:
            for row in await _groups_on_page(page):
                if row["urlname"] not in found:
                    found[row["urlname"]] = row
                    fresh += 1

        if fresh:
            backoff.reset()
            # Dwell over the results — a page of results that is left the moment
            # it renders was never looked at.
            await behavior.beat(behavior.RESULTS_DWELL_S)
            await behavior.beat(behavior.BETWEEN_SEARCH_S)
        else:
            # Nothing new usually means a rate-limit window, not an empty site.
            waited = await backoff.sleep()
            activity.log("scrape", f"Seed {term!r} returned nothing — "
                                   f"backing off {waited:.0f}s", "warn")

    activity.log("scrape", f"Seed sweep: {len(found)} group(s) over {rounds} round(s)",
                 "ok")
    return list(found.values())


# Path prefixes that look like a group urlname but aren't one.
_NOT_A_GROUP = {
    "find", "members", "account", "help", "about", "blog", "login", "register",
    "home", "topics", "cities", "start", "pro", "legal", "terms", "privacy",
    "messages", "notifications", "search", "api", "apps", "gdpr",
}


# ── Actions ───────────────────────────────────────────────────────────


# How long a composer has to render before "this member cannot be messaged" is
# believed. A real refusal shows no box at all and costs this in full, which is
# why it is seconds rather than the inbox's longer budget — it is paid once per
# recipient who cannot be reached, and a campaign can have a run of them.
_COMPOSER_WAIT_S = 10.0


async def send_message(page, handle: str, text: str,
                       image: str | None = None, name: str = "",
                       ctl=None) -> dict:
    """Message one member. Returns {ok, detail}.

    Whether a member can be messaged at all depends on their privacy settings and
    on whether a shared group allows it — so "no composer on the page" is a
    normal, expected outcome, reported rather than raised.

    The address is Settings → chat_link with this member's id filled in, so the
    operator can repoint the whole campaign at a different composer URL without a
    new build. `name` only matters to a template that asks for {name}.

    `ctl` reaches all the way into the TYPING, which is where a campaign spends
    most of its time: a Pause that only lands between recipients leaves the bot
    writing for another minute, and a Stop must never leave a half-typed message
    to be sent. Ending mid-message raises control.RunStopped from here.
    """
    from backend.automation import media

    url = chat_link(handle, name)
    if not await visit.goto(page, url):
        return {"ok": False, "detail": "Message page would not load"}
    await visit.dismiss_overlays(page, SEL["overlays"])
    await guard_challenge(page, f"message {handle}")

    # WAITED for, not counted the instant the navigation returned. The composer
    # this now opens is Meetup's new-conversation view, which is React and
    # arrives well after domcontentloaded — asking for it at once found nothing
    # every time and reported a whole run as "cannot be messaged".
    box = await _wait_for(page, "message_box", seconds=_COMPOSER_WAIT_S,
                          ordered=True)
    if box is None:
        return {"ok": False, "detail": "Member cannot be messaged"}

    await human.think()
    # THE box that was found, not the selector again. Re-resolving it would take
    # the first textarea in DOM order, and Meetup's inbox has one per
    # conversation pane — which is the chat that opened but never got written to.
    await human.type_or_fill(page, box, text, clear=True, ctl=ctl)

    if image:
        try:
            await media.attach(page, _sel("message_attach"), image)
            await human.pause()
        except media.MediaError as exc:
            # Send the text anyway. A missing attachment is worth reporting, but
            # not worth dropping a message that is otherwise ready.
            activity.log("campaign", f"Attachment skipped: {exc}", "warn")

    if not _clicking_send():
        return dict(_NOT_CLICKED)

    # The last look before the irreversible bit. Stopping during the attachment
    # or the pause below has to stop the SEND too, not just the writing — this is
    # the one step in the run that cannot be taken back.
    if ctl is not None:
        await ctl.guard()
    await human.pause()
    pointer = Pointer(page)
    await pointer.click(page.locator(_sel("message_send")).first)
    await asyncio.sleep(random.uniform(1.5, 3.5))

    if await hit_challenge(page):
        return {"ok": False, "detail": "Challenged on send"}
    return {"ok": True, "detail": f"Messaged {handle}"}


# ── A member's profile → their chat ───────────────────────────────────
# The pipeline's last leg: open the member, press the control they would press,
# write, send. Deliberately NOT send_message() above, which types the composer
# URL straight into the address bar. Both end at the same composer, but only one
# of them is a person opening a profile and clicking Chat — and the profile is
# also where a member who cannot be messaged at all says so, before anything is
# typed.


async def _first_of(page, key: str, scan: int = 6):
    """The first VISIBLE match, trying `key`'s candidates one at a time in order.

    _sel() comma-joins candidates and lets Playwright pick the first in DOM
    ORDER, which throws away the ordering the list was written in. For a control
    identified partly by its text that is the wrong trade: `button:has-text(…)`
    matches any ancestor containing the word, so the joined form hands back a
    header button while the profile's own Chat sits further down the page.
    """
    for candidate in SEL[key]:
        try:
            found = await _first_visible(page.locator(candidate), scan=scan)
        except Exception:  # noqa: BLE001 — a bad selector must not stop the rest
            continue
        if found is not None:
            return found
    return None


async def open_chat(page, handle: str = "") -> dict:
    """From a member's profile, get to a composer aimed at them.

    Returns {ok, detail, via} — `via` is "button" when the profile's own control
    was clicked and "route" when the composer had to be opened directly, which
    is worth knowing: a run that is always taking the route means the selector
    has drifted again.
    """
    if "/messages/" in (page.url or ""):
        return {"ok": True, "detail": "Composer already open", "via": "already"}

    button = await _first_of(page, "profile_chat")
    if button is not None:
        try:
            await Pointer(page).click(button, swift=True)
            landed = await _wait_for_url(page, "/messages/", seconds=8.0)
            if landed or await _wait_for(page, "message_box", seconds=4.0,
                                         ordered=True) is not None:
                return {"ok": True, "detail": "Chat opened from the profile",
                        "via": "button"}
        except Exception:  # noqa: BLE001 — fall through to the route
            pass

    if not handle:
        return {"ok": False, "detail": "No chat control on this profile", "via": ""}
    if not await visit.goto(page, chat_link(handle)):
        return {"ok": False, "detail": "Composer would not load", "via": "route"}
    await visit.dismiss_overlays(page, SEL["overlays"])
    if await _wait_for(page, "message_box", seconds=8.0, ordered=True) is None:
        return {"ok": False, "detail": "Member cannot be messaged", "via": "route"}
    return {"ok": True, "detail": "Chat opened", "via": "route"}


async def chat_with_member(page, handle: str, text: str,
                           image: str | None = None) -> dict:
    """Profile → Chat → type → send, on the tab it is handed.

    Takes a page rather than opening one so the caller owns the tab: the pipeline
    keeps several open at once and closes each as its message goes.

    Every step reports instead of raising, and `stage` says which one stopped it
    — "profile", "chat" or "send" are three genuinely different failures (a dead
    profile, a member who cannot be messaged, a composer that refused the text).
    """
    handle = (handle or "").strip().strip("@")
    if not handle:
        return {"ok": False, "detail": "No member handle", "stage": "profile"}

    if f"/members/{handle}" not in (page.url or "") and "/messages/" not in (page.url or ""):
        if not await visit.goto(page, member_url(handle)):
            return {"ok": False, "detail": f"@{handle}'s profile would not load",
                    "stage": "profile"}
    await visit.dismiss_overlays(page, SEL["overlays"])
    await guard_challenge(page, f"profile {handle}")

    opened = await open_chat(page, handle)
    if not opened["ok"]:
        return {**opened, "stage": "chat", "handle": handle}

    sent = await send_in_open_chat(page, text, image=image)
    return {**sent, "stage": "send", "handle": handle, "via": opened.get("via", "")}


# ── The inbox ─────────────────────────────────────────────────────────
# Replying in a thread someone already opened is a different act from cold
# outreach, and a different page: send_message() above opens a fresh composer at
# /messages/new/?member=<handle>, while these walk the existing conversation
# list. They are kept apart because the failure modes are unrelated — cold
# outreach fails on "member cannot be messaged", a reply fails on "no
# conversations yet".

INBOX_URL = f"{config.BASE_URL}/messages/"

# How long to wait for a React-rendered list or composer to appear. Polled
# rather than slept through: the page is usually ready long before this.
_INBOX_WAIT_S = 15.0


async def _first_visible(locator, scan: int = 12):
    """The first locator in `locator` that is actually on screen, or None.

    The row selectors are comma-joined fallbacks, so the first DOM match can be
    an off-screen or collapsed element from a candidate meant for a different
    layout. Clicking one of those silently does nothing.
    """
    try:
        total = await locator.count()
    except Exception:  # noqa: BLE001
        return None
    for index in range(min(total, scan)):
        item = locator.nth(index)
        try:
            if await item.is_visible():
                return item
        except Exception:  # noqa: BLE001
            continue
    return None


async def _wait_for(page, key: str, seconds: float = _INBOX_WAIT_S,
                    ordered: bool = False):
    """Poll until one of `key`'s selectors is visible. Returns it, or None.

    A poll, not a sleep: the inbox renders client-side and its list arrives well
    after domcontentloaded, so a fixed wait is either short enough to miss it or
    long enough to waste every run.

    `ordered` tries the candidates IN THE ORDER THEY ARE WRITTEN rather than
    comma-joined. It matters wherever a list ends in a catch-all: "message_box"
    finishes with a bare `textarea`, and joined into one selector that catch-all
    is indistinguishable from the specific candidates above it — so the first
    visible textarea on the page wins over the one actually identified as the
    composer.
    """
    deadline = seconds
    while deadline > 0:
        found = (await _first_of(page, key) if ordered
                 else await _first_visible(page.locator(_sel(key))))
        if found is not None:
            return found
        # Tight, like every other wait on something already asked for: the step
        # is latency in front of the operator, not pacing anyone can observe.
        step = random.uniform(0.12, 0.3)
        await asyncio.sleep(step)
        deadline -= step
    return None


async def _wait_for_url(page, fragment: str, seconds: float = 8.0) -> bool:
    """Poll until `fragment` appears in the page URL. False if it never does.

    Polled tightly: this is a wait on a navigation that has already been asked
    for, so the step size is pure latency added to every "See all" — it buys no
    disguise, because nothing is being done while it ticks.
    """
    left = seconds
    while left > 0:
        if fragment in (page.url or ""):
            return True
        step = random.uniform(0.12, 0.28)
        await asyncio.sleep(step)
        left -= step
    return fragment in (page.url or "")


async def click_messages_icon(page) -> dict:
    """Click the Messages icon in the header. Returns {ok, detail}.

    Present only on a signed-in header, so "not there" usually means logged out
    rather than a dead selector — which is why the caller checks the session
    rather than treating this as a markup failure.
    """
    icon = await _first_visible(page.locator(_sel("header_messages")))
    if icon is None:
        return {"ok": False, "detail": "No Messages icon in the header"}
    try:
        await human.think()
        await Pointer(page).click(icon)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"Could not click Messages ({type(exc).__name__})"}
    if not await _wait_for_url(page, "/messages"):
        return {"ok": False, "detail": "Clicked Messages but the inbox did not open"}
    return {"ok": True, "detail": "Opened Messages from the header"}


async def open_inbox(page) -> dict:
    """Land on the message inbox. Returns {ok, detail}.

    Prefers CLICKING the header's Messages icon over navigating to /messages/.
    Following the link is what a person does; a bare goto is a URL nobody typed,
    arriving with no referrer and no preceding click on any page that links
    there. The goto stays as the fallback for a cold page whose header hasn't
    rendered, or a layout that doesn't carry one.
    """
    if "/messages" not in (page.url or ""):
        clicked = await click_messages_icon(page)
        if not clicked["ok"] and not await visit.goto(page, INBOX_URL):
            return {"ok": False, "detail": "Inbox would not load"}
    await visit.dismiss_overlays(page, SEL["overlays"])
    await guard_challenge(page, "inbox")

    # The inbox is behind the login wall, so a logged-out session lands on the
    # login form instead — which has none of the row selectors and would
    # otherwise read as "no conversations".
    if not await is_logged_in(page):
        return {"ok": False, "detail": "Not logged in — the inbox needs a session"}
    return {"ok": True, "detail": "Inbox open"}


async def list_conversations(page, limit: int = 50) -> list[dict]:
    """The people in the inbox, top-first, as [{name}].

    Read-only — it names who is there, it does not open anything. Used to show a
    roster before a send and to prove the inbox actually has threads in it.

    Adjacent duplicates are collapsed: `chat_row`'s fallbacks can match both the
    <li> and the <a> inside it, which are the same conversation with the same
    name back to back. Collapsing only CONSECUTIVE repeats keeps two genuinely
    different people who happen to share a name (they wouldn't be adjacent).
    """
    rows = page.locator(_sel("chat_row"))
    try:
        total = await rows.count()
    except Exception:  # noqa: BLE001
        return []

    names: list[str] = []
    for index in range(min(total, limit)):
        row = rows.nth(index)
        try:
            if not await row.is_visible():
                continue
        except Exception:  # noqa: BLE001
            continue
        name = ""
        try:
            label = row.locator(_sel("chat_name")).first
            if await label.count():
                name = ((await label.inner_text()) or "").strip().splitlines()[0]
        except Exception:  # noqa: BLE001
            pass
        if name and names and name == names[-1]:
            continue                      # the <li>/<a> pair for one conversation
        names.append(name)
    return [{"name": n} for n in names]


async def open_first_chat(page) -> dict:
    """Open the conversation at the top of the inbox. Returns {ok, detail, name}.

    "First" is inbox order, which is most-recent-first — the thread at the top is
    the one that was last active, not the oldest.
    """
    row = await _wait_for(page, "chat_row")
    if row is None:
        return {"ok": False, "detail": "No conversations in the inbox", "name": ""}

    name = ""
    try:
        label = row.locator(_sel("chat_name")).first
        if await label.count():
            name = ((await label.inner_text()) or "").strip().splitlines()[0]
    except Exception:  # noqa: BLE001
        pass                              # a nameless row still opens fine

    await human.think()
    pointer = Pointer(page)
    await pointer.click(row)

    # The composer is the honest "the thread is open" signal — the row staying
    # highlighted is not, and neither is the URL, which Meetup updates before the
    # thread has rendered.
    if await _wait_for(page, "message_box", ordered=True) is None:
        return {"ok": False, "name": name,
                "detail": f"Opened {name or 'the first chat'} but no composer appeared"}
    await guard_challenge(page, "chat")
    return {"ok": True, "detail": f"Opened chat with {name or 'the first contact'}",
            "name": name}


async def _send_button(page, seconds: float = _SUBMIT_ENABLE_S):
    """The composer's Send control once it is actually clickable, or None.

    The live button starts `data-disabled="true"` with pointer-events switched
    off and only flips to `data-disabled="false"` once React sees text in the
    box — and it uses that attribute, NOT the native `disabled` one, so
    Playwright reads it as enabled the whole time. A click fired before the flip
    lands on a pointer-events:none element and silently does nothing, which is
    exactly "it wrote the message but never sent". So poll the attribute.
    """
    left = seconds
    while left > 0:
        button = await _first_visible(page.locator(_sel("message_send")))
        if button is not None:
            try:
                data_disabled = (await button.get_attribute("data-disabled")) or "false"
                native_disabled = await button.is_disabled()
            except Exception:  # noqa: BLE001
                data_disabled, native_disabled = "false", False
            if data_disabled.lower() != "true" and not native_disabled:
                return button
        step = random.uniform(0.25, 0.5)
        await asyncio.sleep(step)
        left -= step
    return None


def _clicking_send() -> bool:
    """Whether a written message gets its Send click. Settings → click_send.

    Off is a real mode, not a dry run: the composer is opened, the text typed
    and the image attached, and then it is left alone for a person to read and
    press Send. Nothing else about the run changes — which is the point, since
    what it would say is only worth checking as the run would actually say it.
    """
    return bool(app_settings.current().get("click_send", True))


# What a send returns when the click was withheld. `sent` False is what keeps a
# campaign from counting it — the message exists, on screen, unsent.
_NOT_CLICKED = {"ok": True, "sent": False,
                "detail": "Typed and left in the composer — Click Send is off"}


async def send_in_open_chat(page, text: str, image: str | None = None) -> dict:
    """Type into the composer of the thread already on screen and send it.

    Assumes a thread is open — open_first_chat() is what puts one there.
    """
    from backend.automation import media

    text = (text or "").strip()
    if not text:
        return {"ok": False, "detail": "Nothing to send — the message is empty"}

    box = await _wait_for(page, "message_box", ordered=True)
    if box is None:
        return {"ok": False, "detail": "No composer on this thread"}

    await human.think()
    # The found box, not the selector again — see send_message.
    await human.type_or_fill(page, box, text, clear=True)

    if image:
        try:
            await media.attach(page, _sel("message_attach"), image)
            await human.pause()
        except media.MediaError as exc:
            activity.log("campaign", f"Attachment skipped: {exc}", "warn")

    if not _clicking_send():
        return dict(_NOT_CLICKED)

    await human.before_submit()
    # Wait for Send to actually become clickable before clicking it — see
    # _send_button. Without this the click can fire while the button is still
    # pointer-events:none, which is the "typed but never sent" the operator sees.
    send = await _send_button(page)
    if send is None:
        return {"ok": False,
                "detail": "Send button never became clickable — the composer did "
                          "not accept the message"}
    pointer = Pointer(page)
    await pointer.click(send)
    await asyncio.sleep(random.uniform(1.5, 3.5))

    if await hit_challenge(page):
        return {"ok": False, "detail": "Challenged on send"}

    # Meetup clears the composer once the message is accepted. Checking it is the
    # difference between "we clicked Send" and "the message went" — a disabled
    # or mis-targeted button swallows the click with no error at all.
    try:
        left = (await box.input_value()) or ""
    except Exception:  # noqa: BLE001
        left = ""
    if left.strip() == text:
        return {"ok": False, "detail": "Send did not go through — the box still holds the text"}
    return {"ok": True, "detail": "Message sent"}


async def message_first_chat(page, text: str, image: str | None = None) -> dict:
    """Inbox -> the top conversation -> type -> send.

    Returns {ok, detail, name, stage, conversations} where `conversations` is the
    roster of names the inbox held — so a caller can show who was there and which
    one got the message, whichever step the flow reached.

    The whole flow behind one call so a caller doesn't have to know the order.
    Every step reports rather than raises, so a run gets back which one stopped
    it instead of a traceback.
    """
    step = await open_inbox(page)
    if not step["ok"]:
        return {**step, "name": "", "stage": "inbox", "conversations": []}

    names = [c["name"] for c in await list_conversations(page)]

    opened = await open_first_chat(page)
    if not opened["ok"]:
        return {**opened, "stage": "chat", "conversations": names}

    sent = await send_in_open_chat(page, text, image=image)
    name = opened.get("name") or "the first contact"
    detail = f"{sent['detail']} to {name}" if sent["ok"] else sent["detail"]
    activity.log("campaign", detail, "ok" if sent["ok"] else "error")
    return {**sent, "detail": detail, "name": opened.get("name", ""), "stage": "send",
            "conversations": names}


async def rsvp(page, event_url: str) -> dict:
    """RSVP to one event. Returns {ok, detail}.

    An RSVP is a public, visible action on someone else's event and it commits
    the account to a real-world attendance list — so unlike a scrape, this is one
    the operator has to have asked for explicitly per event.
    """
    if not await visit.goto(page, event_url):
        return {"ok": False, "detail": "Event page would not load"}
    await visit.dismiss_overlays(page, SEL["overlays"])
    await guard_challenge(page, "rsvp")

    button = page.locator(_sel("rsvp_button")).first
    if await button.count() == 0:
        return {"ok": False, "detail": "No RSVP control (full, closed, or already going)"}

    await human.think()
    pointer = Pointer(page)
    await pointer.click(button)
    await asyncio.sleep(random.uniform(1.5, 3.0))
    return {"ok": True, "detail": f"RSVP'd to {event_url}"}


# ── Decoy activity ────────────────────────────────────────────────────
# A session that is nothing but outreach has a request mix no real account
# produces: no browsing, no reading, no navigation that isn't a send. These are
# ordinary things a member does, fired between batches of real work so the
# session's shape is a person using Meetup rather than a script draining a list.
#
# Every one is read-only. A decoy must never RSVP, message, join or follow —
# it exists to shape traffic, and an action with a real consequence taken to
# disguise another action is not something this bot should do behind the
# operator's back.

DECOYS = (
    ("home feed", "/"),
    ("events near you", "/find/events/"),
    ("your groups", "/find/groups/"),
    ("notifications", "/notifications/"),
    ("your profile", "/profile/"),
)


async def decoy(page) -> str:
    """Do one ordinary, read-only thing. Returns what it did.

    Never raises: a decoy that fails is not a reason to abandon the real work it
    was interleaved with.
    """
    label, path = random.choice(DECOYS)
    try:
        if not await visit.goto(page, f"{config.BASE_URL}{path}"):
            return f"{label} (did not load)"
        await visit.dismiss_overlays(page, SEL["overlays"])
        await human.think()
        # Read a little of whatever is there, with a chance of glancing back up.
        for _ in range(random.randint(1, 3)):
            await visit.scroll_step(page)
        if random.random() < 0.4:
            await visit.scroll_back(page)
        await visit.dwell_action(page)
        return label
    except Exception as exc:  # noqa: BLE001
        return f"{label} ({type(exc).__name__})"


# ── Selector health ───────────────────────────────────────────────────


async def probe(page) -> dict:
    """Which selectors match on the page in front of us.

    The tool for the failure this module WILL eventually have: Meetup redeploys,
    a selector stops matching, and a run starts returning zero of everything.
    Point this at a group page or a profile and it says which keys are dead,
    instead of leaving the operator to guess from an empty result.
    """
    report: dict[str, int] = {}
    for key in SEL:
        try:
            report[key] = await page.locator(_sel(key)).count()
        except Exception:  # noqa: BLE001
            report[key] = -1          # -1 = the selector itself is invalid
    return {"url": page.url, "logged_in": await is_logged_in(page),
            "challenge": await hit_challenge(page), "matches": report}
