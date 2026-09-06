"""Tests for the HTTP API.

`TripPlanner` is replaced with a fake, so nothing here touches the Anthropic API or
the network. What is being tested is the *job machinery*: that POST returns
immediately, that progress is replayable, that failures are reported rather than
crashing the server, and that memory stays bounded.

The replay behaviour is the subtle one. A browser does `POST /plans` and *then*
opens an `EventSource`, so the agent has already started by the time anyone is
listening. A stream that only forwards live events silently drops the first few tool
calls — and `EventSource`'s automatic reconnection would drop more.
"""

from __future__ import annotations

import json
import time

import pytest
from conftest import make_itinerary
from fastapi.testclient import TestClient

from nature_trails import api
from nature_trails.agent.loop import PlanResult, Usage
from nature_trails.tools import ToolOutcome

GOOD_REQUEST = {"request": "6 days hut to hut in the Dolomites in September"}


class FakePlanner:
    """Emits a realistic event sequence, then returns a valid itinerary."""

    delay = 0.0
    fail_with: Exception | None = None

    def __init__(self, observer=None, profile=None):
        self.observer = observer
        self.profile = profile
        self.closed_shared = None

    async def plan(self, request: str) -> PlanResult:
        import asyncio

        self.observer.on_phase("Research")
        self.observer.on_turn(1, 30)
        self.observer.on_tool_start("geocode_place")
        self.observer.on_tool_results(
            [
                ToolOutcome(
                    tool_use_id="t1",
                    name="geocode_place",
                    content="{}",
                    summary="1 results",
                    duration_s=0.4,
                )
            ]
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_with:
            raise self.fail_with
        usage = Usage(input_tokens=1000, output_tokens=500, requests=3)
        self.observer.on_usage(usage)
        return PlanResult(
            itinerary=make_itinerary(), brief="a brief", usage=usage, turns=2, tool_calls=1
        )

    async def aclose(self, close_shared_http: bool = True) -> None:
        self.closed_shared = close_shared_http


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api, "TripPlanner", FakePlanner)
    monkeypatch.setattr(api.settings, "anthropic_api_key", "sk-ant-test", raising=False)
    api.JOBS.clear()
    FakePlanner.delay = 0.0
    FakePlanner.fail_with = None
    with TestClient(app=api.app) as test_client:
        yield test_client
    api.JOBS.clear()


