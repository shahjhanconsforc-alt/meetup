"""Tagger Agent — the narrow, filterable labels a verdict carries.

Categories are too broad to write a message against: "Tech" covers a Rust kernel
developer and someone learning HTML. Tags are what the campaign actually filters
on — `in:berlin`, `plays:climbing`, `job:founder`, `cat:tech`.

Also holds the ONE rule that can add a category after the classifier has settled:
a bio naming several cities is a traveller, and that is a conclusion no single
term supports. pipeline.py re-reads the categories off the tags afterwards so the
two columns can never disagree.
"""
from __future__ import annotations

from .. import config
from ..contracts import Signal
from ..lexicon import tag_for

# A bio has to name at least this many distinct places before multiple locations
# mean "travels" rather than "moved once" or "works in one city, lives in another".
_TRAVEL_LOCATIONS = 3

# Tag prefix for a category, so a category is filterable through the same
# mechanism as everything else rather than needing its own column in the query.
_CATEGORY_PREFIX = "cat"


class TaggerAgent:
    """Stateless — one instance is shared by every worker."""

    def run(self, signals: list[Signal], categories: list[str],
            kept: dict[str, float]) -> list[str]:
        tags: list[str] = []

        for category in categories:
            tags.append(f"{_CATEGORY_PREFIX}:{category.lower()}")

        locations: list[str] = []
        for signal in signals:
            if signal.kind != "entity" or not signal.facet:
                continue
            tag = signal.tag or tag_for(signal.facet, signal.subject)
            if tag and tag not in tags:
                tags.append(tag)
            if signal.facet == "loc":
                locations.append(signal.subject)

        # The multi-location rule. Applied here rather than in the lexicon's
        # IMPLIES because it is a fact about the SET of signals, not about any one
        # of them — no single place name can imply it.
        if len(set(locations)) >= _TRAVEL_LOCATIONS:
            travel = f"{_CATEGORY_PREFIX}:travel"
            if travel not in tags:
                tags.append(travel)

        return tags[:config.MAX_TAGS]

    def categories_for(self, tags: list[str]) -> list[str]:
        """Read the categories back off the tags.

        Called by pipeline.py after this agent runs, because the Travel rule above
        can add a category the classifier never saw. Making the tags the single
        source of truth for both columns is what keeps them from drifting apart.
        """
        prefix = f"{_CATEGORY_PREFIX}:"
        return [tag[len(prefix):].title() for tag in tags if tag.startswith(prefix)]
