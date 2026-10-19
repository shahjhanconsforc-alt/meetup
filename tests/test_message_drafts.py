"""The message drafts: what is stored, and which of them a run may send.

The panel shows them as a strip of tabs — they are ALTERNATIVES the sender
rotates between, not a sequence — and each one can be renamed, turned off or
deleted. Only two of those need defending here: what survives a round trip
through storage, and what a disabled draft does to a run.

Storage is the in-memory one from conftest, so these never touch the operator's
bucket.
"""
from __future__ import annotations

import asyncio

import pytest

from backend import db
from backend.automation import campaign
from services.storage import message_storage as msg


class Ctl:
    """A control.Run stand-in: counts, never stops."""

    stopping = False

    def __init__(self):
        self.total = 0
        self.done = 0
        self.meta: dict = {}

    async def checkpoint(self, label=None):
        return True

    def advance(self, n, label=None):
        self.done += n


# ── What storage keeps ───────────────────────────────────────────────────


def test_a_draft_keeps_its_name_and_its_off_switch():
    """Rename and disable are the two edits that are not the text itself, and
    both used to be dropped on the way through: the record was {label, text}
    and everything else fell off the end of a save."""
    msg.push([{"label": "opener", "text": "hello", "disabled": True}])

    stored = msg.load()

    assert stored == [{"label": "opener", "text": "hello", "disabled": True}]


def test_a_draft_with_no_name_is_given_one():
    msg.push([{"text": "hello"}])

    assert msg.load()[0]["label"] == "msg1"


def test_a_bare_string_still_loads():
    """An early build stored plain strings. One of those is an ENABLED draft —
    a missing flag is not a draft somebody turned off."""
    msg.push(["just text"])

    stored = msg.load()

    assert stored[0]["text"] == "just text"
    assert stored[0]["disabled"] is False


def test_an_empty_draft_is_not_stored():
    """A draft IS its text. A tab opened and never typed into is not a message
    the sender should rotate onto."""
    msg.push([{"label": "blank", "text": "   "}, {"label": "real", "text": "hi"}])

    assert [d["label"] for d in msg.load()] == ["real"]


# ── What a run will send ─────────────────────────────────────────────────


def _outreach(**kwargs):
    return asyncio.run(campaign.outreach(Ctl(), dry_run=True, **kwargs))


def test_a_disabled_draft_is_never_sent(monkeypatch):
    """Turned off is not deleted: it stays in the store and stays on the strip,
    and the rotation skips it."""
    seen = []

    def _targets(*args, **kwargs):
        return [{"username": "someone", "name": "Someone"}]

    monkeypatch.setattr(campaign, "pick_targets", _targets)
    db.save_message_drafts([
        {"label": "off", "text": "NEVER", "disabled": True},
        {"label": "on", "text": "ALWAYS", "disabled": False},
    ])

    result = _outreach()

    seen = " ".join(p.get("text", "") for p in result.get("previews", []))
    assert "ALWAYS" in seen
    assert "NEVER" not in seen


def test_a_set_with_every_draft_disabled_says_so(monkeypatch):
    """Different from having written none, and the operator needs to be told
    which — one is a thing to write, the other a switch to flip."""
    monkeypatch.setattr(campaign, "pick_targets",
                        lambda *a, **k: [{"username": "someone"}])
    db.save_message_drafts([{"label": "off", "text": "hi", "disabled": True}])

    result = _outreach()

    assert result["sent"] == 0
    assert "disabled" in result["detail"]


def test_no_drafts_at_all_still_reads_as_none_written(monkeypatch):
    monkeypatch.setattr(campaign, "pick_targets",
                        lambda *a, **k: [{"username": "someone"}])
    db.save_message_drafts([])

    result = _outreach()

    assert result["sent"] == 0
    assert "no message drafts" in result["detail"]


# ── Skipping the messages step ───────────────────────────────────────────
# The panel lets one of Messages / Media be skipped, never both. Skipping
# Messages arrives here as an empty draft list, which is a picture-only run.


def test_an_empty_draft_list_is_a_picture_only_run(monkeypatch):
    """Text is optional when there is a picture to carry the message."""
    monkeypatch.setattr(campaign, "pick_targets",
                        lambda *a, **k: [{"username": "someone"}])
    db.save_message_drafts([{"label": "stored", "text": "STORED"}])

    result = _outreach(drafts=[], images=["pic.png"])

    assert [p["image"] for p in result["previews"]] == ["pic.png"]
    assert [p["text"] for p in result["previews"]] == [""]