def wait_for_terminal(client: TestClient, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/plans/{job_id}").json()
        if body["state"] in {"done", "failed", "cancelled"}:
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished; last state {body['state']}")


# --- basics ----------------------------------------------------------------


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["api_key_configured"] is True


def test_post_returns_immediately_with_urls(client):
    response = client.post("/plans", json=GOOD_REQUEST)
    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "queued"
    assert body["events_url"] == f"/plans/{body['id']}/events"
    assert body["status_url"] == f"/plans/{body['id']}"


def test_short_request_is_rejected_by_validation(client):
    assert client.post("/plans", json={"request": "hi"}).status_code == 422


def test_unknown_job_is_404(client):
    assert client.get("/plans/nope").status_code == 404
    assert client.delete("/plans/nope").status_code == 404
    assert client.get("/plans/nope/events").status_code == 404


def test_missing_api_key_is_503_not_a_crash(client, monkeypatch):
    monkeypatch.setattr(api.settings, "anthropic_api_key", None, raising=False)
    assert client.post("/plans", json=GOOD_REQUEST).status_code == 503


# --- the happy path --------------------------------------------------------


def test_plan_completes_and_returns_a_validated_itinerary(client):
    job_id = client.post("/plans", json=GOOD_REQUEST).json()["id"]
    body = wait_for_terminal(client, job_id)

    assert body["state"] == "done"
    assert body["error"] is None
    assert body["itinerary"]["title"] == "Alta Via 1: Dolomites Traverse"
    # The nested schema must survive serialisation, not just the top level.
    assert body["itinerary"]["days"][0]["accommodation"]["name"] == "Rifugio Biella"
    assert body["itinerary"]["budget"]["currency"] == "EUR"
    assert body["usage"]["estimated_cost_usd"] > 0
    assert body["elapsed_seconds"] >= 0


def test_profile_reaches_the_planner(client, monkeypatch):
    seen: list = []

    class Capturing(FakePlanner):
        def __init__(self, observer=None, profile=None):
            super().__init__(observer, profile)
            seen.append(profile)

    monkeypatch.setattr(api, "TripPlanner", Capturing)
    client.post(
        "/plans",
        json={**GOOD_REQUEST, "profile": {"nationality": "Canadian", "group_size": 3}},
    )
    assert seen and seen[0].nationality == "Canadian"
    assert seen[0].group_size == 3


def test_planner_is_closed_without_touching_the_shared_http_client(client):
    """One job closing the process-wide OSM client would break every other job."""
    created: list = []
    original = api.TripPlanner

    class Tracking(original):
        def __init__(self, observer=None, profile=None):
            super().__init__(observer, profile)
            created.append(self)

    api.TripPlanner = Tracking
    try:
        job_id = client.post("/plans", json=GOOD_REQUEST).json()["id"]
        wait_for_terminal(client, job_id)
    finally:
        api.TripPlanner = original

    assert created and created[0].closed_shared is False


# --- failures --------------------------------------------------------------


def test_a_failing_plan_is_reported_not_crashed(client):
    FakePlanner.fail_with = RuntimeError("overpass exploded")
    job_id = client.post("/plans", json=GOOD_REQUEST).json()["id"]
    body = wait_for_terminal(client, job_id)

    assert body["state"] == "failed"
    assert "overpass exploded" in body["error"]
    assert body["itinerary"] is None
    # Server still healthy afterwards.
    assert client.get("/health").json()["status"] == "ok"


def test_cancel_stops_a_running_plan(client):
    FakePlanner.delay = 5.0
    job_id = client.post("/plans", json=GOOD_REQUEST).json()["id"]
    time.sleep(0.1)
    client.delete(f"/plans/{job_id}")
    body = wait_for_terminal(client, job_id, timeout=5.0)
    assert body["state"] == "cancelled"


def test_a_job_that_overruns_its_wall_clock_budget_is_failed(client, monkeypatch):
    """NT_MAX_TURNS bounds turns, not time.

    Found live: a spell of DNS failures put every tool call into
    retry-with-backoff, and the job held a concurrency slot for 75 minutes on
    10 requests. Turn count alone cannot catch that.
    """
    monkeypatch.setattr(api.settings, "job_timeout_seconds", 1, raising=False)
    FakePlanner.delay = 30.0

    job_id = client.post("/plans", json=GOOD_REQUEST).json()["id"]
    body = wait_for_terminal(client, job_id, timeout=15.0)

    assert body["state"] == "failed"
    assert "Timed out after 1s" in body["error"]


def test_timeout_frees_the_concurrency_slot(client, monkeypatch):
    """The point of the timeout: a wedged job must not block later ones."""
    monkeypatch.setattr(api.settings, "job_timeout_seconds", 1, raising=False)
    monkeypatch.setattr(api.settings, "max_concurrent_plans", 1, raising=False)
    api._semaphore = None  # rebuild with the patched limit

    FakePlanner.delay = 30.0
    stuck = client.post("/plans", json=GOOD_REQUEST).json()["id"]
    wait_for_terminal(client, stuck, timeout=15.0)

    FakePlanner.delay = 0.0
    ok = client.post("/plans", json=GOOD_REQUEST).json()["id"]
    assert wait_for_terminal(client, ok, timeout=15.0)["state"] == "done"
    api._semaphore = None


# --- events ----------------------------------------------------------------


def read_events(client: TestClient, job_id: str, limit: int = 60) -> list[dict]:
    events = []
    with client.stream("GET", f"/plans/{job_id}/events") as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                if payload:
                    events.append(payload)
            if len(events) >= limit:
                break
    return events


def test_events_are_replayed_for_a_late_subscriber(client):
    """The core correctness property: connecting after the fact loses nothing."""
    job_id = client.post("/plans", json=GOOD_REQUEST).json()["id"]
    wait_for_terminal(client, job_id)

    events = read_events(client, job_id)
    kinds = [e["type"] for e in events]

    assert kinds[0] == "accepted"
    assert "phase" in kinds
    assert "tool_start" in kinds
    assert "tool_results" in kinds
    assert kinds[-1] == "done"

    # Sequence numbers must be dense and ordered, or the client cannot dedupe.
    assert [e["seq"] for e in events] == list(range(len(events)))


def test_tool_results_carry_what_the_ui_needs(client):
    job_id = client.post("/plans", json=GOOD_REQUEST).json()["id"]
    wait_for_terminal(client, job_id)

    results = next(e for e in read_events(client, job_id) if e["type"] == "tool_results")
    entry = results["results"][0]
    assert entry["name"] == "geocode_place"
    assert entry["summary"] == "1 results"
    assert entry["error"] is False
    assert entry["seconds"] == 0.4


def test_events_stream_live_while_the_plan_runs(client):
    """Subscribing mid-run must yield the terminal event without polling."""
    FakePlanner.delay = 0.6
    job_id = client.post("/plans", json=GOOD_REQUEST).json()["id"]
    events = read_events(client, job_id)
    assert events[-1]["type"] == "done"


def test_failure_appears_on_the_stream(client):
    FakePlanner.fail_with = ValueError("nope")
    job_id = client.post("/plans", json=GOOD_REQUEST).json()["id"]
    wait_for_terminal(client, job_id)
    events = read_events(client, job_id)
    assert events[-1]["type"] == "failed"
    assert "nope" in events[-1]["error"]


# --- housekeeping ----------------------------------------------------------


def test_finished_jobs_are_evicted_to_bound_memory(client, monkeypatch):
    monkeypatch.setattr(api.settings, "job_retention", 3, raising=False)
    for _ in range(6):
        job_id = client.post("/plans", json=GOOD_REQUEST).json()["id"]
        wait_for_terminal(client, job_id)
    assert len(api.JOBS) <= 3


def test_cors_allows_the_next_dev_server(client):
    response = client.options(
        "/plans",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code in (200, 204)
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_openapi_spec_includes_the_itinerary_schema(client):
    """This is what generates the frontend's TypeScript types."""
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    assert "Itinerary" in schemas
    assert "Day" in schemas
    assert "Stage" in schemas
    assert "TravellerProfile" in schemas
    assert "/plans" in spec["paths"]
