"""Shared fixtures — every test runs against a throwaway store and profile tree.

The one rule here: a test must never read or write the real
`backend/data/store.json`, `backend/data/profiles/`, or the MinIO bucket.
The first two hold live credentials and live Chrome sessions, and a test that
clears a session would destroy a login that cost a captcha to obtain. The third
holds the operator's collected data, and a test that writes to it puts its own
fixtures in front of them as if they were real.
"""
from __future__ import annotations

import itertools
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend import accounts, config, db, sessions
from services.storage import minio_client as mc

# Chrome's cookie expiry epoch (1601-01-01) in microseconds before the Unix one.
CHROME_EPOCH_OFFSET = 11_644_473_600_000_000


def to_chrome_time(when: datetime) -> int:
    """A datetime as Chrome stores it: microseconds since 1601-01-01."""
    return int(when.timestamp() * 1_000_000) + CHROME_EPOCH_OFFSET


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point the JSON store and the profiles tree at tmp_path.

    Autouse: forgetting to request this fixture in one test is exactly the
    mistake that would wipe the real store, so it is not opt-in.
    """
    store_file = tmp_path / "store.json"
    profiles = tmp_path / "profiles"
    profiles.mkdir()

    monkeypatch.setattr(config, "DB_FILE", store_file)
    monkeypatch.setattr(config, "PLAYWRIGHT_USER_DATA_DIR", tmp_path / "chrome-profile")
    monkeypatch.setattr(sessions, "PROFILES_DIR", profiles)
    monkeypatch.setattr(db, "_cache", None)          # force a reload from tmp_path
    monkeypatch.setattr(db, "_draft_cache", None)

    yield tmp_path

    db._cache = None


@pytest.fixture(autouse=True)
def isolated_objects(monkeypatch):
    """Point every object read and write at a dict, not at MinIO.

    `isolated_store` one level down. Rosters and member files go STRAIGHT to
    storage with no store.json in between, so isolating the JSON file left
    every test that collected an event writing its fixtures — "First event:
    Alice[alice], Bob[bob]" — into the operator's real bucket, where the Data
    panel showed them alongside genuinely scraped rosters.

    Autouse, because forgetting to ask for it is exactly the mistake that put
    them there. Yields the dict, so a test can assert on what was written.
    """
    objects: dict[tuple[str, str], bytes] = {}
    written: dict[tuple[str, str], int] = {}
    clock = itertools.count(1)

    def _put(bucket, name, data, content_type="application/octet-stream"):
        objects[(bucket, name)] = bytes(data)
        # A counter, not a clock: `at` only ever gets compared, and two writes
        # in the same millisecond are a real thing in a test.
        written[(bucket, name)] = next(clock)
        return len(data)

    def _get(bucket, name):
        return objects.get((bucket, name))

    def _list(bucket, prefix=""):
        return [{"name": name, "size": len(blob), "at": written[(owner, name)]}
                for (owner, name), blob in objects.items()
                if owner == bucket and name.startswith(prefix)]

    def _remove(bucket, name):
        objects.pop((bucket, name), None)
        written.pop((bucket, name), None)

    def _no_client():
        raise AssertionError("a test reached for a real MinIO connection — "
                             "it has found a way around isolated_objects")

    monkeypatch.setattr(mc, "put_bytes", _put)
    monkeypatch.setattr(mc, "get_bytes", _get)
    monkeypatch.setattr(mc, "list_objects", _list)
    monkeypatch.setattr(mc, "remove_object", _remove)
    monkeypatch.setattr(mc, "ensure_bucket", lambda bucket=mc.BUCKET: None)
    monkeypatch.setattr(mc, "ensure_all_buckets", lambda: [])
    # The four above are the only door to the bucket. Anything that opens a
    # client has found another one, and it should say so rather than quietly
    # reach the real server — the health tests, which are ABOUT the client,
    # patch this back to their own fake.
    monkeypatch.setattr(mc, "get_client", _no_client)
    return objects


@pytest.fixture
def make_profile(tmp_path):
    """Build a Chrome profile directory containing a cookie DB.

    `cookies` is a list of (host_key, name, expires_utc) with expires_utc already
    in Chrome time; pass an empty list for a profile that has a cookie store but
    no cookies in it.
    """
    def _make(name: str, cookies, rel: str = "Default/Network/Cookies") -> Path:
        profile = tmp_path / "profiles" / name
        target = profile / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target)
        conn.execute(
            "CREATE TABLE cookies ("
            "  host_key TEXT, name TEXT, value TEXT,"
            "  expires_utc INTEGER, is_persistent INTEGER DEFAULT 1)"
        )
        conn.executemany(
            "INSERT INTO cookies (host_key, name, value, expires_utc) VALUES (?,?,?,?)",
            [(h, n, "x", e) for h, n, e in cookies],
        )
        conn.commit()
        conn.close()
        return profile

    return _make


@pytest.fixture
def future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=30)


@pytest.fixture
def past() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=1)


@pytest.fixture
def account() -> dict:
    """One stored account, created through the real add path."""
    return accounts.add_account("Tester", "tester@example.com", "hunter2")