def test_skipping_messages_does_not_fall_back_to_the_stored_drafts(monkeypatch):
    """THE trap in this: `drafts or db.get_message_drafts()` reads an empty list
    as "none given" and sends the bucket — which is the very thing the operator
    skipped, to every recipient, silently."""
    monkeypatch.setattr(campaign, "pick_targets",
                        lambda *a, **k: [{"username": "someone"}])
    db.save_message_drafts([{"label": "stored", "text": "STORED"}])

    result = _outreach(drafts=[], images=["pic.png"])

    assert "STORED" not in " ".join(p["text"] for p in result["previews"])


def test_a_run_with_neither_text_nor_picture_is_refused(monkeypatch):
    """Both skipped is not a thing the panel allows, and the run says so rather
    than sending an empty message to everybody."""
    monkeypatch.setattr(campaign, "pick_targets",
                        lambda *a, **k: [{"username": "someone"}])
    db.save_message_drafts([{"label": "stored", "text": "STORED"}])

    result = _outreach(drafts=[], images=[])

    assert result["sent"] == 0
    assert "no image" in result["detail"]


# ── Click Send off ───────────────────────────────────────────────────────
# The run does everything except the last press. What it must NOT do is count
# those as sent, or mark the members contacted — nobody has been contacted
# until somebody presses the button.


class _Page:
    """The window's page, and the tabs a run opens off it.

    A real run now opens one tab per recipient at that member's chat link, so
    the double has to carry a context that can hand them out — and to remember
    which of them were closed, since a composer left open is how a Click-Send-off
    run delivers its work.
    """

    url = ""

    def __init__(self, context=None):
        self.context = context
        self.closed = False

    async def close(self):
        self.closed = True


class _Context:
    def __init__(self):
        self.pages: list[_Page] = []

    async def new_page(self):
        page = _Page(self)
        self.pages.append(page)
        return page


async def _noop_page(**kwargs):
    return _Page(_Context())


