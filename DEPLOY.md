# Deploying meetup-bot

Two supported shapes: **local on Windows** (a real desktop, a visible browser —
what most of this was built against) and **Docker on an Ubuntu Desktop VM** (Chromium opens
headed on the VM's own desktop — or runs headless).

They are genuinely different: the desktop-cursor features in
`backend/automation/cursor.py` need a Windows desktop to drive and are no-ops in
a container. Everything else runs on Playwright, which is headless-native.

---

## Local (Windows)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

copy .env.example .env      # then edit it
python -m backend.main
```

Open <http://127.0.0.1:4200>.

MinIO still has to be reachable. Either point `MINIO_URL` at an existing server
or run just that service:

```powershell
docker compose up -d minio
```

### Running it as a service

`uvicorn` under NSSM or Task Scheduler is enough — there is no worker pool to
manage. Point it at the venv's Python and set the working directory to the repo
root, or the relative `.env` lookup in `config.py` won't find the file:

```
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 4200
```

Do **not** run it headless on Windows if you want the desktop-cursor path — it
needs an interactive session, which a service run under `LocalSystem` does not
have.

---

## Docker on an Ubuntu Desktop VM (headed on the VM desktop, or headless)

```bash
# on the VM (Ubuntu Desktop with Docker + the compose plugin installed)
git clone https://github.com/shahjhanconsforc-alt/meetup.git && cd meetup
cp .env.example .env         # MINIO_USER and MINIO_PASSWORD (+ Meetup creds)
docker compose up -d --build
```

**Headed (default).** The container has no screen of its own: the VM desktop's
X socket (`/tmp/.X11-unix`) is mounted in and `DISPLAY` (default `:0`) points
at it, so every Chromium window the bot opens appears on the VM's desktop. The
container runs as root, so the desktop must let root draw on it — run this
once per desktop login, in a terminal on the VM desktop:

```bash
xhost +SI:localuser:root
```

To make that permanent, add it as a GNOME *Startup Application*
(command: `xhost +SI:localuser:root`). If the desktop's display is not `:0`
(`echo $DISPLAY` in a desktop terminal), set `DISPLAY=` in `.env`. After a
reboot the container waits for the desktop's X display to appear (log in to
the desktop) before starting the app.

**Headless.** No desktop, or nothing to watch: set in `.env`

```
DISPLAY_MODE=headless
PLAYWRIGHT_HEADLESS=true
```

| service | port | what |
|---|---|---|
| app | 4200 | the UI and API |
| minio | 9000 | S3 API |
| minio | 9001 | web console |

Host ports are overridable in `.env` (`PORT`, `MINIO_API_PORT`,
`MINIO_CONSOLE_PORT`). To use an existing shared MinIO instead of the bundled
one, add a `docker-compose.override.yml` that sets the app's `MINIO_URL` and
`depends_on: !reset []`, and puts `minio` under a profile.

Compose reads `MINIO_USER` / `MINIO_PASSWORD` from `.env` and uses them for
**both** the MinIO server and the app, so they are set in one place. Both are
`:?`-guarded — compose refuses to start rather than silently booting MinIO with
default credentials.

Chromium runs as root in the container, so the image sets
`PLAYWRIGHT_NO_SANDBOX=true` (Chromium will not start as root with its sandbox).
Stale Chrome profile locks left by a previous container are cleared on start.

The base image is pinned to the same Playwright version as `requirements.txt`
(`v1.49.1-jammy`) so the library and the bundled browsers can't drift apart.
**Change both together or neither.**

Logs: `docker compose logs -f app`.

### Without Docker on the Ubuntu desktop

```bash
# as the normal desktop user — NOT root (Chrome refuses to run as root)
sudo apt install -y python3-venv
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
sudo .venv/bin/python -m playwright install-deps chromium
cp .env.example .env      # PLAYWRIGHT_CHANNEL=chromium, HOST=0.0.0.0
python -m backend.main    # from a terminal on the desktop, so DISPLAY is set
```

### The one volume that matters

```yaml
volumes:
  - app_data:/app/backend/data
```

That directory holds the logged-in Chrome profiles. Losing it means every account
logging in again from what Meetup sees as an unfamiliar device — which is exactly
the situation that produces email codes and captchas. Back it up, or use
`POST /api/minio/sessions/{id}/backup` to snapshot a profile into MinIO first.

### What doesn't work in a container

- **Desktop cursor / address bar** (`cursor.navigate_via_address_bar`,
  `type_desktop`). No desktop. They log and return `False`; nothing else breaks.
- **Real Google Chrome.** The image uses Playwright's bundled Chromium
  (`PLAYWRIGHT_CHANNEL=chromium`).

---

## Configuration

Everything is read in `backend/config.py` and nowhere else — except the `MINIO_*`
vars, which `services/storage/minio_client.py` reads from the same `.env` so it
also works when run as a standalone script.

| var | default | notes |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `4200` | 4000 and 4100 belong to the sibling bots |
| `ENV` | `development` | shown on `/api/status` |
| `MU_USERNAME` / `MU_PASSWORD` | — | the default profile's login |
| `PLAYWRIGHT_HEADLESS` | `false` | `true` for a headless run |
| `PLAYWRIGHT_CHANNEL` | `chrome` | `chromium` in Docker |
| `PLAYWRIGHT_CLEAN_LAUNCH` | `true` | strips the automation infobar + `navigator.webdriver` |
| `PLAYWRIGHT_NO_SANDBOX` | `false` | `true` in Docker (Chromium runs as root) |
| `DISPLAY_MODE` | `host` | Docker only: `host` = VM desktop, `headless` = no window |
| `DISPLAY` | `:0` | Docker only: the VM desktop's X display |
| `PROXY_SERVER` | — | one proxy, applied at launch, **never rotated** |
| `MINIO_URL` | `localhost:9000` | `minio:9000` inside compose |
| `MINIO_BUCKET*` | `meetup-*` | S3 names can't contain underscores |
| `CAT_WORKERS` | `4` | pipeline threads |
| `CAT_RECLASSIFY` | `0` | re-classify members that already have a verdict |

Operator-tunable behaviour (pacing, per-run caps) is **not** here — it lives in
`backend/settings.py` and is editable from the Settings panel. The split is
deliberate: everything in `.env` needs a restart, everything in Settings is safe
to change mid-run.

---

## Backups

The MinIO tables are the irreplaceable part — a scrape costs hours of
rate-limited browsing to redo.

```bash
# dump both tables out of the bucket
docker compose exec minio mc cp local/meetup-members/members.txt /tmp/
```

Or from the app: `GET /api/data/export?what=members` and `?what=categories`
stream the same TSV the stores hold. Dated dumps go in `_minio_backup/`, and
`Categorization_Agents_Pipeline/ingest.py:from_backup()` reads them straight back
in — the format round-trips exactly, including bios containing newlines.

---

## Health checks

| endpoint | tells you |
|---|---|
| `GET /api/status` | server, store, browser |
| `GET /api/minio/health` | MinIO up/down (cached ~5s; `?force=true` to skip) |
| `GET /api/minio/buckets` | object counts and sizes per bucket |
| `GET /api/automation/probe` | **which Meetup selectors still match** |

That last one is the first thing to run when a scrape starts returning nothing.
It separates "Meetup changed their markup" from "we're logged out" in one call,
instead of leaving it to be guessed at from an empty result.

---

## Upgrading

1. Stop the app (`docker compose down`, keeping volumes).
2. Pull, then check `requirements.txt` — if `playwright` moved, the Dockerfile's
   base image tag must move with it.
3. `docker compose up -d --build`.

Settings migrate themselves on boot: a stored value still sitting on a
*superseded* default moves to the current one and logs the change
(`settings.SUPERSEDED`). Values an operator actually chose are never touched.
