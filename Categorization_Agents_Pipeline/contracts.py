"""The records agents hand to each other.

Each agent takes one of these and returns the next. Keeping the hand-offs as
explicit dataclasses (rather than loose dicts) is what makes the agents testable
in isolation: you can hand CleanBio to the Interest agent without a MinIO, a
queue, or any of the agents before it.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Profile:
    """Data Loader Agent output — one validated member record."""
    username: str
    bio: str
    name: str = ""
    location: str = ""
    groups: str = ""

    @property
    def searchable(self) -> str:
        """Bio plus the group names this member was found in.

        A group name is real evidence about a person — someone in
        "berlin-hiking-club" is telling you something even with an empty bio —
        and on Meetup a large fraction of profiles have no bio at all. Folding
        the slugs in is what keeps those members classifiable instead of all
        landing in Unknown.
        """
        slugs = " ".join(str(self.groups or "").replace(",", " ").split("-"))
        return f"{self.bio} {slugs} {self.location}".strip()


@dataclass
class CleanBio:
    """Bio Cleaning Agent output.

    `text` is the normalized, lower-cased form every matcher runs against;
    `raw` is kept so a verdict can be explained against what the member actually
    wrote. The affordances a bio leans on — emoji, hashtags, mentions — are
    pulled out separately rather than stripped, because each is its own kind of
    evidence with its own weight.
    """
    username: str
    raw: str
    text: str
    tokens: list[str] = field(default_factory=list)
    emojis: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    language: str = "und"          # ISO-639-1-ish, "und" when undetermined


@dataclass
class Signal:
    """One piece of evidence for one conclusion.

    Signals are the pipeline's currency: the Interest, Category and Entity agents
    all emit them, and the Confidence Agent scores a conclusion by how many
    INDEPENDENT kinds of signal support it. `kind` is the weight key from config;
    `evidence` is the literal substring that fired, so a verdict can be audited.
    """
    subject: str                   # what this is evidence FOR (a category / entity)
    kind: str                      # exact | phrase | entity | hashtag | emoji | implied
    evidence: str                  # the text that matched
    tag: str | None = None         # the structured tag this yields, if any
    facet: str | None = None       # loc | sport | team | brand | org | job | hobby
    # Where in the cleaned bio this matched. Carried so a shorter term sitting
    # inside a longer one can be suppressed — "Berlin" inside "Berlin Marathon".
    # None for evidence with no position: emoji, hashtags, implied categories.
    span: tuple[int, int] | None = None


@dataclass
class Verdict:
    """The finished record for one member — what the Writer Agent persists."""
    username: str
    bio: str
    categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    entities: dict[str, list[str]] = field(default_factory=dict)
    confidence: dict[str, float] = field(default_factory=dict)
    language: str = "und"

    @property
    def primary(self) -> str:
        """The single best category.

        The store (and the panels reading it) key off ONE category per member, so
        the highest-confidence one is promoted; the full list rides alongside in
        its own column.
        """
        return self.categories[0] if self.categories else "Unknown"

    def as_row(self) -> dict:
        """The shape meetup_category_storage stores."""
        return {
            "username": self.username,
            "bio": self.bio,
            "category": self.primary,
            "categories": list(self.categories),
            "tags": list(self.tags),
            "specifics": list(self.entities.get("hobby", []))
                         + list(self.entities.get("sport", [])),
            "locations": list(self.entities.get("loc", [])),
        }
