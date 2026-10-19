"""Matching primitives — compiled once, shared by every agent and worker.

Two jobs:

1. Build word-boundary regexes from the lexicon. Compiled at import, not per
   profile: there are a few hundred terms and tens of thousands of bios, so
   recompiling per profile would dominate the entire run.

2. Resolve overlaps. This is the part that matters. Terms sit inside other terms
   — "running" inside "trail running", "berlin" inside "berlin marathon", "art"
   inside "martial arts" — and a naive matcher fires both, so a bio saying
   "martial arts" produces an Arts signal it never earned. `suppress_contained`
   drops any match whose span is inside a longer one.
"""
from __future__ import annotations

import re
import unicodedata

from .contracts import Signal
from .lexicon import ENTITIES, all_terms

# Emoji don't have word boundaries, so \b would never match one. They are
# searched as plain substrings instead — see `_find_plain`.
_EMOJI_KINDS = {"emoji"}


def _boundary(term: str) -> re.Pattern:
    """A case-insensitive whole-word matcher for `term`.

    \\b on both sides is what stops "ai" matching inside "email" and "art"
    inside "start" — the single most common source of nonsense verdicts before
    this existed. Multi-word phrases allow any run of whitespace between words,
    so a line break inside a bio doesn't hide a phrase.
    """
    parts = [re.escape(word) for word in term.split()]
    return re.compile(r"\b" + r"\s+".join(parts) + r"\b", re.IGNORECASE)


def _compile_categories() -> dict[str, list[tuple[str, str, re.Pattern | None]]]:
    out: dict[str, list[tuple[str, str, re.Pattern | None]]] = {}
    for category, rows in all_terms().items():
        compiled = []
        for kind, term in rows:
            if kind in _EMOJI_KINDS:
                compiled.append((kind, term, None))       # substring search
            elif kind == "hashtag":
                compiled.append((kind, term, re.compile(r"#" + re.escape(term) + r"\b",
                                                        re.IGNORECASE)))
            else:
                compiled.append((kind, term, _boundary(term)))
        out[category] = compiled
    return out


def _compile_entities() -> dict[str, list[tuple[str, re.Pattern]]]:
    out: dict[str, list[tuple[str, re.Pattern]]] = {}
    for facet, values in ENTITIES.items():
        # Longest first so the longest match is found first and the shorter one
        # inside it is suppressed rather than competing with it.
        out[facet] = [(value, _boundary(value))
                      for value in sorted(values, key=len, reverse=True)]
    return out


CATEGORY_MATCHERS = _compile_categories()
ENTITY_MATCHERS = _compile_entities()


def normalize(text: str) -> str:
    """The lower-cased, accent-folded form every matcher runs against.

    Folding accents is what lets "München" match "munchen" and "Zürich" match
    "zurich" — Meetup bios are written by people typing their own city name in
    their own keyboard layout, and the lexicon can't hold every spelling.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    # Collapse whitespace so a phrase spanning a line break still matches.
    return re.sub(r"\s+", " ", stripped).strip().lower()


def find_category_signals(text: str) -> list[Signal]:
    """Every category term present in `text`, as Signals with their spans."""
    signals: list[Signal] = []
    for category, rows in CATEGORY_MATCHERS.items():
        for kind, term, pattern in rows:
            if pattern is None:
                index = text.find(term)
                if index >= 0:
                    signals.append(Signal(subject=category, kind=kind,
                                          evidence=term, span=None))
                continue
            match = pattern.search(text)
            if match:
                signals.append(Signal(subject=category, kind=kind,
                                      evidence=match.group(0),
                                      span=match.span()))
    return signals


def find_entity_signals(text: str) -> list[Signal]:
    """Every named entity in `text`, as Signals carrying their facet."""
    signals: list[Signal] = []
    for facet, rows in ENTITY_MATCHERS.items():
        for value, pattern in rows:
            match = pattern.search(text)
            if match:
                signals.append(Signal(subject=value, kind="entity",
                                      evidence=match.group(0), facet=facet,
                                      span=match.span()))
    return signals


def suppress_contained(signals: list[Signal]) -> list[Signal]:
    """Drop any signal whose span sits inside a longer signal's span.

    The whole reason this module exists. "Berlin" inside "Berlin Marathon" is not
    a second, independent piece of evidence — it is the same words counted twice,
    and counting it twice is exactly what pushes a wrong category over the
    threshold.

    Signals with no span (emoji, hashtags, implied) are always kept: they have no
    position, so containment is meaningless for them.
    """
    positioned = [s for s in signals if s.span]
    unpositioned = [s for s in signals if not s.span]

    # Longest span first, so a shorter one is always compared against a longer
    # one already accepted.
    positioned.sort(key=lambda s: s.span[1] - s.span[0], reverse=True)

    kept: list[Signal] = []
    for signal in positioned:
        start, end = signal.span
        contained = any(
            other.span[0] <= start and end <= other.span[1]
            and other.span != signal.span
            for other in kept
        )
        if not contained:
            kept.append(signal)

    # Restore document order — a verdict's evidence reads better in the order it
    # appears in the bio than in length order.
    kept.sort(key=lambda s: s.span[0])
    return kept + unpositioned


def dedupe(signals: list[Signal]) -> list[Signal]:
    """One signal per (subject, kind, evidence).

    A term appearing three times in a bio is emphasis, not three independent
    pieces of evidence — and scoring it as three is how a single repeated word
    outvotes genuine corroboration from two different kinds of signal.
    """
    seen: set[tuple] = set()
    out: list[Signal] = []
    for signal in signals:
        key = (signal.subject, signal.kind, signal.evidence.lower())
        if key not in seen:
            seen.add(key)
            out.append(signal)
    return out