def _run_with_send(monkeypatch, *, clicking: bool, tabs: list | None = None,
                   ctl: Ctl | None = None, drafts: list | None = None,
                   images: list | None = None,
                   targets: list | None = None, send=None, width: int = 1,
                   batching: bool = False, account_ids: list | None = None,
                   settings: dict | None = None):
    """One outreach run of two members, with the browser and the PACING stubbed.

    The pacing matters as much as the browser here: a real run puts a human
    pause between members and minutes between batches, and a test that waits
    those out is a test nobody runs.

    Pass `tabs` a list to collect the per-recipient tabs the run opened, and
    `ctl` your own control to read the live table off its `meta`. `settings`
    merges into the stubbed settings — patching app_settings.current from a test
    does not work, because this helper replaces it after the test's own patch.
    """
    from backend.automation import meetup

    context = _Context()

    async def _open(**kwargs):
        return _Page(context)

    async def _send(page, handle, text, image=None, name="", ctl=None):
        return ({"ok": True, "detail": f"Messaged {handle}"} if clicking
                else {"ok": True, "sent": False, "detail": "Typed and left"})

    async def _instant(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(campaign, "pick_targets",
                        lambda *a, **k: [{"username": "someone"}])
    monkeypatch.setattr(meetup, "send_message", send or _send)
    monkeypatch.setattr(campaign.browser, "open_page", _open)
    monkeypatch.setattr(campaign.human, "pause", _instant)
    monkeypatch.setattr(campaign.behavior, "beat", _instant)
    monkeypatch.setattr(campaign.app_settings, "current", lambda: {
        **campaign.app_settings.defaults(),
        "batch_campaigns": batching, "decoy_activity": False,
        "campaign_tabs": width,
        **(settings or {}),
    })
    db.save_message_drafts(drafts or [{"label": "one", "text": "hello"}])
    result = asyncio.run(campaign.outreach(
        ctl or Ctl(), images=images, account_ids=account_ids,
        targets=targets or [{"username": "u1"}, {"username": "u2"}]))
    if tabs is not None:
        tabs.extend(context.pages)
    return result


def test_a_message_that_was_only_typed_is_not_counted_as_sent(monkeypatch):
    result = _run_with_send(monkeypatch, clicking=False)

    assert result["sent"] == 0
    assert result["typed"] == 2
    assert "Click Send is off" in result["detail"]


def test_a_message_that_was_only_typed_does_not_mark_the_member(monkeypatch):
    """used_count is "we have contacted this person". A composer with text in
    it has contacted nobody, and skipping them next run would be wrong."""
    db.record_scraped_members(["u1", "u2"])
    _run_with_send(monkeypatch, clicking=False)

    used = {r["username"]: r["used_count"] for r in db.list_scraped_members()}
    assert used.get("u1", 0) == 0
    assert used.get("u2", 0) == 0


def test_with_click_send_on_they_are_sent_and_marked(monkeypatch):
    db.record_scraped_members(["u1", "u2"])
    result = _run_with_send(monkeypatch, clicking=True)

    assert result["sent"] == 2
    assert result["typed"] == 0
    used = {r["username"]: r["used_count"] for r in db.list_scraped_members()}
    assert used["u1"] == 1 and used["u2"] == 1


# ── Working several recipients at once ───────────────────────────────────
# Settings → campaign_tabs. A pool of workers off one queue, a tab each: the
# width decides how many are in flight, not how the queue is cut up.


def _run_wide(monkeypatch, *, width, count=8, watcher=None, **kwargs):
    """A run of `count` recipients at `width` at a time. `watcher` is called with
    the live concurrency each time a send starts and finishes."""
    live = {"now": 0, "peak": 0}

    async def _send(page, handle, text, image=None, name="", ctl=None):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        if watcher:
            watcher(live)
        await asyncio.sleep(0)          # yield, so the others get to start
        live["now"] -= 1
        return {"ok": True, "detail": f"Messaged {handle}"}

    _run_with_send(monkeypatch, clicking=True, width=width, send=_send,
                   targets=[{"username": f"u{i}"} for i in range(count)],
                   **kwargs)
    return live


def test_five_recipients_are_worked_at_once_by_default():
    """The default the operator asked for, read straight off the schema."""
    assert campaign.app_settings.defaults()["campaign_tabs"] == 5


def test_the_setting_decides_how_many_run_at_once(monkeypatch):
    assert _run_wide(monkeypatch, width=4)["peak"] == 4


def test_raising_it_to_ten_really_runs_ten(monkeypatch):
    """The number the operator types is the number of tabs in flight — not a
    hint that some other cap quietly overrides."""
    assert _run_wide(monkeypatch, width=10, count=20)["peak"] == 10


def test_one_at_a_time_is_still_one_at_a_time(monkeypatch):
    assert _run_wide(monkeypatch, width=1)["peak"] == 1


def test_the_pool_never_outnumbers_the_queue(monkeypatch):
    """Ten workers over three recipients is seven that open nothing."""
    assert _run_wide(monkeypatch, width=10, count=3)["peak"] == 3


def test_every_recipient_is_taken_exactly_once(monkeypatch):
    """The whole risk of a shared queue: two workers taking the same row would
    message somebody twice, and there is no unsend."""
    seen = []

    async def _send(page, handle, text, image=None, name="", ctl=None):
        seen.append(handle)
        await asyncio.sleep(0)
        return {"ok": True, "detail": "ok"}

    ctl = Ctl()
    _run_with_send(monkeypatch, clicking=True, width=6, send=_send, ctl=ctl,
                   targets=[{"username": f"u{i}"} for i in range(15)])

    assert sorted(seen) == sorted(f"u{i}" for i in range(15))
    assert len(seen) == len(set(seen))
    assert {r["status"] for r in ctl.meta["members"]} == {"sent"}


def test_the_width_rides_on_the_run_for_the_panel(monkeypatch):
    """The live board shows more than one row Processing at once, which reads as
    a bug unless the header has said how many are meant to be."""
    ctl = Ctl()
    _run_with_send(monkeypatch, clicking=True, width=7, ctl=ctl,
                   targets=[{"username": f"u{i}"} for i in range(9)])

    assert ctl.meta["tabs"] == 7


def test_a_stop_during_a_batch_pause_does_not_wait_it_out(monkeypatch):
    """The between-batch pause is one to three MINUTES. Slept in one go, an End
    pressed a second into it sat there until the sleep ran out — a button that
    visibly did nothing."""
    slept = []

    async def _sleep(seconds):
        slept.append(seconds)

    class Stopping:
        stopping = True

    monkeypatch.setattr(campaign.asyncio, "sleep", _sleep)
    waited = asyncio.run(campaign._quiet(Stopping(), (60, 180)))

    assert waited == 0
    assert slept == []


def test_a_wait_nobody_stopped_is_slept_in_full(monkeypatch):
    slept = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(campaign.asyncio, "sleep", _sleep)
    waited = asyncio.run(campaign._quiet(Ctl(), (2, 2)))

    assert waited == pytest.approx(2)
    # In slices, not one long sleep — that IS what makes it interruptible.
    assert len(slept) > 1 and sum(slept) == pytest.approx(2)


def test_a_paused_run_holds_every_worker(monkeypatch):
    """Pause has to stop the whole pool, not just the worker that noticed. It
    already does — checkpoint blocks — but with several workers in flight that
    is worth pinning."""
    from backend import control

    ctl = control.Run("outreach")
    started = []

    async def _send(page, handle, text, image=None, name="", ctl=None):
        started.append(handle)
        if len(started) == 2:
            ctl.pause()                      # mid-flight, as the operator would
        await asyncio.sleep(0)
        return {"ok": True, "detail": "ok"}

    async def _drive():
        run = asyncio.ensure_future(_outreach_with(monkeypatch, ctl, _send, 12))
        # Let the pool get going and hit the pause.
        for _ in range(50):
            await asyncio.sleep(0)
        assert ctl.status == "paused"
        held = len(started)
        for _ in range(50):
            await asyncio.sleep(0)
        # Nothing new was picked up while paused — the in-flight ones finished
        # and every worker is sitting on its checkpoint.
        assert len(started) == held
        ctl.resume()
        return await run

    result = asyncio.run(_drive())
    assert result["sent"] == 12          # and it carries on where it left off


async def _outreach_with(monkeypatch, ctl, send, count):
    """One outreach run at width 4, wired to `send`. Async so a test can drive
    the pool and the control at the same time."""
    from backend.automation import meetup

    context = _Context()

    async def _open(**kwargs):
        return _Page(context)

    async def _instant(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(meetup, "send_message", send)
    monkeypatch.setattr(campaign.browser, "open_page", _open)
    monkeypatch.setattr(campaign.human, "pause", _instant)
    monkeypatch.setattr(campaign.behavior, "beat", _instant)
    monkeypatch.setattr(campaign.app_settings, "current", lambda: {
        **campaign.app_settings.defaults(), "campaign_tabs": 4,
        "batch_campaigns": False, "decoy_activity": False})
    db.save_message_drafts([{"label": "one", "text": "hello"}])
    return await campaign.outreach(
        ctl, targets=[{"username": f"u{i}"} for i in range(count)])


def test_ending_a_paused_run_drops_the_rest(monkeypatch):
    """Continue or end — this is the end half. What was sent stays sent, and
    what was never reached stays Pending rather than being called failed."""
    from backend import control

    ctl = control.Run("outreach")

    async def _send(page, handle, text, image=None, name="", ctl=None):
        if ctl.done >= 3:
            ctl.pause()
        await asyncio.sleep(0)
        return {"ok": True, "detail": "ok"}

    async def _drive():
        run = asyncio.ensure_future(_outreach_with(monkeypatch, ctl, _send, 40))
        for _ in range(200):
            await asyncio.sleep(0)
            if ctl.status == "paused":
                break
        assert ctl.status == "paused"
        ctl.stop()                        # End campaign, from the paused state
        return await run

    result = asyncio.run(_drive())

    assert result["sent"] < 40
    assert any(r["status"] == "pending" for r in ctl.meta["members"])
    # Not failed. Nobody tried to message them.
    assert not any(r["status"] == "failed" for r in ctl.meta["members"])


def test_a_stop_ends_every_worker(monkeypatch):
    """Stopping has to halt the pool, not just the worker that noticed."""
    class StopAfter(Ctl):
        def __init__(self, after):
            super().__init__()
            self.after = after
            self.seen = 0

        async def checkpoint(self, label=None):
            self.seen += 1
            return self.seen <= self.after

    ctl = StopAfter(4)
    result = _run_with_send(monkeypatch, clicking=True, width=4, ctl=ctl,
                            targets=[{"username": f"u{i}"} for i in range(30)])

    assert result["sent"] <= 4
    # The rows never reached stay Pending — a stop is not a failure, and calling
    # it one would misreport what happened to them.
    assert any(r["status"] == "pending" for r in ctl.meta["members"])


def test_the_width_is_clamped_to_the_ceiling(monkeypatch):
    monkeypatch.setattr(campaign.app_settings, "current", lambda: {
        **campaign.app_settings.defaults(), "campaign_tabs": 500})
    assert campaign.campaign_width() == campaign.CAMPAIGN_TABS_MAX


def test_a_nonsense_width_falls_back_rather_than_sending_nothing(monkeypatch):
    """A width of 0 would start no workers and finish instantly, reporting a
    campaign that sent nothing as if it had run."""
    for bad in (0, -3, "", None, "lots"):
        monkeypatch.setattr(campaign.app_settings, "current", lambda v=bad: {
            **campaign.app_settings.defaults(), "campaign_tabs": v})
        assert campaign.campaign_width() >= 1


def test_a_dry_run_stays_single_file(monkeypatch):
    """Nothing is opened and nothing is paced, so parallelism there would only
    reorder a list of previews."""
    from backend.automation import meetup

    monkeypatch.setattr(campaign, "pick_targets", lambda *a, **k: [])
    monkeypatch.setattr(campaign.app_settings, "current", lambda: {
        **campaign.app_settings.defaults(), "campaign_tabs": 10,
        "batch_campaigns": False, "decoy_activity": False})
    db.save_message_drafts([{"label": "one", "text": "hello"}])

    ctl = Ctl()
    result = asyncio.run(campaign.outreach(
        ctl, targets=[{"username": f"u{i}"} for i in range(4)], dry_run=True))

    assert result["planned"] == 4
    assert ctl.meta["tabs"] == 1
    assert [p["username"] for p in result["previews"]] == \
        [r["username"] for r in ctl.meta["members"]]


# ── Batching, and the ranges it rolls from ───────────────────────────────
# A campaign sends in runs with a real stop between them. Both the size of a run
# and the length of the stop are RANGES the operator sets, sampled fresh every
# time: a fixed ten-then-two-minutes is a metronome, and not being a metronome is
# the entire reason for batching.


def test_the_batch_size_comes_from_the_settings():
    values = {**campaign.app_settings.defaults(),
              "batch_size_min": 8, "batch_size_max": 20}
    assert campaign.batch_span(values) == (8, 20)


def test_the_pause_comes_from_the_settings():
    values = {**campaign.app_settings.defaults(),
              "batch_pause_min_s": 30, "batch_pause_max_s": 90}
    assert campaign.pause_span(values) == (30.0, 90.0)


def test_a_range_the_wrong_way_round_never_makes_the_sampler_raise():
    """random.randint(15, 5) is an error, not a small batch. The maximum is
    lifted to the minimum rather than the operator's floor being ignored."""
    values = {**campaign.app_settings.defaults(),
              "batch_size_min": 30, "batch_size_max": 4,
              "batch_pause_min_s": 200, "batch_pause_max_s": 10}
    assert campaign.batch_span(values) == (30, 30)
    assert campaign.pause_span(values) == (200.0, 200.0)


def test_settings_reconciles_the_pairs_on_the_way_out():
    """And the same rule is applied when the values are READ, so a store written
    by hand cannot hand a run a backwards range in the first place."""
    from backend import settings as s

    fixed = s._reconcile({**s.defaults(), "batch_size_min": 40,
                          "batch_pause_min_s": 500})
    assert fixed["batch_size_max"] == 40
    assert fixed["batch_pause_max_s"] == 500


def test_both_batch_ranges_are_registered_as_ranges():
    """The reconcile is driven off this list — a pair missing from it is a pair
    that can silently go backwards."""
    from backend import settings as s

    assert ("batch_size_min", "batch_size_max") in s.RANGES
    assert ("batch_pause_min_s", "batch_pause_max_s") in s.RANGES


def test_a_run_waits_between_batches(monkeypatch):
    """The behaviour itself: with batching on, a run of 12 at a batch size of 3
    stops several times, and every wait is sampled from the range."""
    waits = []

    async def _beat(span):
        waits.append(span)
        return 0.0

    monkeypatch.setattr(campaign, "_quiet", lambda ctl, span: _beat(span))
    _run_with_send(monkeypatch, clicking=True, batching=True,
                   settings={"batch_size_min": 3, "batch_size_max": 3,
                             "batch_pause_min_s": 42, "batch_pause_max_s": 99},
                   targets=[{"username": f"u{i}"} for i in range(12)])

    assert len(waits) >= 3                      # 12 recipients, batches of 3
    assert all(span == (42.0, 99.0) for span in waits)


def test_batching_off_never_waits(monkeypatch):
    waits = []
    monkeypatch.setattr(campaign, "_quiet",
                        lambda ctl, span: waits.append(span) or _zero())
    _run_with_send(monkeypatch, clicking=True, batching=False,
                   settings={"batch_size_min": 2, "batch_size_max": 2},
                   targets=[{"username": f"u{i}"} for i in range(8)])

    assert waits == []


async def _zero():
    return 0.0


# ── Several accounts at once ─────────────────────────────────────────────
# One LANE per picked account: its own browser window, its own slice of the
# queue, its own tab in the live panel. The whole point is that the run finishes
# in a fraction of the time AND no single account starts every conversation.


@pytest.fixture
def named_accounts(monkeypatch):
    """Labels for the lanes, without a real account store."""
    monkeypatch.setattr(campaign, "account_label",
                        lambda aid: {7: "Tom", 8: "Nick", 9: "Priya"}.get(aid)
                        or f"#{aid}")


def _lane_rows(ctl):
    return {lane["label"]: [r["username"] for r in lane["members"]]
            for lane in ctl.meta["accounts"]}


def test_the_recipients_are_dealt_between_the_accounts(monkeypatch, named_accounts):
    ctl = Ctl()
    _run_with_send(monkeypatch, clicking=True, ctl=ctl, account_ids=[7, 8],
                   targets=[{"username": f"u{i}"} for i in range(10)])

    lanes = _lane_rows(ctl)
    assert sorted(lanes) == ["Nick", "Tom"]
    assert len(lanes["Tom"]) == 5 and len(lanes["Nick"]) == 5
    # Every recipient exactly once, across the lanes — a double-deal would
    # message somebody twice from two different accounts.
    everyone = lanes["Tom"] + lanes["Nick"]
    assert sorted(everyone) == sorted(f"u{i}" for i in range(10))
    assert len(everyone) == len(set(everyone))


def test_an_uneven_split_differs_by_at_most_one(monkeypatch, named_accounts):
    """Seven recipients over three accounts is 3/2/2, not 7/0/0."""
    ctl = Ctl()
    _run_with_send(monkeypatch, clicking=True, ctl=ctl, account_ids=[7, 8, 9],
                   targets=[{"username": f"u{i}"} for i in range(7)])

    sizes = sorted(len(v) for v in _lane_rows(ctl).values())
    assert sizes == [2, 2, 3]


def test_more_accounts_than_recipients_leaves_the_extra_lanes_empty(
        monkeypatch, named_accounts):
    """Two recipients over three accounts: nobody is messaged twice to keep a
    lane busy."""
    ctl = Ctl()
    _run_with_send(monkeypatch, clicking=True, ctl=ctl, account_ids=[7, 8, 9],
                   targets=[{"username": "a"}, {"username": "b"}])

    sizes = sorted(len(v) for v in _lane_rows(ctl).values())
    assert sizes == [0, 1, 1]


def test_each_account_opens_its_own_window(monkeypatch, named_accounts):
    """A lane is a browser, not a tab. Two accounts sharing one window would be
    two accounts sharing one session, which is the thing they exist to avoid.

    Watched through profile_dir — that IS the identity of a window here, and it
    is what open_page is handed."""
    asked = []
    monkeypatch.setattr(campaign.sessions, "profile_dir",
                        lambda i: asked.append(i) or f"profile-{i}")

    _run_with_send(monkeypatch, clicking=True, account_ids=[7, 8],
                   targets=[{"username": f"u{i}"} for i in range(6)])

    assert sorted(asked) == [7, 8]


def test_one_account_still_runs_as_a_single_lane(monkeypatch, named_accounts):
    ctl = Ctl()
    _run_with_send(monkeypatch, clicking=True, ctl=ctl, account_ids=[7],
                   targets=[{"username": f"u{i}"} for i in range(4)])

    assert list(_lane_rows(ctl)) == ["Tom"]
    assert len(ctl.meta["accounts"][0]["members"]) == 4


def test_no_account_at_all_is_still_one_lane(monkeypatch):
    """The older shape — a run with no account picked drives the default
    profile, and the panel must still have one tab to draw."""
    ctl = Ctl()
    _run_with_send(monkeypatch, clicking=True, ctl=ctl,
                   targets=[{"username": "a"}, {"username": "b"}])

    assert len(ctl.meta["accounts"]) == 1
    assert ctl.meta["accounts"][0]["id"] is None


def test_every_lane_sends_its_own_slice(monkeypatch, named_accounts):
    ctl = Ctl()
    result = _run_with_send(monkeypatch, clicking=True, ctl=ctl,
                            account_ids=[7, 8],
                            targets=[{"username": f"u{i}"} for i in range(9)])

    assert result["sent"] == 9
    for lane in ctl.meta["accounts"]:
        assert {r["status"] for r in lane["members"]} == {"sent"}


def test_a_stop_ends_every_account(monkeypatch, named_accounts):
    """End is one button over the whole campaign, not one per window."""
    from backend import control

    ctl = control.Run("outreach")

    async def _send(page, handle, text, image=None, name="", ctl=None):
        if run.done >= 2:
            run.stop()
        await asyncio.sleep(0)
        return {"ok": True, "detail": "ok"}

    run = ctl
    _run_with_send(monkeypatch, clicking=True, ctl=run, account_ids=[7, 8],
                   send=_send,
                   targets=[{"username": f"u{i}"} for i in range(40)])

    rows = [r for lane in run.meta["accounts"] for r in lane["members"]]
    assert any(r["status"] == "pending" for r in rows)     # both lanes stopped
    assert sum(1 for r in rows if r["status"] == "sent") < 40
    # And no lane carried on after the stop — the counts on both tabs are short.
    for lane in run.meta["accounts"]:
        assert any(r["status"] == "pending" for r in lane["members"])


# ── The chat link, and the tab it opens in ───────────────────────────────
# Settings → chat_link is the address a campaign opens per recipient. The run
# fills the member's id into it and opens THAT, in a tab of its own.


def test_each_recipient_gets_a_tab_of_their_own(monkeypatch):
    tabs = []
    _run_with_send(monkeypatch, clicking=True, tabs=tabs)

    assert len(tabs) == 2


def test_a_sent_message_closes_its_tab(monkeypatch):
    tabs = []
    _run_with_send(monkeypatch, clicking=True, tabs=tabs)

    assert all(tab.closed for tab in tabs)


def test_a_typed_composer_closes_its_tab_too(monkeypatch):
    """EVERY tab closes when its recipient is done, Click Send on or off.

    An earlier build kept the written-but-not-sent ones up, on the reasoning
    that a loaded composer is what Click Send off exists to produce. A run of
    twenty left twenty tabs open and the window was unusable long before
    anybody read one, so the tab goes and the typed draft goes with it."""
    tabs = []
    result = _run_with_send(monkeypatch, clicking=False, tabs=tabs)

    assert tabs and all(tab.closed for tab in tabs)
    assert result["typed"] == 2          # still reported as written, not sent


# ── The live table ───────────────────────────────────────────────────────
# The Campaign live board is ctl.meta["members"], polled. The run owns those rows
# — who gets which draft and which picture is decided before anything is sent —
# so the panel never has to guess the pairing back out of a result.


def test_the_run_publishes_a_row_per_recipient(monkeypatch):
    ctl = Ctl()
    _run_with_send(monkeypatch, clicking=True, ctl=ctl)

    rows = ctl.meta["members"]
    assert [r["username"] for r in sorted(rows, key=lambda r: r["username"])] \
        == ["u1", "u2"]


def test_a_row_names_its_draft_and_its_picture(monkeypatch):
    ctl = Ctl()
    _run_with_send(monkeypatch, clicking=True, ctl=ctl,
                   drafts=[{"label": "sms1", "text": "hello {name}"}],
                   images=["promo.png"])

    row = ctl.meta["members"][0]
    assert row["sms"] == "sms1"
    assert row["media"] == "promo.png"
    # The rendered words ride along for the row's tooltip — {name} with nothing
    # to fill it becomes the neutral fallback rather than being left literal.
    assert row["text"] == "hello there"


def test_an_unnamed_draft_is_called_by_its_position():
    """A draft nobody titled still has to be nameable in a column. Storage gives
    a stored draft its own name, so this fallback is for one handed straight to
    a run — but a blank cell in the SMS column is not an option either way."""
    assert campaign._draft_label({"text": "a"}, [{"text": "a"}]) == "sms1"
    two = [{"text": "a"}, {"text": "b"}]
    assert campaign._draft_label(two[1], two) == "sms2"
    assert campaign._draft_label({"label": "opener"}, []) == "opener"
    assert campaign._draft_label(None, []) == ""


def test_every_row_lands_on_a_final_status(monkeypatch):
    ctl = Ctl()
    _run_with_send(monkeypatch, clicking=True, ctl=ctl)

    assert {r["status"] for r in ctl.meta["members"]} == {"sent"}


def test_click_send_off_leaves_the_rows_typed(monkeypatch):
    """Not sent and not failed — the board has to say the third thing."""
    ctl = Ctl()
    _run_with_send(monkeypatch, clicking=False, ctl=ctl)

    assert {r["status"] for r in ctl.meta["members"]} == {"typed"}


def test_a_failure_carries_its_reason_onto_the_row(monkeypatch):
    """"Failed" on its own is not something an operator can act on."""
    async def _refuse(page, handle, text, image=None, name="", ctl=None):
        return {"ok": False, "detail": "Member cannot be messaged"}

    ctl = Ctl()
    _run_with_send(monkeypatch, clicking=True, ctl=ctl, send=_refuse)

    rows = ctl.meta["members"]
    assert {r["status"] for r in rows} == {"failed"}
    assert all(r["detail"] == "Member cannot be messaged" for r in rows)


def test_the_finished_table_comes_back_with_the_result(monkeypatch):
    """A run that ends between two polls would otherwise leave its last row
    showing Processing for good."""
    result = _run_with_send(monkeypatch, clicking=True)

    assert [r["status"] for r in result["members"]] == ["sent", "sent"]


def test_the_saved_link_is_filled_in_per_member():
    from backend.automation import meetup

    built = meetup.chat_link("312094871", "Alex Doe",
                             template="https://x.test/c?u={username}&n={name}")

    assert built == "https://x.test/c?u=312094871&n=Alex+Doe"


def test_the_default_link_is_meetups_new_conversation_composer():
    """The two the operator gave, exactly. A name with a space is the whole
    point of the second: it goes in FORM-encoded, as "Nick+w" — %20 is what
    ordinary URL quoting would produce and not what Meetup's own links carry."""
    from backend.automation import meetup

    assert meetup.chat_link("212235799", "Tom") == (
        "https://www.meetup.com/messages/?new_convo=true"
        "&member_id=212235799&name=Tom")
    assert meetup.chat_link("305387249", "Nick w") == (
        "https://www.meetup.com/messages/?new_convo=true"
        "&member_id=305387249&name=Nick+w")


def test_a_link_with_no_placeholder_gets_the_id_appended():
    """A URL pasted straight out of the address bar is the likeliest thing to be
    saved, and opening the identical link for every recipient is the worst
    reading of it."""
    from backend.automation import meetup

    assert meetup.chat_link("abc", template="https://x.test/c?member=") \
        == "https://x.test/c?member=abc"
    assert meetup.chat_link("abc", template="https://x.test/c") \
        == "https://x.test/c/abc"


def test_an_emptied_link_falls_back_to_the_built_in_composer():
    """A blank Settings box is not a reason for a run to have nowhere to go."""
    from backend.automation import meetup

    assert meetup.chat_link("abc", "Tom", template="  ") \
        == meetup.chat_link("abc", "Tom", template=meetup.DEFAULT_CHAT_LINK)


def test_drafts_left_unset_still_come_from_the_store(monkeypatch):
    """The other half of that distinction: not passing drafts at all is not the
    same as passing none, and still means "use what is saved"."""
    monkeypatch.setattr(campaign, "pick_targets",
                        lambda *a, **k: [{"username": "someone"}])
    db.save_message_drafts([{"label": "stored", "text": "STORED"}])

    result = _outreach()

    assert "STORED" in " ".join(p["text"] for p in result["previews"])
