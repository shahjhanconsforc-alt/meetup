"""MinIO connection + a terminal logger for it.

One place that knows how to reach the MinIO server: it reads the MINIO_* vars
from the project's .env, hands back a shared client, makes sure the bucket is
there, and prints to the terminal whether MinIO is running, stopped, or
something went wrong.

Nothing here ever raises just because MinIO is down. A health check returns
False and logs it; an operation that needs a live server raises MinioUnavailable
so the caller can decide. The app must keep working with MinIO offline.
"""
from __future__ import annotations

import itertools
import logging
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import urllib3
from dotenv import load_dotenv
from minio import Minio
from minio.error import S3Error

# ── .env ──────────────────────────────────────────────────────────
# services/storage/minio_client.py -> project root is two levels up. Load the
# same .env the backend uses, so this works whether it's imported by the running
# app or run on its own as a one-off script.
_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")


# ── Terminal logging ──────────────────────────────────────────────
# Every MinIO log line is (a) printed to the server's stdout terminal and (b)
# kept in a small ring buffer, so the in-app "MinIO logs" terminal — the one you
# open from the MinIO health pill — can show the same stream without a second
# logging setup.
_LOG_BUFFER: deque[dict] = deque(maxlen=500)
_log_seq = itertools.count(1)


class _BufferHandler(logging.Handler):
    """Keeps the last N log lines so the UI can render them, each with an id so
    the panel only fetches what's new."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _LOG_BUFFER.append({
                "id": next(_log_seq),
                "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "level": record.levelname.lower(),
                "message": record.getMessage(),
            })
        except Exception:  # noqa: BLE001 — logging must never raise
            pass


def _make_logger() -> logging.Logger:
    """A named logger streaming to stdout AND the ring buffer. Configured once;
    if the app already set up logging we just add our handlers rather than
    fighting over the root logger."""
    logger = logging.getLogger("meetup-bot.minio")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-5s  minio: %(message)s",
            datefmt="%H:%M:%S",
        ))
        logger.addHandler(handler)
        logger.addHandler(_BufferHandler())
        logger.propagate = False           # don't double-print via the root
    return logger


log = _make_logger()


def recent_logs(after: int = 0) -> list[dict]:
    """Buffered log lines with id greater than `after` — for the UI to poll."""
    return [e for e in _LOG_BUFFER if e["id"] > after]


# ── Config ────────────────────────────────────────────────────────
def _parse_endpoint(raw: str) -> tuple[str, bool]:
    """Split MINIO_URL into (host:port, secure). Accepts a bare host:port or a
    full http(s):// URL; the scheme, if any, decides TLS."""
    value = (raw or "localhost:9000").strip()
    secure = value.lower().startswith("https://")
    for prefix in ("https://", "http://"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
            break
    return value.rstrip("/"), secure


ENDPOINT, SECURE = _parse_endpoint(os.getenv("MINIO_URL", "localhost:9000"))
ACCESS_KEY = os.getenv("MINIO_USER", "")
SECRET_KEY = os.getenv("MINIO_PASSWORD", "")

# One bucket per kind of data. NOTE: S3/MinIO bucket names may NOT contain
# underscores (they allow only lowercase letters, digits, dots and hyphens), so
# the prefix is "meetup-" rather than "meetup_". All four can be set in .env.
BUCKET_MEMBERS = os.getenv("MINIO_BUCKET", "meetup-members")
BUCKET_SESSIONS = os.getenv("MINIO_BUCKET_SESSIONS", "meetup-sessions")
BUCKET_MEDIA = os.getenv("MINIO_BUCKET_MEDIA", "meetup-media")
BUCKET_MESSAGES = os.getenv("MINIO_BUCKET_MESSAGES", "meetup-messages")

# `BUCKET` means the members bucket, so the common case reads naturally.
BUCKET = BUCKET_MEMBERS

ALL_BUCKETS = [BUCKET_MEMBERS, BUCKET_SESSIONS, BUCKET_MEDIA, BUCKET_MESSAGES]


class MinioUnavailable(RuntimeError):
    """Raised when an operation needs MinIO but it can't be reached."""


# Whether there are credentials to send at all. Without them the SDK still
# builds a client and still talks to the server — it just talks ANONYMOUSLY,
# and MinIO answers every request with AccessDenied. That error names the
# bucket, so it reads like a permissions problem on one bucket rather than the
# blank line in .env that it is.
HAS_CREDENTIALS = bool(ACCESS_KEY and SECRET_KEY)

_NO_CREDENTIALS = (
    f"no MinIO credentials — MINIO_USER/MINIO_PASSWORD are empty in .env, so "
    f"every request to {ENDPOINT} goes out anonymous and is denied"
)


def credential_problem() -> str:
    """The misconfiguration, in words, or "" when the keys are set."""
    return "" if HAS_CREDENTIALS else _NO_CREDENTIALS


def _why(exc: Exception) -> str:
    """One error, with the reason it is probably happening appended.

    An AccessDenied with no credentials configured is not a permissions puzzle
    to go and debug in MinIO's console — it is an unfilled setting, and saying
    so here is the difference between a five-second fix and an afternoon.
    """
    text = str(exc)
    if not HAS_CREDENTIALS and "AccessDenied" in text:
        return f"{text} — {_NO_CREDENTIALS}"
    return text


# ── Client (shared, lazily built) ─────────────────────────────────
_client: Minio | None = None
_lock = threading.Lock()


def get_client() -> Minio:
    """The shared MinIO client, built on first use. Building it does no network
    I/O — the first real call is where a dead server shows up."""
    global _client
    with _lock:
        if _client is None:
            log.info("connecting to %s (bucket %r, %s)",
                     ENDPOINT, BUCKET, "https" if SECURE else "http")
            if not HAS_CREDENTIALS:
                log.warning("%s", _NO_CREDENTIALS)
            # Fail fast: the SDK's default pool retries for ~30s, which would
            # make "is MinIO up?" hang the server's startup and every health
            # poll. A 3s timeout with no retries turns a dead server into an
            # answer in seconds. Real uploads pass their own bytes and still
            # complete inside this per-request window for a local server.
            http = urllib3.PoolManager(
                timeout=urllib3.Timeout(connect=3.0, read=10.0),
                retries=urllib3.Retry(total=0),
            )
            _client = Minio(ENDPOINT, access_key=ACCESS_KEY,
                            secret_key=SECRET_KEY, secure=SECURE,
                            http_client=http)
        return _client


# The last state is_up() reported, so a steady state can be logged once instead
# of once per poll. None = nothing reported yet, so the first answer always
# prints whichever way it goes.
_last_up: bool | None = None


def is_up() -> bool:
    """True if MinIO answers, False if it's stopped/unreachable.

    This is the running/stopped signal — call it on startup or before a batch to
    print MinIO's state to the terminal without blowing up when it's down.

    Only CHANGES are logged at warning/info. The Storage pill polls this every
    few seconds, and a line per poll buries every other message in the terminal
    within a minute of MinIO being down — which is exactly when the rest of the
    log matters. The unchanged state still goes out at debug for anyone who
    wants the full trace.
    """
    global _last_up
    try:
        get_client().list_buckets()          # cheapest authenticated round-trip
        up, detail = True, None
    except Exception as exc:  # noqa: BLE001 — any failure means "not usable now"
        up = False
        detail = f"{type(exc).__name__}: {_why(exc).splitlines()[0][:200]}"

    if up == _last_up:
        log.debug("service still %s at %s", "RUNNING" if up else "STOPPED", ENDPOINT)
        return up

    first = _last_up is None
    _last_up = up
    if up:
        log.info("service RUNNING at %s%s", ENDPOINT, "" if first else " (recovered)")
    else:
        log.warning("service STOPPED / unreachable at %s (%s)%s", ENDPOINT, detail,
                    "" if first else " — was running")
    return up


def ensure_bucket(bucket: str = BUCKET) -> None:
    """Create `bucket` if it isn't there yet (defaults to the members bucket).
    Raises MinioUnavailable if the server can't be reached, so callers fail
    loudly rather than half-working."""
    client = get_client()
    try:
        if client.bucket_exists(bucket):
            log.info("bucket %r ready", bucket)
        else:
            client.make_bucket(bucket)
            log.info("bucket %r created", bucket)
    except S3Error as exc:
        log.error("bucket check/create failed for %r: %s", bucket, exc)
        raise MinioUnavailable(f"MinIO bucket error: {_why(exc)}") from exc
    except Exception as exc:  # noqa: BLE001 — connection refused, DNS, timeout…
        log.error("cannot reach MinIO at %s (%s)", ENDPOINT, type(exc).__name__)
        raise MinioUnavailable(
            f"MinIO is not reachable at {ENDPOINT} ({type(exc).__name__})"
        ) from exc


def ensure_all_buckets() -> list[str]:
    """Create every bucket the app uses (members, sessions, media, messages).
    Returns the list. Safe to call repeatedly — existing buckets are left as-is."""
    for bucket in ALL_BUCKETS:
        ensure_bucket(bucket)
    return list(ALL_BUCKETS)


# Health, cached so a fast poll (the pill, the terminal's status line) doesn't
# fire a network round-trip every time. is_up() itself always checks live.
_health_lock = threading.Lock()
_last_up: bool | None = None
_last_check: float = 0.0
_HEALTH_TTL = 5.0          # seconds a check is considered fresh


def health(force: bool = False) -> dict:
    """Cached running/stopped status. Re-checks live at most every few seconds
    (or immediately when `force`), so polling stays cheap. Only a fresh live
    check appends a RUNNING/STOPPED line to the log — the cache stays quiet, so
    the terminal doesn't fill with identical lines on every poll."""
    global _last_up, _last_check
    with _health_lock:
        fresh = (time.monotonic() - _last_check) < _HEALTH_TTL
        if force or _last_up is None or not fresh:
            _last_up = is_up()
            _last_check = time.monotonic()
        up = _last_up
    return {"endpoint": ENDPOINT, "bucket": BUCKET, "secure": SECURE, "up": up,
            "detail": credential_problem()}


def status() -> dict:
    """A small dict for a health endpoint / the UI: where it points and whether
    it's up right now. Always a live check — use health() for a cached one."""
    return {"endpoint": ENDPOINT, "bucket": BUCKET, "secure": SECURE,
            "up": is_up(), "detail": credential_problem()}


# ── Generic object helpers (shared by the storage modules) ────────
import io as _io  # noqa: E402 — local to these helpers


def put_bytes(bucket: str, name: str, data: bytes,
              content_type: str = "application/octet-stream") -> int:
    """Write `data` to bucket/name, creating the bucket if needed. Returns the
    byte count. Raises MinioUnavailable if MinIO can't be reached."""
    ensure_bucket(bucket)
    try:
        get_client().put_object(bucket, name, _io.BytesIO(data), length=len(data),
                                content_type=content_type)
    except Exception as exc:  # noqa: BLE001
        log.error("write %s/%s failed (%s)", bucket, name, type(exc).__name__)
        raise MinioUnavailable(f"MinIO write error: {_why(exc)}") from exc
    return len(data)


def get_bytes(bucket: str, name: str) -> bytes | None:
    """Read bucket/name. None if the object isn't there; raises only when MinIO
    itself is unreachable, so 'missing' and 'down' are never confused."""
    try:
        response = get_client().get_object(bucket, name)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()
    except S3Error as exc:
        if exc.code in ("NoSuchKey", "NoSuchBucket"):
            return None
        raise MinioUnavailable(f"MinIO read error: {_why(exc)}") from exc
    except Exception as exc:  # noqa: BLE001
        raise MinioUnavailable(
            f"MinIO not reachable ({type(exc).__name__})"
        ) from exc


def list_objects(bucket: str, prefix: str = "") -> list[dict]:
    """[{name, size, at}] for every object in the bucket (optionally under
    prefix); `at` is the last-modified epoch, for cache-busting. Empty list if
    the bucket is empty or missing."""
    try:
        return [
            {"name": obj.object_name, "size": obj.size,
             "at": int(obj.last_modified.timestamp()) if obj.last_modified else 0}
            for obj in get_client().list_objects(bucket, prefix=prefix, recursive=True)
        ]
    except S3Error as exc:
        if exc.code == "NoSuchBucket":
            return []
        raise MinioUnavailable(f"MinIO list error: {_why(exc)}") from exc
    except Exception as exc:  # noqa: BLE001
        raise MinioUnavailable(
            f"MinIO not reachable ({type(exc).__name__})"
        ) from exc


def remove_object(bucket: str, name: str) -> None:
    """Delete bucket/name. Missing objects are a no-op."""
    try:
        get_client().remove_object(bucket, name)
    except S3Error as exc:
        if exc.code in ("NoSuchKey", "NoSuchBucket"):
            return
        raise MinioUnavailable(f"MinIO delete error: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise MinioUnavailable(
            f"MinIO not reachable ({type(exc).__name__})"
        ) from exc
