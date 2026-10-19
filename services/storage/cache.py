"""A tiny TTL cache in front of the MinIO stores.

Every store here keeps a whole table in ONE object, so `load_all()` is a full
download. The Data panel polls, the campaign wizard re-reads on every step, and
the pipeline's loader reads the same table the writer just wrote — without this,
a single page render fires several identical multi-megabyte GETs.

Deliberately NOT a general cache: entries are keyed by store name, the TTL is
short, and every mutating path calls `drop()` for its own key. A stale read here
would show an operator a member list that doesn't match what the next write is
about to be based on, so correctness beats hit rate — when in doubt it expires.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

DEFAULT_TTL = 5.0          # seconds — long enough for one page render, short
                           # enough that a scrape's results appear immediately

_lock = threading.Lock()
_entries: dict[str, tuple[float, Any]] = {}


def get(key: str, ttl: float = DEFAULT_TTL) -> Any | None:
    """The cached value for `key`, or None if absent/expired.

    None is also a legitimate cached value in theory, but no store here ever
    caches one — they cache lists — so collapsing the two keeps the API simple.
    """
    with _lock:
        entry = _entries.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if (time.monotonic() - stored_at) > ttl:
            _entries.pop(key, None)
            return None
        return value


def put(key: str, value: Any) -> Any:
    with _lock:
        _entries[key] = (time.monotonic(), value)
    return value


def drop(*keys: str) -> None:
    """Invalidate specific keys — call after any write to that store."""
    with _lock:
        for key in keys:
            _entries.pop(key, None)


def clear() -> int:
    with _lock:
        count = len(_entries)
        _entries.clear()
        return count


def through(key: str, loader: Callable[[], Any], ttl: float = DEFAULT_TTL) -> Any:
    """Read `key` from cache, calling `loader()` on a miss and caching the result.

    The loader runs OUTSIDE the lock: it does network I/O to MinIO and can take
    seconds when the server is slow, and holding the lock across it would turn a
    slow MinIO into a stalled server. Two concurrent misses may therefore both
    load — wasteful but harmless, whereas the alternative is a global stall.
    """
    hit = get(key, ttl)
    if hit is not None:
        return hit
    return put(key, loader())


def stats() -> dict:
    with _lock:
        now = time.monotonic()
        return {
            "entries": len(_entries),
            "keys": sorted(_entries),
            "ages_s": {k: round(now - at, 1) for k, (at, _) in _entries.items()},
        }
