"""The vocabulary — what words mean what.

This file IS the classifier's knowledge. There is no model: a category is a set
of terms, phrases, hashtags and emoji, and a member gets a category when their
bio provides enough independent evidence from that set.

Deliberately hand-written rather than learned. The store it feeds is used to
decide who gets messaged, so being able to point at the exact term that produced
a verdict — and delete it when it turns out to be wrong — is worth more than the
recall a model would add.

Meetup-shaped in two ways worth knowing:

  * The categories mirror how Meetup groups actually cluster (tech, outdoors,
    fitness, business, arts, social), because the group slug a member was found
    in is itself a signal and those slugs use this vocabulary.
  * TERMS lean towards what people put in a Meetup profile — occupation, city,
    what they turn up for — rather than the follower-bait a social bio carries.
"""
from __future__ import annotations

# ── Categories ────────────────────────────────────────────────────
# category -> {terms, phrases, hashtags, emojis}
#
# `phrases` are multi-word and score higher (see config.WEIGHTS) because length
# buys unambiguity: "product manager" can only mean one thing, "product" cannot.

CATEGORIES: dict[str, dict[str, list[str]]] = {
    "Tech": {
        "terms": ["developer", "engineer", "programmer", "coder", "devops", "sre",
                  "python", "javascript", "typescript", "rust", "golang", "java",
                  "kubernetes", "docker", "aws", "backend", "frontend", "fullstack",
                  "data", "ml", "ai", "cybersecurity", "infosec", "hacker",
                  "blockchain", "web3", "linux", "opensource"],
        "phrases": ["software engineer", "software developer", "data scientist",
                    "machine learning", "web developer", "site reliability",
                    "platform engineer", "security researcher", "open source"],
        "hashtags": ["dev", "coding", "python", "devops", "webdev", "tech", "ai"],
        "emojis": ["💻", "⌨️", "🖥️", "🤖", "🐍"],
    },
    "Business": {
        "terms": ["founder", "cofounder", "entrepreneur", "startup", "ceo", "cto",
                  "investor", "vc", "consultant", "freelance", "saas", "b2b",
                  "marketing", "sales", "growth", "product", "strategy", "finance",
                  "fintech", "ecommerce"],
        "phrases": ["product manager", "business owner", "angel investor",
                    "venture capital", "seed stage", "small business",
                    "growth marketing", "side hustle"],
        "hashtags": ["startup", "founder", "entrepreneur", "business", "saas"],
        "emojis": ["📈", "💼", "🚀"],
    },
    "Outdoors": {
        "terms": ["hiking", "hiker", "trekking", "camping", "backpacking",
                  "climbing", "bouldering", "mountaineering", "kayaking",
                  "surfing", "skiing", "snowboarding", "trail", "nature",
                  "wildlife", "birding", "sailing"],
        "phrases": ["trail running", "rock climbing", "mountain biking",
                    "national park", "great outdoors", "wild camping"],
        "hashtags": ["hiking", "outdoors", "camping", "climbing", "nature"],
        "emojis": ["🥾", "⛰️", "🏔️", "🏕️", "🧗", "🌲"],
    },
    "Fitness": {
        "terms": ["running", "runner", "marathon", "triathlon", "cycling",
                  "cyclist", "gym", "crossfit", "weightlifting", "powerlifting",
                  "yoga", "pilates", "swimming", "boxing", "martial", "jiujitsu",
                  "bjj", "football", "soccer", "basketball", "tennis", "padel",
                  "badminton", "cricket", "rugby", "volleyball"],
        "phrases": ["half marathon", "personal trainer", "strength training",
                    "martial arts", "five a side"],
        "hashtags": ["running", "fitness", "gym", "yoga", "cycling"],
        "emojis": ["🏃", "🚴", "🏋️", "🧘", "⚽", "🏀", "🎾", "🥊"],
    },
    "Arts": {
        "terms": ["artist", "painter", "illustrator", "designer", "photographer",
                  "photography", "musician", "guitarist", "drummer", "singer",
                  "producer", "dj", "writer", "author", "poet", "filmmaker",
                  "actor", "theatre", "dance", "ceramics", "pottery"],
        "phrases": ["graphic designer", "creative director", "film photography",
                    "live music", "visual artist", "makeup artist"],
        "hashtags": ["art", "photography", "music", "design", "writing"],
        "emojis": ["🎨", "📷", "🎸", "🎧", "🎭", "✍️", "🎬"],
    },
    "Food": {
        "terms": ["foodie", "chef", "cook", "cooking", "baking", "baker",
                  "vegan", "vegetarian", "coffee", "barista", "wine", "beer",
                  "brewing", "whisky", "cocktails", "sommelier"],
        "phrases": ["home cook", "craft beer", "wine tasting", "supper club",
                    "specialty coffee"],
        "hashtags": ["foodie", "coffee", "vegan", "baking", "wine"],
        "emojis": ["🍳", "☕", "🍷", "🍺", "🥐", "🍜"],
    },
    "Social": {
        "terms": ["boardgames", "dnd", "tabletop", "trivia", "karaoke", "language",
                  "exchange", "expat", "newcomer", "networking", "volunteer",
                  "community", "book", "bookclub", "movie", "film"],
        "phrases": ["board games", "book club", "language exchange",
                    "meet new people", "social club", "pub quiz"],
        "hashtags": ["boardgames", "bookclub", "community", "meetup"],
        "emojis": ["🎲", "📚", "🎤", "🍻"],
    },
    "Wellness": {
        "terms": ["meditation", "mindfulness", "wellness", "therapy", "coach",
                  "coaching", "breathwork", "reiki", "holistic", "mentalhealth",
                  "sober", "recovery"],
        "phrases": ["mental health", "life coach", "sound bath", "self care"],
        "hashtags": ["mindfulness", "wellness", "meditation", "selfcare"],
        "emojis": ["🧘", "🕯️", "🌱"],
    },
    "Travel": {
        "terms": ["travel", "traveller", "traveler", "backpacker", "nomad",
                  "wanderlust", "expat", "roadtrip"],
        "phrases": ["digital nomad", "solo travel", "road trip",
                    "travelled the world"],
        "hashtags": ["travel", "nomad", "wanderlust"],
        "emojis": ["✈️", "🌍", "🗺️", "🎒"],
    },
}

