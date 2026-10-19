"""Every number that decides whether a run reads as a person or a script.

One module, because these constants only mean anything relative to each other.
A 40ms beat between searches is not "fast" in isolation — it is fast next to the
2.2–4.0s a person takes to look at a page of results, and scattering the two
across different files is how they drift apart until one of them is wrong.

The rule behind all of it: a bot is not caught by any single action looking
wrong, it is caught by the DISTRIBUTION being wrong. A human's inter-action
gaps are noisy and long-tailed; a script's are a spike at one value. So almost
nothing here is a constant — it is a range, sampled fresh each time.

    await beat(READ_DWELL_S)          sleep a sampled duration
    jitter(box)                       a point inside a rect, never its centre
    plan_typing("hello")              keystrokes with typos and corrections
    batches(rows)                     a recipient list cut into uneven runs
    Backoff()                         5s doubling to 60s when a surface dries up

Nothing in here does I/O or touches Playwright. It is pure, so the pacing can be
tested without a browser — see the smoke test in the repo's scratchpad.
"""
from __future__ import annotations

import asyncio
import random
import string
from dataclasses import dataclass

# ── 1. Browser launch ────────────────────────────────────────────────
# Chrome's own flags that announce automation. Passed to `ignore_default_args`
# so Playwright does NOT add them: --enable-automation is what paints the
# "controlled by automated test software" infobar and sets several JS tells,
# and --no-sandbox is a combination almost no real desktop Chrome runs with.
AUTOMATION_ARGS = ["--enable-automation", "--no-sandbox"]

# Added on purpose. AutomationControlled is the blink feature behind
# navigator.webdriver === true; disabling it makes the property undefined,
# which is what a real browser reports.
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--start-maximized",
    "--no-default-browser-check",
    "--no-first-run",
    "--disable-features=Translate,MediaRouter,OptimizationHints",
]

# ── 2. Pointer ───────────────────────────────────────────────────────
# Travel time for one pointer move. Long enough to be a hand, short enough that
# a run of 200 clicks doesn't take an extra three minutes.
POINTER_TRAVEL_S = (0.25, 0.75)

# A click lands at a random offset inside the target, up to a sixth of its size
# from centre. Dead-centre clicks are the single easiest synthetic-input tell:
# a real pointer essentially never hits the exact middle of a button twice.
AIM_JITTER = 1 / 6

# Pauses either side of a real click.
PRE_CLICK_S = (0.15, 0.35)
POST_CLICK_S = (0.40, 0.80)
PRE_ENTER_S = (0.20, 0.50)

# Where in the omnibox the pointer lands — left third, so it looks like someone
# aiming at the start of the text rather than at a computed midpoint.
OMNIBOX_X_FRACTION = (0.06, 0.33)

# ── 3. Typing ────────────────────────────────────────────────────────
TYPE_SPEED_MS = 120        # base per-character delay
TYPE_JITTER_MS = 60        # uniform extra per character

# One deliberate typo roughly every N characters, then 1–3 backspaces and a
# retype. Real typing is not error-free, and a 400-character message entered
# with zero corrections is itself an anomaly.
TYPO_EVERY = 8
TYPO_BACKSPACE = (1, 3)
TYPO_NOTICE_S = (0.12, 0.40)   # the beat before you see the mistake

# Login form: gaps between fields, and before submitting.
FIELD_GAP_S = (0.30, 0.80)
PRE_SUBMIT_S = (0.30, 0.90)

# Typing a search seed is faster than typing prose — you know what you're after.
SEARCH_TYPE_MS = (55, 120)

# ── 4. Scrolling and reading ─────────────────────────────────────────
# One scroll step, as a fraction of the viewport. Never a whole screen: that
# skips content a reader would have passed, and lands on suspiciously round
# scroll offsets.
SCROLL_STEP_FRACTION = (0.80, 0.95)
SCROLL_PAUSE_S = (0.40, 0.90)

# Skimming, not reading. Rolling a category, opening its first event and running
# down its attendee list is read-only browsing that produces no artefact on
# anyone else's screen, and a person skimming a guest list for names does it at
# this speed — the careful pacing above is for the actions with consequences.
# The step is also bigger, because skimming skips.
SWIFT_SCROLL_STEP_FRACTION = (0.95, 1.15)
SWIFT_SCROLL_PAUSE_S = (0.10, 0.28)

# Every N forward steps, flick back up 1–2 steps. People re-read; an
# infinite-scroll session that only ever advances is not one of them.
BACK_SCROLL_EVERY = 5
BACK_SCROLL_STEPS = (1, 2)

# Bio mode — walking profiles instead of skimming a list — is deliberately
# slower: a smaller step, one profile per step, and a real dwell on each.
BIO_STEP_MULTIPLIER = 0.35
BIO_DWELL_S = (1.1, 2.4)
BIO_BACK_SCROLL_EVERY = (20, 30)

