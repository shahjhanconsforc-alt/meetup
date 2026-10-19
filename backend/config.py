"""Central config — every .env value is read here and nowhere else."""
import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent
ROOT_DIR = BACKEND_DIR.parent
FRONTEND_DIR = ROOT_DIR / "frontend"

load_dotenv(ROOT_DIR / ".env")


def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


HOST = os.getenv("HOST", "127.0.0.1")
# 4200, not 4000/4100: the Instagram bot owns 4000 and the Threads bot 4100 on
# the machine these run side by side on, and two servers cannot share a port —
# the second one to start dies on bind. Override with PORT in .env.
PORT = int(os.getenv("PORT", 4200))
ENV = os.getenv("ENV", "development")

DB_FILE = BACKEND_DIR / os.getenv("DB_FILE", "data/store.json")

PLAYWRIGHT_HEADLESS = _bool("PLAYWRIGHT_HEADLESS", False)
PLAYWRIGHT_CHANNEL = os.getenv("PLAYWRIGHT_CHANNEL", "chrome")
# Where "Open Chrome" lands when no URL is given. Empty = blank window.
PLAYWRIGHT_START_URL = os.getenv("PLAYWRIGHT_START_URL", "").strip()

MU_USERNAME = os.getenv("MU_USERNAME", "").strip()
MU_PASSWORD = os.getenv("MU_PASSWORD", "")
PLAYWRIGHT_SLOWMO = int(os.getenv("PLAYWRIGHT_SLOWMO", 0))

# Persistent Chrome profile — keeps you logged in between runs.
PLAYWRIGHT_USER_DATA_DIR = BACKEND_DIR / os.getenv("PLAYWRIGHT_USER_DATA_DIR", "data/chrome-profile")
# Strip the "controlled by automated test software" infobar + navigator.webdriver.
PLAYWRIGHT_CLEAN_LAUNCH = _bool("PLAYWRIGHT_CLEAN_LAUNCH", True)
# Keep Chromium's --no-sandbox. A clean launch strips it (a real desktop Chrome
# never has it), but Chromium refuses to start as root without it — which is
# exactly how it runs inside the Docker image. Leave false on a desktop.
PLAYWRIGHT_NO_SANDBOX = _bool("PLAYWRIGHT_NO_SANDBOX", False)

# ─── Meetup ────────────────────────────────────────────────────────────────
BASE_URL = "https://www.meetup.com"
LOGIN_URL = f"{BASE_URL}/login/"
# Meetup's own cookie name for a logged-in session. sessions.py reads the Chrome
# profile's cookie store for this and nothing else — its presence and expiry are
# the whole "are we logged in?" answer, with no browser launched.
SESSION_COOKIE = "MEETUP_SESSION"

# ─── Proxy (optional) ──────────────────────────────────────────────────────
# Route the browser through a residential/mobile proxy so automation doesn't run
# from a flagged datacenter IP (the single biggest bot-detection tell). Set ONE
# proxy — it's applied at launch and kept for the whole session: Meetup treats an
# IP change on a logged-in session as a "suspicious login", so we deliberately do
# NOT rotate mid-session. Leave PROXY_SERVER empty to run without a proxy.
#   PROXY_SERVER e.g. http://host:port  or  socks5://host:port
PROXY_SERVER = os.getenv("PROXY_SERVER", "").strip()
PROXY_USERNAME = os.getenv("PROXY_USERNAME", "").strip()
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD", "")
# Comma-separated hosts that skip the proxy (e.g. "localhost,127.0.0.1").
PROXY_BYPASS = os.getenv("PROXY_BYPASS", "").strip()


def playwright_proxy() -> dict | None:
    """The `proxy=` dict Playwright's launch wants, or None when no proxy is set.

    Kept here so .env reading stays in one place; the runner just passes the
    result straight to launch_persistent_context()."""
    if not PROXY_SERVER:
        return None
    proxy: dict = {"server": PROXY_SERVER}
    if PROXY_USERNAME:
        proxy["username"] = PROXY_USERNAME
    if PROXY_PASSWORD:
        proxy["password"] = PROXY_PASSWORD
    if PROXY_BYPASS:
        proxy["bypass"] = PROXY_BYPASS
    return proxy
