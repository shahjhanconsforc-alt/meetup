"""Entity Agent — named things, and what they imply.

Two jobs, deliberately kept as separate calls:

    run(clean)        extract entities (places, jobs, sports, hobbies, orgs)
    imply(signals)    turn those into weak category signals

They are separate because the ORDER matters and pipeline.py needs to control it:
extraction has to happen before suppression (so overlapping entities can be
resolved), and inference has to happen after (so a suppressed entity can't imply
a category it never really supported). Fusing them would make that impossible to
express.
"""
from __future__ import annotations

from ..contracts import CleanBio, Signal
from ..lexicon import IMPLIES, tag_for
from ..matching import dedupe, find_entity_signals


class EntityAgent:
    """Stateless — one instance is shared by every worker."""

    def run(self, clean: CleanBio) -> list[Signal]:
        signals = find_entity_signals(clean.text)
        for signal in signals:
            signal.tag = tag_for(signal.facet or "", signal.subject)
        return dedupe(signals)

    def imply(self, signals: list[Signal]) -> list[Signal]:
        """Category signals implied by the entities that SURVIVED suppression.

        Weakest evidence in the system (config.WEIGHTS["implied"] = 0.35) because
        it is inference from inference: nobody wrote "Fitness", we concluded it
        from a sport we concluded from a word. Three implied signals still can't
        reach the keep threshold on their own, which is intentional — an implied
        category has to be corroborated by something the member actually said.
        """
        out: list[Signal] = []
        for signal in signals:
            if signal.kind != "entity" or not signal.facet:
                continue
            for category in IMPLIES.get(signal.facet, []):
                out.append(Signal(subject=category, kind="implied",
                                  evidence=signal.subject, span=None))
        return dedupe(out)

    def collect(self, signals: list[Signal]) -> dict[str, list[str]]:
        """Surviving entities grouped by facet — what the Verdict stores.

        Sorted, so two runs over the same bio produce byte-identical rows. The
        store is a text file people diff; unstable ordering would make every
        re-run look like a change.
        """
        out: dict[str, list[str]] = {}
        for signal in signals:
            if signal.kind == "entity" and signal.facet:
                out.setdefault(signal.facet, [])
                if signal.subject not in out[signal.facet]:
                    out[signal.facet].append(signal.subject)
        return {facet: sorted(values) for facet, values in out.items()}