# Take a few rows at a time off the visible list rather than grabbing a whole
# screenful and burst-opening every one of them.
RESULTS_RELEASE_PER_PASS = (2, 4)

# How long a menu or action surface stays open before the next move.
MENU_DWELL_S = (0.45, 1.2)
ACTION_DWELL_S = (0.6, 1.6)

# ── 5. Search ────────────────────────────────────────────────────────
BETWEEN_SEARCH_S = (2.2, 4.0)
PRE_TYPE_BEAT_S = (0.10, 0.25)
RESULTS_DWELL_S = (0.30, 0.70)

# ── 6. Campaign volume ───────────────────────────────────────────────
BATCH_SIZE = (5, 15)               # recipients per uninterrupted run
BETWEEN_BATCH_S = (60, 180)        # 1–3 minutes between runs
DECOY_EVERY = (3, 10)              # rows between one decoy activity

SCRAPE_BATCH = (10, 20)            # handles collected before a short stop
SCRAPE_BATCH_PAUSE_S = (1.0, 3.0)

# Alternate which surface a scrape works, on a clock rather than at random —
# a session that only ever uses one surface has an obviously narrow footprint.
SURFACE_ROTATE_S = {"group": 300, "search": 120}

BACKOFF_START_S = 5.0
BACKOFF_MAX_S = 60.0


# ── Sampling helpers ─────────────────────────────────────────────────


def pick(span: tuple[float, float]) -> float:
    """A uniform sample from an (low, high) range."""
    low, high = span
    return random.uniform(low, high)


def pick_int(span: tuple[int, int]) -> int:
    low, high = span
    return random.randint(int(low), int(high))


async def beat(span: tuple[float, float]) -> float:
    """Sleep a sampled duration from `span`. The unit of pacing everywhere."""
    seconds = pick(span)
    await asyncio.sleep(seconds)
    return seconds


def jitter(box: dict, fraction: float = AIM_JITTER) -> tuple[float, float]:
    """A point inside `box` (Playwright's {x,y,width,height}), off centre.

    Offset by up to `fraction` of each dimension. See AIM_JITTER for why the
    exact centre is the one point worth never hitting.
    """
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    return (cx + random.uniform(-fraction, fraction) * box["width"],
            cy + random.uniform(-fraction, fraction) * box["height"])


def ease_out_quad(t: float) -> float:
    """Decelerating easing. A hand arrives at a target slowing down; linear
    interpolation is the motion of a machine and reads as one."""
    return 1 - (1 - t) * (1 - t)


# ── Typing plan ──────────────────────────────────────────────────────

# A plausible wrong key is a physical neighbour. A typo of "q" for "m" reads as
# noise; "n" for "m" reads as a hand on a keyboard.
_NEIGHBOURS = {
    "a": "sq", "b": "vn", "c": "xv", "d": "sf", "e": "wr", "f": "dg", "g": "fh",
    "h": "gj", "i": "uo", "j": "hk", "k": "jl", "l": "k", "m": "n", "n": "bm",
    "o": "ip", "p": "o", "q": "wa", "r": "et", "s": "ad", "t": "ry", "u": "yi",
    "v": "cb", "w": "qe", "x": "zc", "y": "tu", "z": "x",
}


def slip(char: str) -> str:
    """A believable wrong key for `char`."""
    options = _NEIGHBOURS.get(char.lower())
    wrong = random.choice(options) if options else random.choice(string.ascii_lowercase)
    return wrong.upper() if char.isupper() else wrong


@dataclass
class Keystroke:
    """One step of a typing plan.

    `kind` is "type" (send text), "back" (n backspaces) or "pause". Building the
    plan as data rather than as a loop of awaits is what lets it be unit-tested:
    you can assert that a 400-character message ends up with the right final
    text without driving a browser to find out.
    """
    kind: str
    text: str = ""
    count: int = 0
    delay_s: float = 0.0


def plan_typing(text: str, *, speed_ms: int = TYPE_SPEED_MS,
                jitter_ms: int = TYPE_JITTER_MS,
                typos: bool = True) -> list[Keystroke]:
    """Turn `text` into keystrokes with human timing, typos and corrections.

    The plan always *ends* with `text` correct — a correction retypes what it
    removed. Verified by the round-trip assertion in the smoke test.
    """
    plan: list[Keystroke] = []
    since_typo = 0

    def delay() -> float:
        return (speed_ms + random.uniform(0, jitter_ms)) / 1000.0

    i = 0
    chars = list(text or "")
    while i < len(chars):
        # Occasionally fumble: type some wrong characters, notice, fix them.
        if (typos and since_typo >= TYPO_EVERY and chars[i].isalpha()
                and random.random() < 0.5):
            run = min(pick_int(TYPO_BACKSPACE), len(chars) - i)
            wrong = "".join(slip(c) for c in chars[i:i + run])
            plan.append(Keystroke("type", text=wrong, delay_s=delay()))
            plan.append(Keystroke("pause", delay_s=pick(TYPO_NOTICE_S)))
            plan.append(Keystroke("back", count=run, delay_s=delay()))
            since_typo = 0
            continue                       # retype the same run correctly

        plan.append(Keystroke("type", text=chars[i], delay_s=delay()))
        since_typo += 1
        i += 1

        # People stall at boundaries, not at even intervals.
        if chars[i - 1] in ".!?" and i < len(chars):
            plan.append(Keystroke("pause", delay_s=pick((0.25, 0.9))))
        elif chars[i - 1] == " ":
            plan.append(Keystroke("pause", delay_s=pick((0.05, 0.22))))

    return plan


