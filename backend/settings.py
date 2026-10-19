"""Operator-tunable settings, with defaults in one place.

These are the knobs that shape how the bot behaves on Meetup: how fast it
scrolls, how long it waits between actions, how many members a run may touch.
They are NOT credentials or infrastructure — those are .env, read in config.py.
The split matters: everything here is safe to change from the UI mid-run,
everything there needs a restart.

`DEFAULTS` is the schema. A stored value is only accepted if its key is a known
default and its value coerces to the same type, so a stale store.json from an
older build can't inject a key this build doesn't understand.
"""
from __future__ import annotations

from backend import activity, db

# Each entry: key -> (default, minimum, maximum, help text). Numeric ranges are
# enforced on save — a delay of 0 would drive Meetup's rate limiter straight into
# a block, and the UI shouldn't be the only thing preventing that.
DEFAULTS: dict[str, tuple] = {
    # ─── Pacing ───────────────────────────────────────────────────
    "action_delay_min_s": (3.0, 0.5, 120.0,
                           "Shortest pause between two actions"),
    "action_delay_max_s": (9.0, 0.5, 300.0,
                           "Longest pause between two actions"),
    "scroll_pause_s": (1.4, 0.2, 30.0,
                       "Pause after each scroll step while collecting"),
    "page_timeout_s": (30.0, 5.0, 180.0,
                       "How long to wait for a page before giving up"),
    # How long to wait after ticking "I am human". Generous on purpose: when
    # hCaptcha escalates to the image puzzle this is the window the operator has
    # to solve it by hand in the visible browser — the bot does not solve it.
    "captcha_wait_s": (90.0, 5.0, 600.0,
                       "Wait for the captcha to pass (you solve any puzzle)"),

    # ─── Limits ───────────────────────────────────────────────────
    "max_members_per_run": (200, 1, 5000,
                            "Members one scrape run may collect"),
    "max_messages_per_run": (25, 1, 500,
                             "Messages one campaign run may send"),
    "max_rsvps_per_run": (5, 1, 100,
                          "Events one run may RSVP to"),

    # ─── Parallelism ──────────────────────────────────────────────
    # How many recipients a campaign works at once — one browser tab each, taken
    # off the same queue. 5 is the default because it is the width the pipeline
    # has always used for profile tabs and it reads as a person with a handful
    # of tabs open. Raising it makes a run finish sooner and makes the session
    # look less like one: several composers submitting inside the same second
    # from one account is not something a hand does. The ceiling is a guard
    # rail, not a recommendation.
    "campaign_tabs": (5, 1, 20,
                      "Recipients a campaign processes at once — one tab each"),

    # ─── Getting unstuck ──────────────────────────────────────────
    # How long a scrape will watch an event produce NOTHING before giving up on
    # it and searching again from a different city. A stall is not rare: Meetup
    # stops serving the next page of a roster, a tab hangs on a fetch that never
    # returns, a list quietly ends without saying so. None of those raise, so
    # without a clock a sweep sits on one event for as long as the operator
    # leaves it running.
    #
    # 90s because a slow roster page genuinely can take a minute to hand over its
    # next twenty attendees, and cutting a working event short is the worse of
    # the two mistakes — the run has already paid for the page load.
    "scrape_stall_s": (90.0, 15.0, 1800.0,
                       "Give up on an event and search again if no new member "
                       "arrives for this long"),

    # ─── Batching ─────────────────────────────────────────────────
    # A campaign sends in runs with a real stop between them. Both numbers are
    # RANGES, sampled fresh for every batch: a fixed 10-then-2-minutes is a
    # metronome, and a metronome is the easiest thing in the world to spot in a
    # request log. The operator picks the range; the run picks the value.
    "batch_size_min": (5, 1, 500,
                       "Fewest messages in one batch before a pause"),
    "batch_size_max": (15, 1, 500,
                       "Most messages in one batch before a pause"),
    "batch_pause_min_s": (60.0, 5.0, 7200.0,
                          "Shortest wait between two batches"),
    "batch_pause_max_s": (180.0, 5.0, 7200.0,
                          "Longest wait between two batches"),

    # ─── Typing ───────────────────────────────────────────────────
    # The per-key model. Raising the speed past a real typist's rate is the
    # fastest way to undo everything typing.py does.
    "type_speed_ms": (120, 10, 500,
                      "Base delay per keystroke"),
    "type_jitter_ms": (60, 0, 400,
                       "Random extra per keystroke — the variance is the point"),

    # ─── Behaviour ────────────────────────────────────────────────
    "visit_profiles": (True, None, None,
                       "Open each member's profile to read their bio"),
    "human_typing": (True, None, None,
                     "Type key-by-key, with typos and corrections"),
    "os_level_input": (True, None, None,
                       "Use the real Windows cursor for the login button"),
    "decoy_activity": (True, None, None,
                       "Interleave ordinary browsing between messages"),
    "batch_campaigns": (True, None, None,
                        "Send in uneven batches with minutes between them"),
    "skip_used_members": (True, None, None,
                          "Never contact a member a campaign already used"),
    "stop_on_challenge": (True, None, None,
                          "Halt the run if Meetup shows a captcha or code prompt"),
    # OFF and the message is typed, the image attached, and the composer left
    # sitting there for a person to read and send. The whole run still walks
    # every recipient — it just stops one click short of each one, which is the
    # only way to watch what this thing would actually say before it says it.
    "click_send": (True, None, None,
                   "Click Send after writing the message (off = leave it typed)"),

    # ─── Where to search ──────────────────────────────────────────
    # The cities a scrape searches from. One is picked at RANDOM per run and
    # typed into Meetup's location box, and the first suggestion under it is
    # taken — see automation/meetup.set_location.
    #
    # Random rather than a fixed home city because that is the whole point of
    # the list: every run of this bot searching from the same place is one more
    # thing that ties the runs together. Comma-separated, so a city whose own
    # name contains a comma cannot be listed — none of these do.
    #
    # The default is the ten most populous urban areas in the world, which is a
    # starting point rather than a recommendation: an operator with a market
    # should type their own cities in here.
    "search_cities": ("Tokyo, Delhi, Shanghai, Dhaka, São Paulo, Cairo, "
                      "Mexico City, Beijing, Mumbai, Osaka", None, None,
                      "Cities a scrape searches from — one is picked at "
                      "random per run"),

    # ─── Links ────────────────────────────────────────────────────
    # The address a run opens to reach one member's chat. Stored as a template
    # so the operator owns it: Meetup has moved this URL before, and when it
    # moves again the fix is a line in this box rather than a new build. The
    # default is the composer the app has always used, so leaving it alone
    # changes nothing.
    #
    # Placeholders are filled per recipient — see automation/meetup.chat_link.
    "chat_link": ("https://www.meetup.com/messages/?new_convo=true"
                  "&member_id={id}&name={name}", None, None,
                  "Chat link opened per member — {id}/{username} is their id, "
                  "{name} their display name"),
}

