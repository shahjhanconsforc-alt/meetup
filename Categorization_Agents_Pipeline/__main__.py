"""Run the pipeline from the command line.

    python -m Categorization_Agents_Pipeline
    python -m Categorization_Agents_Pipeline --workers 8 --limit 500
    python -m Categorization_Agents_Pipeline --dry-run          # classify, write nothing
    python -m Categorization_Agents_Pipeline --explain alice    # why one member got their verdict

Run it here rather than through the app when you are changing the lexicon: the
feedback loop is a second instead of a page reload, and --dry-run lets you see
what a weight change does to the whole store before committing it to one.
"""
from __future__ import annotations

import argparse
import sys

from . import config
from .agents import BioCleaningAgent, ConfidenceAgent
from .contracts import Profile
from .pipeline import CategorizationPipeline


def _progress(done: int, total: int) -> None:
    print(f"  … {done}/{total}", flush=True)


def _explain(handle: str) -> int:
    """Print every signal behind one member's verdict.

    The tool for "why on earth is this person filed as Fitness" — it prints the
    literal term that fired, so a bad lexicon entry is found by reading rather
    than by bisecting the weights.
    """
    from services.storage import meetup_member_storage as members

    row = next((r for r in members.load_all()
                if str(r.get("username") or "").lower() == handle.lower().lstrip("@")),
               None)
    if row is None:
        print(f"No member @{handle} in the store.")
        return 1

    pipe = CategorizationPipeline(workers=1, dry_run=True)
    profile = Profile(username=row["username"], bio=row.get("bio") or "",
                      name=row.get("name") or "", location=row.get("location") or "",
                      groups=row.get("groups") or "")
    verdict = pipe.process(profile)

    clean = BioCleaningAgent().run(profile)
    print(f"@{profile.username}")
    print(f"  bio:        {profile.bio!r}")
    print(f"  groups:     {profile.groups or '—'}")
    print(f"  matched on: {clean.text!r}")
    print(f"  language:   {clean.language}")
    print(f"  categories: {verdict.categories or '— none —'}")
    print(f"  tags:       {verdict.tags or '—'}")
    print(f"  entities:   {verdict.entities or '—'}")
    print("  confidence:")
    for subject, score in sorted(verdict.confidence.items(), key=lambda i: -i[1]):
        mark = "PRIMARY" if score >= config.PRIMARY_THRESHOLD else "kept"
        print(f"    {subject:<12} {score:>5}  ({mark})")

    # Re-derive the signals so the evidence can be shown per subject.
    from . import matching

    interests = pipe.interests.run(clean) + pipe.entities.run(clean)
    signals = matching.suppress_contained(interests)
    signals += pipe.entities.imply(signals)
    confidence = ConfidenceAgent()
    print("  evidence:")
    for subject in verdict.confidence:
        for item in confidence.explain(signals, subject):
            print(f"    {subject:<12} {item['kind']:<8} {item['evidence']!r} "
                  f"(+{item['weight']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="Categorization_Agents_Pipeline",
        description="Classify scraped Meetup bios into categories and tags.")
    parser.add_argument("--workers", type=int, default=0,
                        help=f"worker threads (default {config.DEFAULT_WORKERS})")
    parser.add_argument("--limit", type=int, default=None,
                        help="only process this many profiles")
    parser.add_argument("--dry-run", action="store_true",
                        help="classify but write nothing")
    parser.add_argument("--reclassify", action="store_true",
                        help="include members that already have a verdict")
    parser.add_argument("--explain", metavar="HANDLE",
                        help="show why one member got their verdict, then exit")
    args = parser.parse_args(argv)

    if args.reclassify:
        config.RECLASSIFY = True

    try:
        if args.explain:
            return _explain(args.explain)

        pipe = CategorizationPipeline(workers=args.workers or None,
                                      dry_run=args.dry_run,
                                      progress=_progress)
        summary = pipe.run(limit=args.limit)
    except Exception as exc:  # noqa: BLE001
        # Almost always MinIO. Say which rather than printing a traceback that
        # buries it under the storage layer.
        print(f"Pipeline failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Is MinIO running? Try: docker compose up -d minio", file=sys.stderr)
        return 1

    loader = summary["loader"]
    print()
    print(f"  read         {loader['read']}")
    print(f"  no bio       {loader['no_bio']}")
    print(f"  too short    {loader['too_short']}")
    print(f"  already done {loader['already_done']}")
    print(f"  processed    {summary['processed']}")
    print(f"  written      {summary['written']}"
          + ("  (dry run — nothing saved)" if args.dry_run else ""))
    print(f"  failures     {summary['failure_count']}")
    print(f"  {summary['elapsed_s']}s at {summary['rate_per_s']}/s "
          f"on {summary['workers']} worker(s)")
    for failure in summary["failures"]:
        print(f"    ! {failure}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
