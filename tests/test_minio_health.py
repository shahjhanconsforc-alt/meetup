"""The MinIO up/down signal, and how loudly it says it.

The Storage pill polls this every few seconds. A line per poll buried every
other message in the terminal within a minute of MinIO being down — which is
exactly when the rest of the log matters most.
"""
from __future__ import annotations

import logging

import pytest

from services.storage import minio_client as mc


@pytest.fixture
def records():
    """Capture on the logger itself.

    minio_client sets `propagate = False` so its lines don't double-print
    through the root, which also means pytest's caplog — a root handler — never
    sees them. Attaching here is the only way to read what it actually emitted.
    """
    class Collector(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.seen: list[logging.LogRecord] = []

        def emit(self, record):
            self.seen.append(record)

    handler = Collector()
    previous = mc.log.level
    mc.log.setLevel(logging.DEBUG)
    mc.log.addHandler(handler)
    try:
        yield handler.seen
    finally:
        mc.log.removeHandler(handler)
        mc.log.setLevel(previous)


class Dead:
    """A client whose every call fails, the way an unreachable server's does."""

    def list_buckets(self):
        raise ConnectionError("Max retries exceeded with url: /")


class Live:
    def list_buckets(self):
        return []


@pytest.fixture
def dead_minio(monkeypatch):
    monkeypatch.setattr(mc, "get_client", lambda: Dead())
    monkeypatch.setattr(mc, "_last_up", None)


@pytest.fixture
def live_minio(monkeypatch):
    monkeypatch.setattr(mc, "get_client", lambda: Live())
    monkeypatch.setattr(mc, "_last_up", None)


def loud(records):
    """The lines a human would see — debug is opt-in, so it doesn't count."""
    return [r for r in records if r.levelno >= logging.INFO]


def test_a_dead_server_is_reported_once_not_once_per_poll(dead_minio, records):
    results = [mc.is_up() for _ in range(6)]

    assert results == [False] * 6            # every caller still gets the answer
    assert len(loud(records)) == 1
    assert loud(records)[0].levelno == logging.WARNING


def test_a_live_server_is_reported_once(live_minio, records):
    results = [mc.is_up() for _ in range(4)]

    assert results == [True] * 4
    assert len(loud(records)) == 1
    assert loud(records)[0].levelno == logging.INFO


def test_going_down_is_logged_even_after_quiet_polls(live_minio, records, monkeypatch):
    """The transition is the whole point — the quiet must not swallow it."""
    mc.is_up()
    mc.is_up()
    monkeypatch.setattr(mc, "get_client", lambda: Dead())

    assert mc.is_up() is False

    messages = [r.getMessage() for r in loud(records)]
    assert len(messages) == 2
    assert "was running" in messages[1]


def test_recovery_is_logged(dead_minio, records, monkeypatch):
    mc.is_up()
    monkeypatch.setattr(mc, "get_client", lambda: Live())

    assert mc.is_up() is True

    messages = [r.getMessage() for r in loud(records)]
    assert len(messages) == 2
    assert "recovered" in messages[1]


def test_the_quiet_polls_still_trace_at_debug(dead_minio, records):
    for _ in range(3):
        mc.is_up()

    debug = [r for r in records if r.levelno == logging.DEBUG]
    assert len(debug) == 2          # the first poll spoke up; the rest just trace


def test_the_reason_is_kept_on_the_first_report(dead_minio, records):
    """Losing the exception text would leave "STOPPED" with no way to tell a
    stopped container from a wrong port or a bad key."""
    mc.is_up()
    assert "ConnectionError" in loud(records)[0].getMessage()


# ── An empty MINIO_USER, which reads as a permissions problem ────────────
# Without credentials the SDK still builds a client and still talks to the
# server — anonymously. MinIO answers every request with AccessDenied, naming
# the bucket, so it reads like a policy to go and debug rather than the blank
# line in .env that it is.

def test_an_access_denied_with_no_credentials_says_which_it_is(monkeypatch):
    from minio.error import S3Error

    from services.storage import minio_client as mc

    monkeypatch.setattr(mc, "HAS_CREDENTIALS", False)
    denied = S3Error(None, "AccessDenied", "Access Denied.",
                     "/meetup-members", "req-1", "host-1")

    said = mc._why(denied)                                    # noqa: SLF001

    assert "AccessDenied" in said
    assert "MINIO_USER/MINIO_PASSWORD are empty" in said


def test_a_real_permissions_error_is_left_alone(monkeypatch):
    """With the keys set, an AccessDenied IS a permissions problem — telling
    the operator to check .env would send them the wrong way."""
    from minio.error import S3Error

    from services.storage import minio_client as mc

    monkeypatch.setattr(mc, "HAS_CREDENTIALS", True)
    denied = S3Error(None, "AccessDenied", "Access Denied.",
                     "/meetup-members", "req-1", "host-1")

    assert "MINIO_USER" not in mc._why(denied)                # noqa: SLF001


def test_the_health_dict_carries_the_reason(monkeypatch):
    """The pill polls this. "down" with no reason is a shrug."""
    from services.storage import minio_client as mc

    monkeypatch.setattr(mc, "HAS_CREDENTIALS", False)
    assert "MINIO_USER" in mc.credential_problem()

    monkeypatch.setattr(mc, "HAS_CREDENTIALS", True)
    assert mc.credential_problem() == ""
