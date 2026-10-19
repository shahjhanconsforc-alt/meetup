"""Confidence Agent — how much a conclusion is actually supported.

Scores each subject by summing its signals' weights, then adds a bonus for the
number of DISTINCT kinds of evidence behind it. That second part is the whole
idea: one strong signal repeated is one observation, while a term plus a hashtag
plus an emoji is three independent people-writing-things, and the second case
should win.

Concretely, this is what stops a bio containing the single word "data" from being
filed as confidently Tech, while a bio with "python", #devops and 💻 is.
"""
from __future__ import annotations

from .. import config
from ..contracts import Signal


class ConfidenceAgent:
    """Stateless — one instance is shared by every worker."""

    def score(self, signals: list[Signal]) -> dict[str, float]:
        """Total weight per subject, with the diversity bonus applied."""
        totals: dict[str, float] = {}
        kinds: dict[str, set[str]] = {}

        for signal in signals:
            # Only category signals are scored. Entities are extracted facts, not
            # competing conclusions — an entity doesn't need to beat a threshold
            # to be true, it just needs to have been found.
            if signal.kind == "entity":
                continue
            weight = config.WEIGHTS.get(signal.kind, 0.5)
            totals[signal.subject] = totals.get(signal.subject, 0.0) + weight
            kinds.setdefault(signal.subject, set()).add(signal.kind)

        for subject, distinct in kinds.items():
            # Bonus per kind BEYOND the first — one kind of evidence earns no
            # corroboration bonus, which is the point.
            totals[subject] += config.DIVERSITY_BONUS * max(0, len(distinct) - 1)

        return {subject: round(score, 3)
                for subject, score in sorted(totals.items(), key=lambda i: -i[1])}

    def keep(self, scored: dict[str, float]) -> dict[str, float]:
        """Just the subjects that cleared the keep threshold, best first."""
        return {subject: score for subject, score in scored.items()
                if score >= config.KEEP_THRESHOLD}

    def explain(self, signals: list[Signal], subject: str) -> list[dict]:
        """Every piece of evidence behind one subject, for auditing a verdict.

        Not used in the hot path — this is what you call when a member has been
        filed somewhere surprising and you need to know which term did it.
        """
        return [
            {"kind": s.kind, "evidence": s.evidence,
             "weight": config.WEIGHTS.get(s.kind, 0.5)}
            for s in signals if s.subject == subject
        ]
