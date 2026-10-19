"""Live activity log — one stream for everything the app is doing.

An in-memory ring buffer plus fan-out to any connected listeners. Nothing is
persisted: this is a window onto the running process, not an audit trail.

`log()` is deliberately synchronous and never raises — logging must not be able
to break the thing it is reporting on.
"""
import asyncio
import itertools
from collections import deque
from datetime import datetime, timezone
from typing import Any

MAX_EVENTS = 600
QUEUE_SIZE = 200

LEVELS = ("info", "ok", "warn", "error")

_events: deque[dict] = deque(maxlen=MAX_EVENTS)
_seq = itertools.count(1)
_subscribers: set[asyncio.Queue] = set()

# Set when the server is going down, so the SSE feed can end its own stream.
#
# Without it Ctrl+C hung: uvicorn stops accepting new connections and then waits
# for the in-flight ones, and an event stream held open by a browser tab never
# finishes on its own — `request.is_disconnected()` stays False because the
# CLIENT is still there. The server sat waiting on a request that would never
# end. Built lazily: an Event binds to the running loop, and this module is
# imported long before there is one.
_closing: asyncio.Event | None = None


def closing() -> asyncio.Event:
    """The shutdown flag, created on first use inside the running loop."""
    global _closing
    if _closing is None:
        _closing = asyncio.Event()
    return _closing


def begin_shutdown() -> None:
    """Tell every open stream to finish. Called from the lifespan's teardown."""
    closing().set()
    # Wake anything blocked on a queue read so it notices immediately rather
    # than after its keep-alive timeout.
    for queue in list(_subscribers):
        try:
            queue.put_nowait({"id": 0, "source": "server", "level": "info",
                              "message": "shutting down", "closing": True})
        except Exception:  # noqa: BLE001
            pass


def log(source: str, message: str, level: str = "info", **extra: Any) -> dict:
    event = {
        "id": next(_seq),
        "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": level if level in LEVELS else "info",
        "source": source,
        "message": str(message)[:400],
        **extra,
    }
    _events.append(event)
    for queue in list(_subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # a listener that can't keep up loses events rather than blocking us
            pass
        except Exception:  # noqa: BLE001
            _subscribers.discard(queue)
    return event


def history(since: int = 0, limit: int = 300) -> list[dict]:
    items = [e for e in _events if e["id"] > since]
    return items[-limit:]


def clear() -> int:
    count = len(_events)
    _events.clear()
    return count


def subscribe() -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    _subscribers.discard(queue)


def stats() -> dict:
    return {
        "buffered": len(_events),
        "capacity": MAX_EVENTS,
        "listeners": len(_subscribers),
        "latest_id": _events[-1]["id"] if _events else 0,
    }
