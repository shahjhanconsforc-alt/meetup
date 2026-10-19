"""Bio Cleaning Agent — one Profile into the normalized form matchers run on.

Extracts rather than strips. Emoji, hashtags and mentions are each their own kind
of evidence with their own weight (see config.WEIGHTS), so throwing them away
during cleaning would throw away signal — but leaving them inline would let a
hashtag match as if it were prose, which is a different and stronger claim than
it deserves.
"""
from __future__ import annotations

import re

from ..contracts import CleanBio, Profile
from ..matching import normalize

_HASHTAG = re.compile(r"#(\w{2,40})")
_MENTION = re.compile(r"@(\w{2,40})")
_URL = re.compile(r"https?://\S+|www\.\S+")

# Emoji live in a handful of Unicode blocks. A range check rather than a library:
# the pipeline has no dependency beyond the standard library on purpose, and this
# is accurate enough for evidence weighted at 0.45.
_EMOJI_RANGES = (
    (0x1F300, 0x1FAFF),   # symbols, pictographs, supplemental
    (0x2600, 0x27BF),     # misc symbols + dingbats
    (0x1F000, 0x1F0FF),   # mahjong, cards
    (0x2190, 0x21FF),     # arrows
)

# Language guesses from character blocks alone. Not a real detector — it answers
# "should a Latin-alphabet lexicon even be applied to this?", which is the only
# question the pipeline actually asks of it.
_SCRIPTS = (
    ("ar", (0x0600, 0x06FF)),
    ("ru", (0x0400, 0x04FF)),
    ("el", (0x0370, 0x03FF)),
    ("he", (0x0590, 0x05FF)),
    ("hi", (0x0900, 0x097F)),
    ("th", (0x0E00, 0x0E7F)),
    ("ja", (0x3040, 0x30FF)),
    ("ko", (0xAC00, 0xD7AF)),
    ("zh", (0x4E00, 0x9FFF)),
)


def _is_emoji(char: str) -> bool:
    point = ord(char)
    return any(low <= point <= high for low, high in _EMOJI_RANGES)


def _language(text: str) -> str:
    """A rough script-based guess. "und" when it can't tell, "en" for Latin.

    "en" is a stand-in for "Latin alphabet", not a real claim about English — the
    lexicon is English, and what matters downstream is only whether applying it
    makes sense.
    """
    if not text:
        return "und"
    counts: dict[str, int] = {}
    latin = 0
    for char in text:
        point = ord(char)
        if char.isascii() and char.isalpha():
            latin += 1
            continue
        for code, (low, high) in _SCRIPTS:
            if low <= point <= high:
                counts[code] = counts.get(code, 0) + 1
                break
    if counts:
        best = max(counts, key=counts.get)
        # Only call it a non-Latin language if that script actually dominates —
        # one Japanese character in an English bio is decoration, not a language.
        if counts[best] > latin:
            return best
    return "en" if latin else "und"


class BioCleaningAgent:
    """Stateless — one instance is shared by every worker."""

    def run(self, profile: Profile) -> CleanBio:
        raw = profile.bio or ""
        # Match against the bio AND the group slugs: a member with no bio is the
        # common case on Meetup, and their groups are what makes them classifiable.
        source = profile.searchable

        hashtags = [h.lower() for h in _HASHTAG.findall(source)]
        mentions = [m.lower() for m in _MENTION.findall(source)]
        emojis = [c for c in source if _is_emoji(c)]

        # URLs go before normalization: a link's path is full of words that would
        # otherwise match as if the member had written them.
        stripped = _URL.sub(" ", source)
        # Mentions too — "@berlin_running_club" is a handle, not a claim about
        # this member, and it would otherwise fire both a location and a sport.
        stripped = _MENTION.sub(" ", stripped)

        text = normalize(stripped)

        return CleanBio(
            username=profile.username,
            raw=raw,
            text=text,
            tokens=[t for t in re.split(r"[^\w]+", text) if t],
            emojis=emojis,
            hashtags=hashtags,
            mentions=mentions,
            language=_language(raw),
        )
