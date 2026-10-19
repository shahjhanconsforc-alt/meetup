# Categorization Agents Pipeline

Turns scraped Meetup bios into structured categories, tags and entities.

There is **no model call anywhere in here**. The whole thing is a hand-written
lexicon plus regex, which is why it classifies tens of thousands of bios in
seconds, costs nothing to run, and gives the same answer twice. The store it
writes decides who gets messaged, so being able to point at the exact term that
produced a verdict — and delete that term when it turns out to be wrong — is
worth more than the recall a model would add.

## Shape

```
                     Data Loader Agent          members.txt (MinIO)
                            │
                    ┌───────▼───────┐
                    │     queue     │
                    └───────┬───────┘
      ┌──────────┬──────────┼──────────┬──────────┐
   Worker 1   Worker 2   Worker 3   Worker 4  …  Worker N
      │
      ├─ Bio Cleaning Agent      normalize, pull out emoji/hashtags
      ├─ Interest Agent          category terms      -> Signals
      ├─ Entity Agent            places, jobs, orgs  -> Signals
      ├─ (suppress overlaps)     "art" inside "martial arts" is dropped
      ├─ Entity Agent .imply()   weak category signals from entities
      ├─ Confidence Agent        weight + diversity  -> scores
      ├─ Classifier Agent        scores              -> categories
      └─ Tagger Agent            signals             -> tags
                            │
                     Database Writer Agent      members_categories.txt (MinIO)
```

## The idea

A **Signal** is one piece of evidence for one conclusion. Every agent in the
middle emits them, and a category is only kept when enough *independent kinds* of
signal support it.

That last part is the point. A bio saying "football" three times is one
observation repeated. "football" + a team name + ⚽ is three different people-
writing-things, and it should win. `config.DIVERSITY_BONUS` is what makes it.

Weights are in `config.py`, ordered by how much a human actually committed to
saying the thing:

| kind | weight | why |
|---|---|---|
| `phrase` | 1.2 | multi-word, so unambiguous — "product manager" means one thing |
| `exact` | 1.0 | a lexicon term on a word boundary |
| `entity` | 0.9 | a named place, org, team or brand |
| `group` | 0.8 | the member was found in a group whose name matched |
| `hashtag` | 0.7 | self-declared, but people hashtag aspirationally |
| `emoji` | 0.45 | suggestive, rarely decisive |
| `implied` | 0.35 | inferred from another signal, not from the text |

`implied` is deliberately low enough that three implied signals still can't clear
`KEEP_THRESHOLD` alone — an inferred category has to be corroborated by something
the member actually wrote.

## Two things that are easy to get wrong

**Overlapping terms.** "running" sits inside "trail running"; "berlin" sits inside
"berlin marathon"; "art" sits inside "martial arts". A naive matcher fires both
and the short one becomes free evidence for a category nobody earned.
`matching.suppress_contained` drops any match whose span is inside a longer one —
and it has to run *after* extraction but *before* inference, which is why
`pipeline.process` orders those three steps explicitly.

**Members with no bio.** On Meetup most profiles are blank. Rather than dropping
them, `Profile.searchable` folds in the slugs of the groups they were found in —
someone in `berlin-hiking-club` is telling you something even with an empty
profile. Without this the pipeline would only ever classify the small minority
who wrote an intro.

## Running it

```bash
python -m Categorization_Agents_Pipeline                  # classify what's new
python -m Categorization_Agents_Pipeline --workers 8
python -m Categorization_Agents_Pipeline --dry-run        # classify, write nothing
python -m Categorization_Agents_Pipeline --reclassify     # redo everyone
python -m Categorization_Agents_Pipeline --explain alice  # why did she get that?
```

`--explain` is the one to reach for when a verdict looks wrong. It prints the
literal term that fired for each category, so a bad lexicon entry is found by
reading rather than by bisecting weights:

```
@alice
  bio:        'Backend engineer, mostly Python and Postgres. Climbing on weekends.'
  categories: ['Tech', 'Outdoors']
  evidence:
    Tech         exact    'engineer' (+1.0)
    Tech         exact    'python' (+1.0)
    Outdoors     exact    'climbing' (+1.0)
```

Or from the app: `POST /api/data/categorize`, which runs the same thing off the
event loop.

## Editing the lexicon

`lexicon.py` is the knowledge. Adding a category means adding one entry to
`CATEGORIES` with its terms, phrases, hashtags and emoji — nothing else in the
pipeline needs to change.

Two rules worth keeping:

- **Short terms are dangerous.** Anything under four characters ("ai", "art",
  "run") will match inside other words. The matchers use `\b` boundaries, but
  that doesn't help with "ai" in "aikido". Prefer a phrase.
- **Test with `--dry-run` first.** A weight or threshold change re-labels the
  entire store, and `--dry-run` shows you the damage before you do it.

## Concurrency, honestly

The per-profile work is regex over a short string and takes well under a
millisecond, and CPython holds the GIL through `re`. The threads here buy
pipeline shape and overlapped store I/O — **not** a linear speedup. The run is
dominated by the two MinIO round-trips at either end. The worker count is real
and worth raising if the lexicon grows or an agent later does I/O of its own, but
it is not a promise of N-times throughput.
