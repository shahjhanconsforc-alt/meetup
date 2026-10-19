"""FastAPI entrypoint.  Run:  python -m backend.main   (or: uvicorn backend.main:app --reload)"""
import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend import activity, config, db, sessions
from backend.automation import playwright_runner as browser
from backend.routes import (
    accounts_routes,
    automation_routes,
    campaign_routes,
    data_routes,
    minio_routes,
    settings_routes,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    activity.log("server", f"meetup-bot started on {config.HOST}:{config.PORT}", "ok")
    # Sessions expire while we're closed, so trust disk over whatever the store
    # last recorded. Reads cookie files only — no browser, ~5ms per account.
    try:
        sessions.sweep()
    except Exception as exc:  # noqa: BLE001 — never let this stop the server booting
        activity.log("sessions", f"Startup sweep failed: {exc}", "error")
    # The browse results are scratch, not records: they are "what the last browse
    # found", and the last browse belonged to a session that is over. Kept across
    # a restart they came back on screen as a panel full of events nobody in this
    # session collected — under a category this run may never visit. The members
    # they yielded are already in the member store, which is the part worth
    # keeping, so nothing of value goes with them.
    try:
        dropped = db.clear_events()
        if dropped:
            activity.log("scraping", f"Cleared {dropped} event(s) left by the "
                                     f"last session", "info")
    except Exception as exc:  # noqa: BLE001 — never let this stop the server booting
        activity.log("scraping", f"Could not clear the old browse: {exc}", "warn")
    # A stored setting that is still on a SUPERSEDED default moves to the current
    # one. Without this, an install that had ever saved any setting kept the old
    # pacing for ever — the stored copy wins over the default, and it was never a
    # choice anyone made. Deliberate values are untouched.
    try:
        from backend import settings as _settings

        moved = _settings.migrate_stale_defaults()
        for key, (old, new) in moved.items():
            activity.log("config", f"{key}: {old} → {new} (was the old default)", "ok")
    except Exception as exc:  # noqa: BLE001 — a failed migration mustn't stop boot
        activity.log("config", f"Settings migration skipped: {exc}", "warn")
    # Members stored before names were kept on the way in get theirs back off the
    # event rosters, which have held them all along. This is why the Data panel
    # could show a name the Users tab could not: Data reads the roster files,
    # Users reads the member store, and only one of them ever had the name.
    #
    # Free once it has run: with every row named there is nothing to look up and
    # it returns without opening a single roster.
    try:
        filled = db.backfill_member_names()
        if filled["named"]:
            activity.log("scraping", f"Named {filled['named']} stored member(s) "
                                     f"from {filled['rosters']} event roster(s)",
                         "ok")
    except Exception as exc:  # noqa: BLE001 — MinIO down mustn't stop boot
        activity.log("scraping", f"Could not fill in member names: "
                                 f"{type(exc).__name__}", "warn")
    # Print MinIO's running/stopped state to the terminal on boot. Best-effort:
    # a stopped MinIO must never stop the app from starting — is_up() just logs
    # and returns False, and the storage layer stays dormant until it's back.
    try:
        from services.storage import minio_client

        minio_client.is_up()
    except Exception as exc:  # noqa: BLE001
        activity.log("minio", f"MinIO status check skipped: {exc}", "warn")
    yield
    # Teardown. Order matters and both halves are bounded, because "the server
    # won't die on Ctrl+C" is worse than "the server died a second early".
    activity.begin_shutdown()          # let the SSE feed end its own stream
    try:
        # Chrome can be slow or wedged, and an unbounded close means an
        # unbounded shutdown. A leaked process is recoverable; a hung terminal
        # gets killed, which is how a profile ends up half-written.
        await asyncio.wait_for(browser.shutdown(), timeout=10.0)
    except asyncio.TimeoutError:
        activity.log("browser", "Browser did not close within 10s — leaving it",
                     "warn")
    except Exception as exc:  # noqa: BLE001
        activity.log("browser", f"Shutdown was not clean: {type(exc).__name__}",
                     "warn")


app = FastAPI(title="meetup-bot agent", version="1.0.0", lifespan=lifespan)

# Which panel a given endpoint belongs to — so the Activity feed reads in the
# user's terms ("accounts") rather than the router's ("/api/accounts/login").
_SOURCES = {
    "data": "data",
    "automation": "browser",
    "campaign": "campaign",
    "accounts": "accounts",
    "settings": "config",
    "minio": "minio",
}

# Chatty endpoints the panels poll on a timer. Logging these would bury the
# real events under a stream of health checks.
_QUIET = {"/api/automation/status", "/api/automation/runs", "/api/accounts",
          "/api/minio/health", "/api/minio/logs", "/api/data/status"}


@app.middleware("http")
async def log_requests(request, call_next):
    path = request.url.path
    # never log the activity endpoints themselves — that feeds back on itself
    if not path.startswith("/api/") or path.startswith("/api/data/activity"):
        return await call_next(request)

    started = time.perf_counter()
    response = await call_next(request)
    ms = round((time.perf_counter() - started) * 1000)

    quiet = path in _QUIET and response.status_code < 400
    # A task poll is the chattiest thing here and its path carries an id, so it
    # can't be listed by name — matched by prefix instead.
    quiet = quiet or (path.startswith("/api/automation/tasks/")
                      and response.status_code < 400)
    if quiet and request.method == "GET":
        return response

    parts = path.split("/")
    source = _SOURCES.get(parts[2] if len(parts) > 2 else "", "http")
    level = "error" if response.status_code >= 500 else "warn" if response.status_code >= 400 else "ok"
    activity.log(
        source,
        f"{request.method} {path} → {response.status_code}",
        level,
        ms=ms,
        status=response.status_code,
    )
    return response


app.include_router(data_routes.router)
app.include_router(campaign_routes.router)
app.include_router(automation_routes.router)
app.include_router(accounts_routes.router)
app.include_router(settings_routes.router)
app.include_router(minio_routes.router)


@app.get("/api/status")
async def status():
    return {
        "status": "ready",
        "env": config.ENV,
        "db": db.status(),
        "browser": await browser.status(),
    }


_SHOTS = config.BACKEND_DIR / "data" / "screenshots"
_SHOTS.mkdir(parents=True, exist_ok=True)
app.mount("/shots", StaticFiles(directory=_SHOTS), name="shots")


@app.middleware("http")
async def _no_cache_frontend(request, call_next):
    """Stop the browser caching the UI.

    Without this there's no Cache-Control at all, so browsers fall back to
    heuristic caching and keep serving an old app.js from cache without ever
    asking whether it changed — an edit then looks like it did nothing until a
    hard refresh. The API is tiny and the UI is local, so never cache either.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/shots"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.get("/")
async def index():
    return FileResponse(config.FRONTEND_DIR / "index.html")


# Mounted last so it never shadows /api/*.
app.mount("/", StaticFiles(directory=config.FRONTEND_DIR, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    class _Server(uvicorn.Server):
        """Uvicorn, told to let the live feed go the instant Ctrl+C lands.

        `handle_exit` is the first thing that runs on the signal — BEFORE the
        server starts waiting for in-flight requests. That ordering is the whole
        point: the lifespan's teardown runs *after* that wait, so signalling the
        streams from there would be signalling them once the waiting was already
        over. Ctrl+C used to hang here for exactly that reason.
        """

        def handle_exit(self, sig, frame):
            try:
                activity.begin_shutdown()
            except Exception:  # noqa: BLE001 — never block the exit path
                pass
            super().handle_exit(sig, frame)

    # And a hard ceiling behind it. A wedged connection must not be able to hold
    # the terminal hostage; five seconds is long enough for an orderly close and
    # short enough that nobody reaches for the second Ctrl+C.
    _Server(uvicorn.Config(app, host=config.HOST, port=config.PORT,
                           timeout_graceful_shutdown=5)).run()