# Which placeholders chat_link understands, in the order the help lists them.
# Here rather than in meetup.py because the Settings panel shows them and has no
# business importing the automation layer to do it.
CHAT_LINK_TOKENS = ("{id}", "{username}", "{name}")

# Text settings that are really a LIST of short entries. Stored as one
# comma-separated string — that is the whole value, and every reader still gets a
# plain string — but the panel gives them a row each with an X, because a list is
# what they are and editing one entry in the middle of a 60-character line means
# counting commas.
LIST_SETTINGS = ("search_cities",)

# Defaults that CHANGED between builds. A stored setting still sitting on an old
# default is moved to the current one on boot — without this, an install that had
# ever saved any setting kept the old value forever, because the stored copy wins
# over the default and it was never a choice anyone made. Deliberate values (ones
# that don't match the superseded default exactly) are untouched.
SUPERSEDED: dict[str, list] = {
    "scroll_pause_s": [0.6],          # too fast — tripped Meetup's rate limiting
    "action_delay_min_s": [1.0],      # ditto
    # The composer route this app opened before the new-conversation link. An
    # operator who pressed Save while that was the default has it stored, and
    # without this line they keep it forever — a value nobody chose.
    "chat_link": ["https://www.meetup.com/messages/new/?member={id}"],
}


