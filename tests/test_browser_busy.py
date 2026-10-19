"""One browser, one driver.

The pipeline's steps navigate the live page directly, and a browse task walks
the same page on its own schedule. Nothing queues them: they take turns, and
what the operator sees is their own click apparently opening an event they never
picked — a category browse opening the first card of each category, mid-pipeline.

So a pipeline step refuses while another run holds the browser, and says which
run to stop.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import control
from backend.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def no_runs():
    """Runs are module state — a leftover one would fail the next test."""
    control.stop_all()
    control._runs.clear()
    yield
    control._runs.clear()


PIPELINE_CALLS = [
    ("post", "/api/automation/categories/save", {}),
    ("post", "/api/automation/categories/open", {"key": "652"}),
    ("post", "/api/automation/pipeline/auto", {}),
    ("post", "/api/automation/events/members",
     {"url": "https://www.meetup.com/htb-lahore/events/1/"}),
    ("post", "/api/automation/chat/members",
     {"handles": ["alice"], "text": "hi"}),
]


@pytest.mark.parametrize("method,path,body", PIPELINE_CALLS)
def test_a_pipeline_step_refuses_while_a_browse_is_running(client, method, path, body):
    control.start("categories")

    response = getattr(client, method)(path, **({"json": body} if body else {}))

    assert response.status_code == 409
    assert "categories run is using the browser" in response.json()["detail"]


@pytest.mark.parametrize("method,path,body", PIPELINE_CALLS)
def test_a_paused_run_still_owns_the_browser(client, method, path, body):
    """Paused means "stopped mid-step with a page half-driven", not "let go"."""
    run = control.start("scrape")
    run.pause()

    response = getattr(client, method)(path, **({"json": body} if body else {}))

    assert response.status_code == 409
    assert "scrape run is using the browser" in response.json()["detail"]


def test_reading_the_saved_categories_is_never_blocked(client):
    """It touches no browser — it is the stored strip. Refusing it during a run
    would blank the panel's category pool for the length of that run."""
    control.start("categories")
    response = client.get("/api/automation/categories")

    assert response.status_code == 200
    assert response.json() == {"categories": []}


def test_a_finished_run_does_not_block_anything(client, monkeypatch):
    """The step itself is stubbed out: letting it through would launch a real
    Chrome, and what is under test is the guard, not the browser."""
    async def _open(account_id=None, key=""):
        return {"ok": True, "detail": "opened", "events": [], "key": key,
                "name": "Social Activities"}

    monkeypatch.setattr("backend.routes.automation_routes.campaign.open_category",
                        _open)
    run = control.start("categories")
    run.finish("done")

    response = client.post("/api/automation/categories/open", json={"key": "652"})

    assert response.status_code == 200               # the guard let it past


def test_the_chat_run_does_not_block_itself(client, monkeypatch):
    """The guard has to let `chat` through, or the run could never start; the
    one-run-per-kind rule in control.start is what stops a second one."""
    started = {}

    def _spawn(kind, coro, ctl=None):
        started["kind"] = kind
        coro.close()                    # nothing should actually drive a browser
        return 1

    monkeypatch.setattr("backend.routes.automation_routes.tasks.spawn", _spawn)

    response = client.post("/api/automation/chat/members",
                           json={"handles": ["alice"], "text": "hi"})

    assert response.status_code == 200
    assert started["kind"] == "chat"


def test_a_second_chat_run_is_refused(client, monkeypatch):
    control.start("chat")
    response = client.post("/api/automation/chat/members",
                           json={"handles": ["alice"], "text": "hi"})

    assert response.status_code == 409
    assert "already in progress" in response.json()["detail"]