# ── Entities ──────────────────────────────────────────────────────
# Named things, by facet. Matched case-insensitively on word boundaries, and
# longest-first so "New York" wins over "York" — see matching.suppress_contained.

ENTITIES: dict[str, list[str]] = {
    # Cities Meetup is dense in. Not exhaustive — a location the lexicon misses
    # is simply not extracted, which is better than a wrong one.
    "loc": ["london", "manchester", "birmingham", "edinburgh", "glasgow", "dublin",
            "berlin", "munich", "hamburg", "cologne", "frankfurt", "amsterdam",
            "rotterdam", "paris", "lyon", "madrid", "barcelona", "lisbon", "porto",
            "rome", "milan", "vienna", "zurich", "geneva", "copenhagen", "stockholm",
            "oslo", "helsinki", "warsaw", "prague", "budapest", "athens",
            "new york", "brooklyn", "san francisco", "los angeles", "seattle",
            "austin", "chicago", "boston", "denver", "portland", "miami",
            "toronto", "vancouver", "montreal", "sydney", "melbourne", "auckland",
            "singapore", "tokyo", "seoul", "bangalore", "mumbai", "delhi",
            "dubai", "cape town", "nairobi", "lagos", "accra"],

    "job": ["engineer", "developer", "designer", "founder", "manager", "consultant",
            "teacher", "lecturer", "researcher", "analyst", "accountant", "lawyer",
            "doctor", "nurse", "therapist", "architect", "recruiter", "student",
            "phd", "photographer", "chef", "barista", "trainer"],

    "sport": ["football", "soccer", "basketball", "tennis", "padel", "badminton",
              "cricket", "rugby", "volleyball", "running", "cycling", "swimming",
              "climbing", "bouldering", "yoga", "boxing", "bjj", "crossfit",
              "hiking", "skiing", "snowboarding", "surfing", "golf", "netball"],

    "hobby": ["photography", "painting", "drawing", "pottery", "knitting",
              "gardening", "chess", "boardgames", "dnd", "reading", "writing",
              "baking", "cooking", "gaming", "guitar", "piano", "singing",
              "dancing", "salsa", "tango", "birding", "astronomy"],

    "org": ["google", "microsoft", "amazon", "meta", "apple", "netflix", "spotify",
            "stripe", "shopify", "airbnb", "uber", "revolut", "monzo", "deloitte",
            "pwc", "kpmg", "accenture", "ibm", "oracle", "salesforce", "nhs"],
}

# ── Inference ─────────────────────────────────────────────────────
# entity/facet -> categories it implies. Weakest evidence class there is (see
# config.WEIGHTS["implied"]) because it is a guess about a guess: it fires from
# another signal, not from anything the member actually wrote.

IMPLIES: dict[str, list[str]] = {
    "sport": ["Fitness"],
    "hobby": ["Arts"],
    "job": ["Business"],
    "org": ["Business"],
    # A location alone does NOT imply Travel. Everyone lists a home city, and
    # treating that as wanderlust put half the store in Travel — the Tagger
    # promotes Travel only on a MULTI-location bio, which is the real signal.
}

# ── Tags ──────────────────────────────────────────────────────────
# Structured labels a verdict carries alongside its category. A tag is narrower
# than a category and is what the campaign's targeting actually filters on —
# "Tech" is too broad to write a message to, "tech:python" is not.

TAG_PREFIX = {
    "loc": "in",
    "job": "job",
    "sport": "plays",
    "hobby": "into",
    "org": "at",
}


def tag_for(facet: str, value: str) -> str:
    """The tag one extracted entity becomes, e.g. ("loc", "berlin") -> "in:berlin"."""
    prefix = TAG_PREFIX.get(facet)
    if not prefix:
        return ""
    return f"{prefix}:{str(value).strip().lower().replace(' ', '-')}"


def all_terms() -> dict[str, list[tuple[str, str]]]:
    """Every category term flattened to {category: [(kind, term)]} — what the
    Interest agent compiles its matchers from."""
    out: dict[str, list[tuple[str, str]]] = {}
    for category, groups in CATEGORIES.items():
        rows: list[tuple[str, str]] = []
        for kind, key in (("exact", "terms"), ("phrase", "phrases"),
                          ("hashtag", "hashtags"), ("emoji", "emojis")):
            rows += [(kind, term) for term in groups.get(key, [])]
        out[category] = rows
    return out
