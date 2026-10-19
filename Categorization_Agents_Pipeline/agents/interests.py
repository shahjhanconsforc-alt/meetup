"""Interest Agent — which category terms a bio contains.

The bulk of the evidence. Runs the compiled category matchers over the cleaned
text and emits one Signal per term found, carrying the span so the suppression
pass can drop terms that sit inside longer ones.

Hashtags and emoji are matched against the lists the cleaner extracted rather
than against the prose, so a hashtag scores as a hashtag (0.7) and not as an
exact term (1.0) — the weight difference is the point of separating them.
"""
from __future__ import annotations

from ..contracts import CleanBio, Signal
from ..lexicon import CATEGORIES
from ..matching import dedupe, find_category_signals


class InterestAgent:
    """Stateless — one instance is shared by every worker."""

    def __init__(self):
        # Reverse index built once: which categories claim a given hashtag/emoji.
        # Cheaper than scanning every category's lists per profile, and several
        # categories legitimately share one (🧘 is both Fitness and Wellness).
        self._by_hashtag: dict[str, list[str]] = {}
        self._by_emoji: dict[str, list[str]] = {}
        for category, groups in CATEGORIES.items():
            for tag in groups.get("hashtags", []):
                self._by_hashtag.setdefault(tag.lower(), []).append(category)
            for emoji in groups.get("emojis", []):
                self._by_emoji.setdefault(emoji, []).append(category)

    def run(self, clean: CleanBio) -> list[Signal]:
        signals = find_category_signals(clean.text)

        for tag in clean.hashtags:
            for category in self._by_hashtag.get(tag, []):
                signals.append(Signal(subject=category, kind="hashtag",
                                      evidence=f"#{tag}"))

        for emoji in clean.emojis:
            for category in self._by_emoji.get(emoji, []):
                signals.append(Signal(subject=category, kind="emoji",
                                      evidence=emoji))

        return dedupe(signals)
