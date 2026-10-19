"""Data Loader Agent — the member store to validated Profiles.

The only agent that reads from storage. It also decides what is worth
classifying at all, which is why the numbers it drops are reported rather than
silently skipped: "we processed 400 of 12,000" is a fact the operator needs, and
without the breakdown it looks like a bug rather than a store that is mostly
handles with no bio yet.
"""
from __future__ import annotations

from .. import config
from ..contracts import Profile


class DataLoaderAgent:
    """Reads the member store once and hands out Profiles."""

    def __init__(self, source: str = "bio"):
        self.source = source
        self.stats = {"read": 0, "no_bio": 0, "too_short": 0,
                      "already_done": 0, "loaded": 0}

    def load(self, limit: int | None = None) -> list[Profile]:
        from services.storage import meetup_member_storage as members

        rows = members.load_all()
        self.stats["read"] = len(rows)

        done: set[str] = set()
        if not config.RECLASSIFY:
            from services.storage import meetup_category_storage as cats

            done = cats.categorized()

        out: list[Profile] = []
        for row in rows:
            handle = str(row.get("username") or "").strip().lstrip("@")
            if not handle:
                continue
            if handle.lower() in done:
                self.stats["already_done"] += 1
                continue

            bio = str(row.get("bio") or "").strip()
            if not bio or bio.lower() == config.UNKNOWN_BIO:
                # The store's "profile never opened" sentinel. A member can still
                # be classified from their group slugs alone, so this is only a
                # drop when there is no group either.
                if not str(row.get("groups") or "").strip():
                    self.stats["no_bio"] += 1
                    continue
                bio = ""

            profile = Profile(
                username=handle,
                bio=bio,
                name=str(row.get("name") or ""),
                location=str(row.get("location") or ""),
                groups=str(row.get("groups") or ""),
            )
            # Length is measured on `searchable`, not `bio`: a member with no bio
            # but three group memberships has plenty to go on, and measuring the
            # bio alone would drop exactly the members the group signal exists for.
            if len(profile.searchable) < config.MIN_BIO_CHARS:
                self.stats["too_short"] += 1
                continue

            out.append(profile)
            if limit and len(out) >= limit:
                break

        self.stats["loaded"] = len(out)
        return out

    def report(self) -> dict:
        return dict(self.stats)
