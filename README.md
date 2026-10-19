# meetup-bot

A local agent for Meetup: find groups, collect their members, classify those
members from what they wrote, and run targeted outreach — with a browser that
behaves like a person driving it.

FastAPI backend + a static frontend served by the same process, Playwright for
the browser, MinIO for the data, and a dependency-free agent pipeline for the
classification.

> **Scope.** This drives *your own* Meetup account against Meetup's public
> surface. It stores other people's profile text and messages them, so the two
> things that matter most here are the per-run caps in Settings and running
> `--dry-run` before a real send. There is no unsend.

## Architecture

```
meetup-bot/
├─ backend/                    FastAPI app
│  ├─ automation/              the browser, layered
│  │  ├─ playwright_runner.py    owns the Chrome process + persistent profiles
│  │  ├─ typing.py               types key-by-key, with the pauses a hand makes
│  │  ├─ cursor.py               curved pointer paths; OS-level input (Windows)
│  │  ├─ media.py                MinIO image -> a composer's file input
│  │  ├─ visit.py                generic page moves: goto, scroll, links
│  │  ├─ meetup.py               ★ the ONLY file with a Meetup selector in it
│  │  └─ campaign.py             whole jobs: scrape, outreach, RSVP
│  ├─ routes/                  one router per panel — thin, no logic
│  ├─ db/                      store.json + pass-throughs to the MinIO stores
│  ├─ data/                    runtime state (gitignored) + seed.py
│  ├─ accounts.py              credentials and logging in
│  ├─ sessions.py              are we logged in? — read from cookie files
│  ├─ settings.py              the tunable knobs, with defaults in one place
│  ├─ control.py               pause / resume / stop for long runs
│  ├─ tasks.py                 background tasks behind an HTTP request
│  ├─ activity.py              the live event stream
│  └─ main.py                  entrypoint
├─ Categorization_Agents_Pipeline/   bios -> categories, tags, entities
├─ services/storage/           MinIO: members, categories, media, messages
├─ frontend/                   vanilla JS, no build step
└─ _minio_backup/              dated dumps of the MinIO tables
```

### The layering rule

`meetup.py` is the only module that knows what Meetup's DOM looks like.
`campaign.py` knows the *order* of things; the routes know neither. When Meetup
redeploys and a scrape starts returning nothing, exactly one file needs editing —
and `GET /api/automation/probe` tells you which selector died.

### Two stores, on purpose

The members bucket holds two tables that **overlap by design**:

| table | says |
|---|---|
| `members.txt` | who was scraped — every handle ever seen, bio or not |
| `members_categories.txt` | what they are — the pipeline's verdict |

Purging categorized handles out of the first would make it an unreliable record
of what had been scraped: a member would vanish the moment the pipeline reached
them, taking the campaign's pool of targets with them.

## Setup

```bash
python -m venv .venv && .venv\Scripts\activate     # Windows
pip install -r requirements.txt
playwright install chromium

cp .env.example .env         # fill in MU_USERNAME / MU_PASSWORD / MINIO_*
docker compose up -d minio   # or point MINIO_URL at an existing server

python -m backend.main       # http://127.0.0.1:4200
```

Port **4200** — 4000 and 4100 are taken by the Instagram and Threads bots on the
machine these run beside. Two servers can't share a port; the second to start
dies on bind.

Want something on screen straight away:

```bash
python -m backend.data.seed          # sample groups, drafts and members
python -m backend.data.seed --clear  # take them back out
```

## Using it

1. **Accounts** — add a login, then *Log in selected*. Logins run one at a time
   with a randomised gap; a burst of simultaneous logins from one IP is the
   clearest signal that a set of accounts belongs together.
2. **Scraping** — search a keyword to find groups, add the ones worth targeting,
   pick a source, *Start scrape*. Event attendees are a warmer pool than a whole
   roster: they turned up for a specific topic on a specific date.
3. **Data** — *Run categorization* to classify what came in. See
   [the pipeline's README](Categorization_Agents_Pipeline/README.md).
4. **Campaign** — write several drafts, *Preview targets*, **dry run**, then send.

Runs pause and stop on a clean boundary: a run always finishes the member it is
on rather than being killed mid-action and leaving Meetup with a half-submitted
message.

## On not looking like a bot

Not a claim of undetectability — a determined fingerprinter still wins. These
just close the free giveaways:

- **Persistent profiles.** One Chrome profile per account, kept between runs.
  That directory *is* the session; a fresh context means a fresh login and a
  fresh challenge every time.
- **Real typing.** `page.fill()` sets a 400-character message in zero
  milliseconds with no keydown events. `typing.py` types it key by key, with
  longer pauses at sentence boundaries and the occasional backspaced typo.
- **Curved pointer paths.** `element.click()` teleports the cursor and lands dead
  centre. `cursor.py` traces a bezier to a point somewhere inside the target.
- **Randomised pacing.** A constant gap between actions is a metronome, and a
  metronome is the easiest thing to spot in a request log.
- **One proxy, never rotated.** Meetup treats an IP change on a logged-in session
  as a suspicious login, so `PROXY_SERVER` is applied at launch and kept.
- **Stopping on a challenge.** Continuing through a captcha means submitting
  actions that all silently fail — which is what escalates a challenge into a
  block. `stop_on_challenge` is on by default.

## Storage

MinIO holds four buckets (`meetup-members`, `-sessions`, `-media`, `-messages`).
The tables are **tab-separated text**, not JSON, so they can be opened, grepped
and dropped into a spreadsheet. Every field is escaped on write — Meetup bios
routinely contain real line breaks, and a naive one-record-per-line file would
split a bio across several lines and silently corrupt the table.

The app keeps working with MinIO down: health checks return `false` and log it,
and only operations that genuinely need the server raise.

**Credentials note:** account passwords are stored in plaintext in
`backend/data/store.json`. It is gitignored and local-only — a convenience store,
not a vault. Treat that file as a secret.

## Deployment

See [DEPLOY.md](DEPLOY.md). `docker compose up -d --build` brings up the app and
MinIO together; the app runs headless on bundled Chromium there, and the
desktop-cursor features are Windows-only no-ops.
