"""Ctrl+C has to actually stop the server.

Uvicorn stops accepting new connections and then WAITS for the in-flight ones
before it will exit. The activity feed is an SSE stream a browser tab holds open
indefinitely, so unless it ends itself the server waits on a request that never
finishes — which is exactly what made Ctrl+C hang.
"""
from __future__ import annotations

import asyncio

import pytest

from backend import activity
from backend.routes import data_routes


class ConnectedClient:
    """A browser tab that is still very much there."""

    async def is_disconnected(self):
        return False


@pytest.fixture(autouse=True)
def fresh_flag():
    activity._closing = None
    yield
    activity._closing = None
    activity._subscribers.clear()


async def _open_stream():
    response = await data_activity_stream()
    return response.body_iterator


async def data_activity_stream():
    return await data_routes.activity_stream(ConnectedClient())


def test_the_stream_ends_when_the_server_is_shutting_down():
    async def scenario():
        activity.log("test", "one")            # so history has a frame to flush
        stream = await _open_stream()
        first = await stream.__anext__()       # drains history, stream is live
        assert "one" in first

        activity.begin_shutdown()
        # It must finish on its own now, without the client ever disconnecting.
        rest = []
        async for chunk in stream:
            rest.append(chunk)
        return rest

    # The real assertion is that this returns at all. A generous ceiling, since
    # the bug it guards was an unbounded wait.
    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_a_stream_opened_after_the_signal_ends_immediately():
    """A late reconnect — EventSource retries on its own — must not restart the
    wait the server is trying to finish."""
    async def scenario():
        activity.begin_shutdown()
        stream = await _open_stream()
        async for _ in stream:
            pass

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_the_stream_still_ends_when_the_client_leaves():
    """The original exit path still works: a closed tab ends its own stream."""
    class GoneClient:
        async def is_disconnected(self):
            return True

    async def scenario():
        response = await data_routes.activity_stream(GoneClient())
        async for _ in response.body_iterator:
            pass

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_shutting_down_unsubscribes_every_listener():
    """A listener left in the set would keep a queue alive past the stream."""
    async def scenario():
        stream = await _open_stream()
        await stream.__anext__()
        assert activity._subscribers, "the stream should have registered"

        activity.begin_shutdown()
        async for _ in stream:
            pass
        return len(activity._subscribers)

    assert asyncio.run(asyncio.wait_for(scenario(), timeout=5)) == 0


def test_the_flag_is_built_lazily_inside_the_loop():
    """An asyncio.Event binds to the running loop, and this module is imported
    long before there is one — building it at import time would bind it to the
    wrong loop, or none."""
    activity._closing = None

    async def scenario():
        return activity.closing()

    flag = asyncio.run(scenario())
    assert isinstance(flag, asyncio.Event)


def test_begin_shutdown_survives_a_wedged_listener():
    """One bad queue must not stop the others being told."""
    async def scenario():
        class Wedged:
            def put_nowait(self, _event):
                raise RuntimeError("full")

        wedged = Wedged()
        activity._subscribers.add(wedged)
        try:
            activity.begin_shutdown()          # must not raise
        finally:
            activity._subscribers.discard(wedged)
        return activity.closing().is_set()

    assert asyncio.run(asyncio.wait_for(scenario(), timeout=5)) is True
