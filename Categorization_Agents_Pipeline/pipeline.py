"""The orchestrator: one queue, N workers, eight agents per profile.

                     Data Loader Agent
                            │
                    ┌───────▼───────┐
                    │     queue     │
                    └───────┬───────┘
      ┌──────────┬──────────┼──────────┬──────────┐
   Worker 1   Worker 2   Worker 3   Worker 4  …  Worker N
      └──────────┴──────────┼──────────┴──────────┘
                     Database Writer Agent

Each worker takes a profile off the shared queue and runs it through the full
agent chain independently — clean, extract interests, extract entities, score,
classify, tag, submit. No worker needs to know about any other, which is exactly
why a queue works here: the agents in the middle are pure functions of their
input, so there is no shared state to guard beyond the writer's buffer.

On concurrency, honestly: the per-profile work is regex over a short string and
takes well under a millisecond, and CPython holds the GIL through `re`. Threads
here therefore buy pipeline shape and overlapped store I/O rather than a linear
speedup — the run is dominated by the two MinIO round-trips at either end. The
worker count is still real (and worth raising if the lexicon grows or an agent
later does I/O of its own); it is not a promise of N-times throughput.
"""
from __future__ import annotations

import queue
import threading
import time

from . import config, matching
from .agents import (BioCleaningAgent, ClassifierAgent, ConfidenceAgent,
                     DataLoaderAgent, EntityAgent, InterestAgent, TaggerAgent,
                     WriterAgent)
from .contracts import Profile, Verdict


class CategorizationPipeline:
    """Runs the whole job. Construct once, call `run()`."""

    def __init__(self, workers: int | None = None, dry_run: bool = False,
                 source: str = "bio", progress=None):
        self.workers = max(1, min(int(workers or config.DEFAULT_WORKERS),
                                  config.MAX_WORKERS))
        self.loader = DataLoaderAgent(source=source)
        self.writer = WriterAgent(dry_run=dry_run)
        self.progress = progress          # optional callable(done, total)

        # The middle six are stateless, so one instance each is shared by every
        # worker — there is nothing in them to race on.
        self.cleaner = BioCleaningAgent()
        self.interests = InterestAgent()
        self.entities = EntityAgent()
        self.confidence = ConfidenceAgent()
        self.classifier = ClassifierAgent()
        self.tagger = TaggerAgent()

        self._done = 0
        self._counter_lock = threading.Lock()
        self._stop = threading.Event()
        self.failures: list[str] = []

    # ── the agent chain for ONE profile ──────────────────────────────────
    def process(self, profile: Profile) -> Verdict:
        """Every agent, in order, for a single profile. Pure — no I/O."""
        clean = self.cleaner.run(profile)

        # Extract, THEN resolve overlaps, THEN infer. The order is load-bearing:
        # suppression has to see interest terms and entities together (the word
        # "art" competes with "martial arts"), and inference has to run after it
        # so a suppressed entity can't imply a category.
        found = self.interests.run(clean) + self.entities.run(clean)
        signals = matching.suppress_contained(found)
        signals += self.entities.imply(signals)

        scored = self.confidence.score(signals)
        kept = self.confidence.keep(scored)

        categories = self.classifier.run(signals, kept)
        tags = self.tagger.run(signals, categories, kept)
        # The tagger may add Travel for the multi-location rule after the
        # classifier settled; re-reading the categories off the tags keeps the
        # two columns from ever disagreeing.
        categories = self.tagger.categories_for(tags) or categories

        return Verdict(
            username=profile.username,
            bio=profile.bio,
            categories=categories,
            tags=tags,
            entities=self.entities.collect(signals),
            confidence={k: v for k, v in kept.items()},
            language=clean.language,
        )

    # ── the pool ─────────────────────────────────────────────────────────
    def _worker(self, jobs: "queue.Queue[Profile]", total: int) -> None:
        while not self._stop.is_set():
            try:
                profile = jobs.get_nowait()
            except queue.Empty:
                return
            try:
                verdict = self.process(profile)
                if verdict.categories or verdict.tags:
                    self.writer.submit(verdict)
            except Exception as exc:  # noqa: BLE001 — one bad bio isn't fatal
                self.failures.append(f"@{profile.username}: {type(exc).__name__}: {exc}")
            finally:
                with self._counter_lock:
                    self._done += 1
                    done = self._done
                if self.progress and done % 250 == 0:
                    self.progress(done, total)
                jobs.task_done()

    def run(self, limit: int | None = None) -> dict:
        """Load, fan out, write. Returns a summary of what happened."""
        started = time.monotonic()
        profiles = self.loader.load(limit=limit)
        total = len(profiles)
        if not total:
            return self._summary(started, 0, [])

        jobs: "queue.Queue[Profile]" = queue.Queue()
        for p in profiles:
            jobs.put(p)

        threads = [threading.Thread(target=self._worker, args=(jobs, total),
                                    name=f"cat-worker-{i + 1}", daemon=True)
                   for i in range(self.workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.writer.flush()               # never drop what the workers decided
        return self._summary(started, total, profiles)

    def stop(self) -> None:
        """Ask the workers to finish the profile in hand and stop."""
        self._stop.set()

    def _summary(self, started: float, total: int, profiles) -> dict:
        elapsed = max(time.monotonic() - started, 1e-6)
        return {
            "processed": total,
            "written": self.writer.written,
            "workers": self.workers,
            "elapsed_s": round(elapsed, 2),
            "rate_per_s": round(total / elapsed, 1),
            "loader": self.loader.report(),
            "writer": self.writer.report(),
            "failures": self.failures[:20],
            "failure_count": len(self.failures),
        }
