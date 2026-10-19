# _minio_backup

Dated dumps of the MinIO tables. This is the irreplaceable data: a scrape costs
hours of rate-limited browsing to redo, and there is no way to re-derive it.

## Naming

```
members.<YYYY-MM-DD>.txt                    the scrape's record
members_categories.<YYYY-MM-DD>.txt         the pipeline's verdicts
members.<YYYY-MM-DD>.pre-<what>.txt         taken immediately before a risky change
```

The `pre-` form is the one that matters. Take it before anything that rewrites a
whole table — a lexicon change with `--reclassify`, a bulk import, a `clean()`
run — because all three overwrite in place and there is no undo.

## Making one

```bash
curl -o _minio_backup/members.$(date +%F).txt \
     "http://127.0.0.1:4200/api/data/export?what=members"
curl -o _minio_backup/members_categories.$(date +%F).txt \
     "http://127.0.0.1:4200/api/data/export?what=categories"
```

## Restoring

```python
from Categorization_Agents_Pipeline import ingest
ingest.from_backup("_minio_backup/members.2026-08-07.txt")
```

The format round-trips exactly — every field is escaped, so bios containing real
line breaks survive the trip. Note that `from_backup` **upserts**: it merges into
what is already there rather than replacing it, so restoring an old dump over a
newer table refreshes the rows it holds and leaves the rest alone.

These files carry other people's profile text. They are gitignored by pattern in
the parent `.gitignore`'s spirit — keep them local.