def _coerce(key: str, value):
    """A submitted value as the type its default says it should be, clamped to
    range. Returns None when it can't be used, so the caller keeps the default."""
    default, low, high = DEFAULTS[key][0], DEFAULTS[key][1], DEFAULTS[key][2]
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, str):
        # "" is a real answer — the operator clearing the box — and the callers
        # read an empty template as "use the built-in route", so it is stored
        # rather than dropped back to the default.
        return "" if value is None else str(value).strip()
    try:
        number = type(default)(value)
    except (TypeError, ValueError):
        return None
    if low is not None:
        number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


def defaults() -> dict:
    return {key: spec[0] for key, spec in DEFAULTS.items()}


def cities(values: dict | None = None) -> list[str]:
    """The search cities, as a list. Blanks and duplicates dropped.

    A comma-separated box rather than a table, because the whole thing is one
    line of text an operator types their own markets into — and a list of ten
    short names is not worth a screen of its own.
    """
    raw = (values or current()).get("search_cities") or ""
    out: list[str] = []
    for part in str(raw).split(","):
        city = part.strip()
        if city and city.lower() not in {c.lower() for c in out}:
            out.append(city)
    return out


def schema() -> list[dict]:
    """What the Settings panel renders itself from — so adding a knob here is the
    only change needed to get it on screen."""
    return [
        {"key": key, "default": spec[0], "min": spec[1], "max": spec[2],
         "help": spec[3], "type": _kind(spec[0], key),
         # Only a text setting has any, and only the panel reads it — it puts
         # them under the box so the placeholders are visible where they are
         # typed rather than in a docstring nobody opens.
         "tokens": list(CHAT_LINK_TOKENS) if key == "chat_link" else []}
        for key, spec in DEFAULTS.items()
    ]


def _kind(default, key: str = "") -> str:
    if isinstance(default, bool):
        return "bool"
    if not isinstance(default, str):
        return "number"
    return "list" if key in LIST_SETTINGS else "text"


def current() -> dict:
    """Defaults with any stored overrides applied. Unknown stored keys are
    dropped, so a value this build no longer understands can't leak through."""
    values = defaults()
    for key, raw in (db.get_settings() or {}).items():
        if key not in DEFAULTS:
            continue
        coerced = _coerce(key, raw)
        if coerced is not None:
            values[key] = coerced
    return _reconcile(values)


# Every (minimum, maximum) pair. A value is sampled from each of these, so a min
# above its max would make the sampler raise instead of pausing.
RANGES: tuple[tuple[str, str], ...] = (
    ("action_delay_min_s", "action_delay_max_s"),
    ("batch_size_min", "batch_size_max"),
    ("batch_pause_min_s", "batch_pause_max_s"),
)


def _reconcile(values: dict) -> dict:
    """Fix combinations that are individually valid but nonsense together.

    A minimum above its maximum lifts the MAXIMUM to meet it, rather than the
    operator's minimum being quietly ignored: somebody who types 30 into a "at
    least" box meant at least 30, and silently sending twice as fast as they
    asked is the worse of the two ways to be wrong.
    """
    for low, high in RANGES:
        if values[low] > values[high]:
            values[high] = values[low]
    return values


def update(changes: dict) -> dict:
    """Merge a partial update into the stored settings and return the full set."""
    stored = {k: v for k, v in (db.get_settings() or {}).items() if k in DEFAULTS}
    for key, raw in (changes or {}).items():
        if key not in DEFAULTS:
            continue
        coerced = _coerce(key, raw)
        if coerced is not None:
            stored[key] = coerced
    db.save_settings(stored)
    values = current()
    activity.log("config", f"Settings updated ({len(changes or {})} change(s))", "ok")
    return values


def reset() -> dict:
    db.save_settings({})
    activity.log("config", "Settings reset to defaults", "ok")
    return defaults()


def migrate_stale_defaults() -> dict:
    """Move any stored setting still on a SUPERSEDED default to the current one.

    Returns {key: (old, new)} for whatever moved, so boot can log it — a silent
    change to how fast the bot runs is exactly the kind of thing an operator
    needs told about.
    """
    stored = {k: v for k, v in (db.get_settings() or {}).items() if k in DEFAULTS}
    moved: dict[str, tuple] = {}
    for key, old_defaults in SUPERSEDED.items():
        if key not in stored:
            continue
        for old in old_defaults:
            if stored[key] == old:
                stored[key] = DEFAULTS[key][0]
                moved[key] = (old, DEFAULTS[key][0])
                break
    if moved:
        db.save_settings(stored)
    return moved
