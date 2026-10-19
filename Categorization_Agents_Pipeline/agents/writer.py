"""Database Writer Agent — verdicts into the category store.

The other end of the chain from the loader, and the other agent that touches
MinIO. Every worker submits here, so this is the one place in the pipeline that
needs a lock.

Buffered, because the category store keeps its whole table in ONE object: a write
per verdict would be a full download-and-upload per member, which on a store of
20,000 is the difference between seconds and hours.
"""
from __future__ import annotations

import threading

from .. import config
from ..contracts import Verdict


class WriterAgent:
    """Buffers verdicts and pushes them in batches. Thread-safe."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.written = 0
        self.failed = 0
        self._buffer: list[dict] = []
        self._lock = threading.Lock()
        self.samples: list[dict] = []       # first few rows, for a dry run's report

    def submit(self, verdict: Verdict) -> None:
        """Take one verdict. Flushes automatically once the batch is full."""
        row = verdict.as_row()
        with self._lock:
            self._buffer.append(row)
            if len(self.samples) < 20:
                self.samples.append(row)
            ready = self._buffer if len(self._buffer) >= config.WRITE_BATCH else None
            if ready is not None:
                self._buffer = []
        if ready is not None:
            self._push(ready)

    def flush(self) -> int:
        """Push whatever is left. Always call this at the end of a run — a batch
        that never filled would otherwise be silently dropped."""
        with self._lock:
            pending, self._buffer = self._buffer, []
        return self._push(pending) if pending else 0

    def _push(self, rows: list[dict]) -> int:
        if self.dry_run:
            self.written += len(rows)
            return len(rows)
        from services.storage import meetup_category_storage as cats

        try:
            cats.add(rows)
            self.written += len(rows)
            return len(rows)
        except Exception:  # noqa: BLE001 — a dead MinIO must not kill the run
            # The workers have already done the expensive part; losing a batch is
            # bad, but losing the whole run to an exception raised on a worker
            # thread is worse. Counted so the summary reports it honestly.
            self.failed += len(rows)
            return 0

    def report(self) -> dict:
        return {"written": self.written, "failed": self.failed,
                "dry_run": self.dry_run,
                "buffered": len(self._buffer),
                "samples": self.samples[:5]}
