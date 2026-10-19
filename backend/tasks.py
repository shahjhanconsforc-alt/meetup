"""Background tasks — the bridge between an HTTP request and a long job.

A scrape or a campaign runs for minutes. Holding the request open for that would
time out at every proxy in between and give the UI nothing to render until it
finished, so a route starts a task here, returns its id immediately, and the
panel polls `status(task_id)`.

Pairs with control.py: `control` decides whether a run should keep going, this
decides where it runs and how its result gets back. Kept apart because pausing a
run and tracking an asyncio task are genuinely different concerns — a task that
finished has a result to hand back long after its Run is gone.

    task_id = spawn("scrape", coro_fn(), ctl=run)
    status(task_id) -> {state, result, error, run}
"""
from __future__ import annotations

import asyncio
import itertools
import traceback
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from backend import activity, control

# Finished tasks are kept so a panel that polls a moment late still sees the
# result. Bounded, oldest-first, because nothing else ever clears them.
MAX_KEPT = 50

_seq = itertools.count(1)
_tasks: dict[int, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _prune() -> None:
    finished = [tid for tid, rec in _tasks.items()
                if rec["state"] in ("done", "error", "cancelled")]
    for tid in sorted(finished)[:max(0, len(finished) - MAX_KEPT)]:
        _tasks.pop(tid, None)


def spawn(kind: str, coro: Awaitable, ctl: control.Run | None = None) -> int:
    """Run `coro` in the background. Returns a task id to poll."""
    task_id = next(_seq)
    record = {
        "id": task_id,
        "kind": kind,
        "state": "running",
        "started_at": _now(),
        "finished_at": None,
        "result": None,
        "error": None,
        "ctl": ctl,
    }
    _tasks[task_id] = record

    async def _wrapper():
        try:
            record["result"] = await coro
            record["state"] = "done"
            if ctl is not None and ctl.status not in ("done", "error"):
                ctl.finish()
        except asyncio.CancelledError:
            record["state"] = "cancelled"
            if ctl is not None:
                ctl.finish("Cancelled", error=True)
            raise
        except control.RunStopped as exc:
            # A cooperative stop is a normal ending, not a failure — the operator
            # asked for it, and whatever the run completed before it still counts.
            record["state"] = "done"
            record["result"] = {"stopped": True, "detail": str(exc)}
            if ctl is not None:
                ctl.finish("Stopped")
        except Exception as exc:  # noqa: BLE001 — a failed job must not kill the loop
            record["state"] = "error"
            record["error"] = f"{type(exc).__name__}: {exc}"
            activity.log(kind, f"Failed: {record['error']}", "error")
            # The traceback goes to the server terminal, not the API: it can
            # carry file paths and, in a login flow, arguments worth not echoing.
            traceback.print_exc()
            if ctl is not None:
                ctl.finish(record["error"], error=True)
        finally:
            record["finished_at"] = _now()
            record["task"] = None
            _prune()

    record["task"] = asyncio.create_task(_wrapper(), name=f"{kind}-{task_id}")
    return task_id


def status(task_id: int) -> dict:
    """One task's state, with its run's progress folded in if it has one."""
    record = _tasks.get(int(task_id))
    if record is None:
        raise KeyError(f"No task {task_id}")
    ctl: control.Run | None = record.get("ctl")
    return {
        "id": record["id"],
        "kind": record["kind"],
        "state": record["state"],
        "started_at": record["started_at"],
        "finished_at": record["finished_at"],
        "result": record["result"],
        "error": record["error"],
        "run": ctl.snapshot() if ctl is not None else None,
    }


def listing() -> list[dict]:
    """Every tracked task, newest first."""
    return [status(tid) for tid in sorted(_tasks, reverse=True)]


def running(kind: str | None = None) -> list[dict]:
    return [t for t in listing()
            if t["state"] == "running" and (kind is None or t["kind"] == kind)]


async def cancel(task_id: int) -> dict:
    """Hard-cancel a task.

    Prefer stopping its Run: that lets the browser finish the action it is on.
    This is the escape hatch for a task wedged somewhere with no checkpoint.
    """
    record = _tasks.get(int(task_id))
    if record is None:
        raise KeyError(f"No task {task_id}")
    task: asyncio.Task | None = record.get("task")
    if task is None or task.done():
        return status(task_id)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    return status(task_id)


def run_sync(kind: str, fn: Callable[[], Any]) -> Awaitable:
    """Run a BLOCKING function off the event loop.

    For anything threaded, synchronous and CPU-bound: calling it directly from
    a route would freeze every other request — including the activity poll the
    UI needs to show that it is working.
    """
    return asyncio.get_running_loop().run_in_executor(None, fn)
