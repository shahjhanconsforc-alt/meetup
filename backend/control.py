"""Run control — start, pause, resume, stop.

Every long job (a scrape, a campaign) runs under one of these. It is the single
place that answers "should this run keep going?", so an automation loop only has
to `await ctl.checkpoint()` between units of work and pausing/stopping is handled
for it.

Why a checkpoint rather than cancelling the task: a run is halfway through a
browser action most of the time, and killing that mid-flight leaves Meetup with a
half-submitted message and the profile in a state the next run has to guess at.
Cooperative stopping means a run always finishes the member it is on and stops on
a clean boundary.

    ctl = Run("scrape")
    await ctl.checkpoint()      # returns False when the run should end
    ctl.pause() / resume() / stop()
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from backend import activity

# One run per kind at a time — starting a second scrape while one is going would
# have two browsers driving the same profile. Keyed by kind, not global, so a
# campaign and a scrape can still overlap if an operator wants that.
_runs: dict[str, "Run"] = {}
_lock = threading.Lock()


class RunStopped(Exception):
    """Raised by `guard()` when the run was stopped — lets a deeply nested step
    unwind without every level having to check a return value."""


class Run:
    """The state of one long job."""

    def __init__(self, kind: str, total: int = 0, meta: dict | None = None):
        self.kind = kind
        self.total = int(total or 0)
        self.done = 0
        self.meta: dict[str, Any] = dict(meta or {})
        self.started_at = time.monotonic()
        self.finished_at: float | None = None
        self.status = "running"          # running | paused | stopping | done | error
        self.detail = ""
        self._resume = asyncio.Event()
        self._resume.set()               # not paused
        self._stop = False

    # ── state changes ────────────────────────────────────────────────
    def pause(self) -> dict:
        if self.status == "running":
            self.status = "paused"
            self._resume.clear()
            activity.log(self.kind, "Paused", "warn")
        return self.snapshot()

    def resume(self) -> dict:
        if self.status == "paused":
            self.status = "running"
            self._resume.set()
            activity.log(self.kind, "Resumed", "ok")
        return self.snapshot()

    def stop(self) -> dict:
        self._stop = True
        self.status = "stopping"
        self._resume.set()               # a paused run must wake up to notice
        activity.log(self.kind, "Stop requested — finishing current step", "warn")
        return self.snapshot()

    def finish(self, detail: str = "", error: bool = False) -> dict:
        self.status = "error" if error else "done"
        self.detail = detail
        self.finished_at = time.monotonic()
        activity.log(self.kind, detail or "Finished", "error" if error else "ok")
        with _lock:
            if _runs.get(self.kind) is self:
                _runs.pop(self.kind, None)
        return self.snapshot()

    # ── what an automation loop calls ────────────────────────────────
    @property
    def stopping(self) -> bool:
        """Whether End has been pressed. For steps that WAIT rather than work.

        checkpoint() is the boundary between units of work, and it is enough for
        a loop. A long deliberate pause — the minutes a campaign leaves between
        batches — is neither working nor at a boundary, and sleeping through it
        is what makes End look like a button that did nothing. Those waits watch
        this instead.
        """
        return self._stop

    @property
    def paused(self) -> bool:
        """Whether the run is sitting at a checkpoint waiting to be resumed.

        For WATCHDOGS. A stall clock measures how long the work has produced
        nothing, and a paused run produces nothing by definition — without this
        the first pause long enough to matter would look exactly like a hung
        page and get the work abandoned.
        """
        return not self._resume.is_set()

    async def checkpoint(self, step: str = "") -> bool:
        """Call between units of work. Blocks while paused; returns False when
        the run should end."""
        if self._stop:
            return False
        if not self._resume.is_set():
            await self._resume.wait()
        if step:
            self.detail = step
        return not self._stop

    async def guard(self, step: str = "") -> None:
        """checkpoint() as an exception — for steps too nested to thread a bool
        back up through."""
        if not await self.checkpoint(step):
            raise RunStopped(f"{self.kind} stopped")

    def advance(self, n: int = 1, detail: str = "") -> None:
        self.done += n
        if detail:
            self.detail = detail

    # ── reporting ────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        elapsed = (self.finished_at or time.monotonic()) - self.started_at
        return {
            "kind": self.kind,
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "percent": round(100 * self.done / self.total, 1) if self.total else None,
            "detail": self.detail,
            "elapsed_s": round(elapsed, 1),
            "meta": dict(self.meta),
        }


# ── registry ─────────────────────────────────────────────────────────


def start(kind: str, total: int = 0, meta: dict | None = None) -> Run:
    """Register a new run of `kind`. Raises RuntimeError if one is already going —
    the caller should surface that as a 409 rather than silently queueing."""
    with _lock:
        existing = _runs.get(kind)
        if existing and existing.status in ("running", "paused", "stopping"):
            raise RuntimeError(f"A {kind} run is already in progress")
        run = Run(kind, total=total, meta=meta)
        _runs[kind] = run
        activity.log(kind, f"Started{f' ({total} queued)' if total else ''}", "ok")
        return run


def get(kind: str) -> Run | None:
    return _runs.get(kind)


def require(kind: str) -> Run:
    run = _runs.get(kind)
    if run is None:
        raise KeyError(f"No {kind} run in progress")
    return run


def all_runs() -> list[dict]:
    return [run.snapshot() for run in _runs.values()]


# A run that has not finished still owns the browser: paused means "stopped
# mid-step with a page half-driven", not "let go of it".
LIVE = ("running", "paused", "stopping")


def busy_kinds() -> list[str]:
    """The kinds of run currently driving the browser.

    There is ONE browser context. A step that navigates while a sweep is walking
    categories does not queue behind it — the two take turns on the same page,
    and what the operator sees is their own click apparently opening something
    they never picked. Callers that drive the browser directly check this first.
    """
    with _lock:
        return [kind for kind, run in _runs.items() if run.status in LIVE]


def stop_all() -> list[dict]:
    return [run.stop() for run in list(_runs.values())]