def plan_result(plan: list[Keystroke]) -> str:
    """Replay a plan as text. What the field ends up containing — used by the
    tests to prove corrections actually correct."""
    out: list[str] = []
    for step in plan:
        if step.kind == "type":
            out.extend(step.text)
        elif step.kind == "back":
            del out[len(out) - step.count:]
    return "".join(out)


# ── Search seeds ─────────────────────────────────────────────────────
# Nine shapes, so queries don't all look generated from one template. A search
# log of forty two-letter lowercase seeds is a fingerprint on its own.

def seed() -> str:
    """A random search seed — one of nine shapes, chosen at random."""
    lower, digits = string.ascii_lowercase, string.digits
    shapes = (
        lambda: "".join(random.choices(lower, k=2)),
        lambda: "".join(random.choices(lower, k=3)),
        lambda: "".join(random.choices(lower, k=4)),
        lambda: random.choice(lower) + random.choice(digits),
        lambda: "".join(random.choices(digits, k=2)),
        lambda: random.choice(lower) + random.choice("._-"),
        lambda: "".join(random.choices(lower, k=2)) + random.choice(digits),
        lambda: random.choice(digits) + "".join(random.choices(lower, k=2)),
        lambda: "".join(random.choices(lower + digits, k=random.randint(2, 5))),
    )
    return random.choice(shapes)()


# ── Volume shaping ───────────────────────────────────────────────────


def shuffled(rows: list) -> list:
    """A copy of `rows` in random order.

    Sending down a stored list in order means contacting members in the order
    they were scraped — which is roster order, i.e. the order they appear on one
    group's page. That correlation is visible from the receiving end.
    """
    out = list(rows)
    random.shuffle(out)
    return out


def batches(rows: list, span: tuple[int, int] = BATCH_SIZE) -> list[list]:
    """Cut `rows` into uneven runs. Even slices are their own signature."""
    out: list[list] = []
    i = 0
    while i < len(rows):
        take = pick_int(span)
        out.append(rows[i:i + take])
        i += take
    return out


def should_decoy(rows_since: int) -> bool:
    """Whether to interleave an unrelated activity now.

    A session that is purely outreach has a request mix no real account
    produces. Every few rows the bot goes and does something else instead.
    """
    return rows_since >= pick_int(DECOY_EVERY)


class Backoff:
    """Exponential backoff for a surface that has gone quiet.

    A scrape that finds nothing usually means a rate-limit window, not an empty
    site. Retrying hard is what turns a soft limit into a block, so each empty
    pass doubles the wait up to a ceiling; any success resets it.
    """

    def __init__(self, start: float = BACKOFF_START_S, ceiling: float = BACKOFF_MAX_S):
        self.start = start
        self.ceiling = ceiling
        self.current = start
        self.misses = 0

    def reset(self) -> None:
        self.current = self.start
        self.misses = 0

    def fail(self) -> float:
        """Record an empty pass and return how long to wait."""
        wait = self.current
        self.misses += 1
        self.current = min(self.current * 2, self.ceiling)
        # Jittered, so several accounts backing off together don't sync up into
        # one synchronised retry wave — which is more visible than either alone.
        return wait * random.uniform(0.85, 1.15)

    async def sleep(self) -> float:
        seconds = self.fail()
        await asyncio.sleep(seconds)
        return seconds


class Surface:
    """Which collection surface is in use, rotated on a clock.

    Alternating group rosters and search on a timer (rather than at random)
    keeps the session's footprint broad without the switch itself looking
    random — real use has runs of one activity, not a coin flip per action.
    """

    def __init__(self, order=("group", "search"), clock=None):
        self.order = list(order)
        self.clock = dict(clock or SURFACE_ROTATE_S)
        self.index = 0
        self.elapsed = 0.0

    @property
    def current(self) -> str:
        return self.order[self.index % len(self.order)]

    def advance(self, seconds: float) -> str:
        """Add elapsed time; rotate when this surface's slot is up."""
        self.elapsed += seconds
        if self.elapsed >= self.clock.get(self.current, 300):
            self.index += 1
            self.elapsed = 0.0
        return self.current
