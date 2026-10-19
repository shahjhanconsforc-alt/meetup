"""Tuning for the pipeline — weights, thresholds, worker counts.

Separate from backend/settings.py on purpose: these are not operator knobs. They
are the numbers that decide what the classifier believes, and changing one
re-labels the whole store. They belong with the code that uses them, under
version control, not in a UI where they can be nudged mid-run.
"""
from __future__ import annotations

import os

# ── Concurrency ───────────────────────────────────────────────────
DEFAULT_WORKERS = int(os.getenv("CAT_WORKERS", 4))
MAX_WORKERS = 32

# How many verdicts the writer buffers before pushing to MinIO. The store keeps
# the whole table in one object, so a write per verdict would be a full download
# and upload per member.
WRITE_BATCH = 500

# ── Signal weights ────────────────────────────────────────────────
# What one piece of evidence is worth. The spread is the point: an exact term in
# the bio is strong, an emoji on its own is weak, and something merely IMPLIED by
# another conclusion is weakest — otherwise inference would compound into
# confident nonsense.
WEIGHTS = {
    "exact": 1.0,        # a lexicon term matched on a word boundary
    "phrase": 1.2,       # a multi-word phrase — longer means less ambiguous
    "entity": 0.9,       # a named place, org, team or brand
    "hashtag": 0.7,      # self-declared, but people hashtag aspirationally
    "emoji": 0.45,       # suggestive, rarely decisive
    "group": 0.8,        # the member was found in a group whose name matched
    "implied": 0.35,     # inferred from another signal, not from the text
}

# ── Thresholds ────────────────────────────────────────────────────
# A category needs this much total weight to be kept at all.
KEEP_THRESHOLD = 1.0

# …and this much to be the PRIMARY category. Set above KEEP so a member can
# carry a weak secondary category without it ever being promoted to the one
# column everything else keys off.
PRIMARY_THRESHOLD = 1.4

# Two independent KINDS of evidence beat one strong one. A bio saying "football"
# three times is still one signal; "football" plus a team name plus ⚽ is three,
# and that is what this rewards.
DIVERSITY_BONUS = 0.35

# How many categories a member may carry. Beyond a few it stops being a
# classification and starts being a word list.
MAX_CATEGORIES = 4
MAX_TAGS = 12

# A bio shorter than this is not classified — there is nothing in it to be right
# about, and a guess from three words pollutes the store more than a blank does.
MIN_BIO_CHARS = 12

# ── Loader ────────────────────────────────────────────────────────
# Bios equal to this are the member store's "never read a profile" sentinel, not
# real text. Classifying them would mean classifying the sentinel itself.
UNKNOWN_BIO = "unknown"

# Re-classify members that already have a verdict. Off by default: a full re-run
# over a large store is the expensive case, and the common one is "categorize
# what came in since last time".
RECLASSIFY = os.getenv("CAT_RECLASSIFY", "0").strip().lower() in ("1", "true", "yes")
