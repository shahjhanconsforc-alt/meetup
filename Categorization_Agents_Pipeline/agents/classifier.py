"""Classifier Agent — scores into a ranked list of categories.

Small, because the hard work happened upstream: the Confidence Agent already
decided what is supported. This one applies the last two rules — that the primary
category needs a higher bar than a secondary one, and that a member can only
carry so many before the label stops meaning anything.
"""
from __future__ import annotations

from .. import config
from ..contracts import Signal


class ClassifierAgent:
    """Stateless — one instance is shared by every worker."""

    def run(self, signals: list[Signal], kept: dict[str, float]) -> list[str]:
        """The member's categories, best first. Empty when nothing qualified.

        Empty is a real and correct answer: the writer skips a verdict with no
        categories and no tags, so a member the lexicon has nothing to say about
        stays out of the category store rather than being filed as "Unknown".
        A blank is honest; "Unknown" is a claim.
        """
        if not kept:
            return []

        ranked = sorted(kept.items(), key=lambda i: -i[1])
        best_score = ranked[0][1]

        # Nothing cleared the primary bar — the evidence supports "possibly these
        # things", not "this member is X". Refuse to promote any of it.
        if best_score < config.PRIMARY_THRESHOLD:
            return []

        categories = [subject for subject, _ in ranked[:config.MAX_CATEGORIES]]

        # A secondary category scoring far below the primary is noise the primary
        # dragged along. Half is the cut — a genuine second interest scores in the
        # same range as the first, an incidental term does not.
        floor = best_score * 0.5
        return [categories[0]] + [c for c in categories[1:] if kept[c] >= floor]
