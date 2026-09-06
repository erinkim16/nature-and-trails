"""HTTP API — the job pattern plus a live progress stream.

This file is the web sibling of `cli.py`, and like `cli.py` it is deliberately
thin: parse a request, subclass `Observer` to report progress, hand the result to a
serialiser. Nothing in `agent/`, `tools/` or `core/` knows this file exists.

Why a job, and not just `POST /plan`
------------------------------------
A plan takes three to five minutes. HTTP does not tolerate that — browsers, reverse
proxies and load balancers give up somewhere between 30 and 60 seconds. So:

    POST /plans               -> 202 {"id": ...}         returns immediately
    GET  /plans/{id}          -> status, then itinerary
    GET  /plans/{id}/events   -> Server-Sent Events, live progress
    DELETE /plans/{id}        -> cancel a run rather than keep paying for it

Two details that are easy to get wrong
--------------------------------------
**Events must be replayable.** A browser does `POST /plans` and *then* opens an
`EventSource`. Between those two calls the agent has already started, so a stream
that only forwards live events drops the first few tool calls. Every job therefore
keeps a buffer, and a new subscriber is sent the backlog before being attached to
the live feed. This also makes `EventSource`'s automatic reconnection harmless.

**The shared HTTP client must outlive individual jobs.** The OSM/Open-Meteo client
in `core.http` is a process-wide singleton. If each finishing job closed it, it
would yank connections out from under every concurrent job. Planners are closed with
`close_shared_http=False`, and the singleton is closed once in the lifespan handler.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pydantic import Field as PydanticField
from sse_starlette.sse import EventSourceResponse

from .agent import Observer, TripPlanner, Usage
from .agent.intake import TravellerProfile
from .agent.schemas import Itinerary
from .core.config import get_settings
from .core.http import close_client
from .tools import ToolOutcome

log = logging.getLogger(__name__)


class JobState(StrEnum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


TERMINAL_STATES = {JobState.done, JobState.failed, JobState.cancelled}


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class PlanRequest(BaseModel):
    request: str = PydanticField(
        min_length=10,
        max_length=2000,
        description=(
            "What you want, in plain English. Include destination, month, budget, "
            "fitness and interests."
        ),
    )
    profile: TravellerProfile = PydanticField(
        default_factory=TravellerProfile,
        description="Traveller preferences. Same shape the CLI intake collects.",
    )


class UsageSummary(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    requests: int
    estimated_cost_usd: float

    @classmethod
    def of(cls, usage: Usage) -> UsageSummary:
        return cls(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            requests=usage.requests,
            estimated_cost_usd=usage.estimated_cost_usd,
        )


class PlanCreated(BaseModel):
    id: str
    state: JobState
    events_url: str
    status_url: str


class PlanStatus(BaseModel):
    id: str
    state: JobState
    created_at: float
    elapsed_seconds: float
    error: str | None = None
    itinerary: Itinerary | None = None
    usage: UsageSummary | None = None


# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    request: str
    profile: TravellerProfile
    state: JobState = JobState.queued
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    itinerary: Itinerary | None = None
    error: str | None = None
    usage: Usage | None = None
    task: asyncio.Task | None = None
    # Replay buffer, so a subscriber that connects late sees the whole run.
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.created_at

    def emit(self, event: dict[str, Any]) -> None:
        event = {"seq": len(self.events), "t": round(self.elapsed, 1), **event}
        self.events.append(event)
        for queue in list(self.subscribers):
            queue.put_nowait(event)

    def to_status(self) -> PlanStatus:
        return PlanStatus(
            id=self.id,
            state=self.state,
            created_at=self.created_at,
            elapsed_seconds=round(self.elapsed, 1),
            error=self.error,
            itinerary=self.itinerary,
            usage=UsageSummary.of(self.usage) if self.usage else None,
        )


JOBS: dict[str, Job] = {}


def _evict_old_jobs() -> None:
    """Keep memory bounded. Finished jobs go first, oldest first."""
    limit = get_settings().job_retention
    finished = sorted(
        (j for j in JOBS.values() if j.state in TERMINAL_STATES),
        key=lambda j: j.finished_at or j.created_at,
    )
    while len(JOBS) > limit and finished:
        JOBS.pop(finished.pop(0).id, None)


# ---------------------------------------------------------------------------
# Observer -> SSE
# ---------------------------------------------------------------------------


class QueueObserver(Observer):
    """Turns agent progress into JSON events on a job's replay buffer.

    `Observer`'s methods are synchronous by design, which is what makes this
    possible: `emit` is a plain append plus `put_nowait`, so nothing here can block
    the agent loop or need an `await`.
    """

    def __init__(self, job: Job) -> None:
        self.job = job

    def on_phase(self, name: str) -> None:
        self.job.emit({"type": "phase", "name": name})

    def on_turn(self, number: int, of_max: int) -> None:
        self.job.emit({"type": "turn", "number": number, "of_max": of_max})

    def on_tool_start(self, name: str) -> None:
        self.job.emit({"type": "tool_start", "name": name})

    def on_tool_results(self, outcomes: list[ToolOutcome]) -> None:
        self.job.emit(
            {
                "type": "tool_results",
                "results": [
                    {
                        "name": o.name,
                        "seconds": round(o.duration_s, 1),
                        "summary": o.summary,
                        "error": o.is_error,
                    }
                    for o in outcomes
                ],
            }
        )

    def on_usage(self, usage: Usage) -> None:
        self.job.usage = usage
        self.job.emit(
            {
                "type": "usage",
                "requests": usage.requests,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "estimated_cost_usd": usage.estimated_cost_usd,
            }
        )

    def on_warning(self, message: str) -> None:
        self.job.emit({"type": "warning", "message": message})

    # Thinking and assistant prose are deliberately not streamed to the browser.
    # They are large, they are the most expensive thing to render, and the tool
    # trace is what actually communicates progress. Add them here if needed.


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    """Created lazily: a Semaphore binds to the loop that constructs it."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, get_settings().max_concurrent_plans))
    return _semaphore


async def _execute(job: Job) -> None:
    planner = TripPlanner(observer=QueueObserver(job), profile=job.profile)
    try:
        async with _get_semaphore():
            job.state = JobState.running
            job.emit({"type": "state", "state": job.state.value})
            # A wall-clock ceiling, separate from NT_MAX_TURNS. Turn count does not
            # bound duration: when DNS or Overpass starts failing, every tool call
            # goes into retry-with-backoff and a job can hold a concurrency slot
            # for an hour while barely advancing. Seen for real during development.
            result = await asyncio.wait_for(
                planner.plan(job.request), timeout=settings.job_timeout_seconds
            )

        job.itinerary = result.itinerary
        job.usage = result.usage
        job.state = JobState.done
        job.finished_at = time.time()
        job.emit(
            {
                "type": "done",
                "title": result.itinerary.title,
                "turns": result.turns,
                "tool_calls": result.tool_calls,
                "estimated_cost_usd": result.usage.estimated_cost_usd,
            }
        )
    except TimeoutError:
        # wait_for already cancelled the inner task. Report it as a distinct
        # failure so the cause is obvious rather than a bare CancelledError.
        job.state = JobState.failed
        job.finished_at = time.time()
        job.error = (
            f"Timed out after {settings.job_timeout_seconds}s. The upstream data "
            "services were probably failing and every tool call was retrying. "
            "Partial cost was still incurred."
        )
        log.warning("plan %s timed out", job.id)
        job.emit({"type": "failed", "error": job.error, "reason": "timeout"})
    except asyncio.CancelledError:
        job.state = JobState.cancelled
        job.finished_at = time.time()
        job.emit({"type": "cancelled"})
        raise
    except Exception as exc:  # noqa: BLE001 — the job records it, the server survives
        log.exception("plan %s failed", job.id)
        job.state = JobState.failed
        job.finished_at = time.time()
        job.error = f"{type(exc).__name__}: {exc}"
        job.emit({"type": "failed", "error": job.error})
    finally:
        # False: the OSM/Open-Meteo client is shared across jobs — see the module
        # docstring. It is closed once, in the lifespan handler.
        await planner.aclose(close_shared_http=False)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    # Cancel anything still running, then close the shared HTTP client exactly once.
    for job in list(JOBS.values()):
        if job.task and not job.task.done():
            job.task.cancel()
    await close_client()


settings = get_settings()

app = FastAPI(
    title="Nature & Trails",
    version="0.1.0",
    description=(
        "Trip planning for national parks, long-distance hikes and alpine traverses. "
        "Plans run for several minutes, so POST /plans returns a job id immediately "
        "and progress arrives over Server-Sent Events."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness probe. Railway and friends want one of these."""
    running = sum(1 for j in JOBS.values() if j.state == JobState.running)
    return {
        "status": "ok",
        "model": settings.model,
        "api_key_configured": bool(settings.anthropic_api_key),
        "jobs_tracked": len(JOBS),
        "jobs_running": running,
        "max_concurrent": settings.max_concurrent_plans,
    }


@app.post("/plans", response_model=PlanCreated, status_code=202)
async def create_plan(body: PlanRequest) -> PlanCreated:
    """Start a plan. Returns immediately — watch `events_url` for progress."""
    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="Server has no ANTHROPIC_API_KEY configured.")

    job = Job(id=uuid.uuid4().hex[:12], request=body.request, profile=body.profile)
    JOBS[job.id] = job
    _evict_old_jobs()

    job.emit({"type": "accepted", "request": body.request})
    job.task = asyncio.create_task(_execute(job))

    return PlanCreated(
        id=job.id,
        state=job.state,
        events_url=f"/plans/{job.id}/events",
        status_url=f"/plans/{job.id}",
    )


@app.get("/plans/{job_id}", response_model=PlanStatus)
async def get_plan(job_id: str) -> PlanStatus:
    """Poll for state, and collect the itinerary once the state is `done`."""
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No plan {job_id}.")
    return job.to_status()


@app.delete("/plans/{job_id}", response_model=PlanStatus)
async def cancel_plan(job_id: str) -> PlanStatus:
    """Stop a run you no longer want to pay for."""
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No plan {job_id}.")
    if job.task and not job.task.done():
        job.task.cancel()
    return job.to_status()


@app.get("/plans/{job_id}/events")
async def stream_events(job_id: str, request: Request) -> EventSourceResponse:
    """Live progress as Server-Sent Events.

    Backlog first, then the live feed. That ordering is what makes it safe to open
    this *after* POST /plans, and what makes `EventSource`'s automatic reconnection
    harmless — a reconnecting client simply replays.
    """
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No plan {job_id}.")

    async def generator() -> AsyncIterator[dict[str, str]]:
        queue: asyncio.Queue = asyncio.Queue()
        sent = 0
        job.subscribers.add(queue)
        try:
            # 1. Replay everything that happened before this client connected.
            for event in list(job.events):
                yield {"event": event["type"], "data": json.dumps(event)}
                sent = event["seq"] + 1

            # A job that already finished has nothing live to send.
            if job.state in TERMINAL_STATES:
                return

            # 2. Follow the live feed until a terminal event arrives.
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    # Keep-alive: proxies drop idle connections, and a research
                    # turn can easily be quiet for 30s+.
                    yield {"event": "ping", "data": "{}"}
                    continue

                if event["seq"] < sent:
                    continue  # already replayed from the buffer
                sent = event["seq"] + 1
                yield {"event": event["type"], "data": json.dumps(event)}
                if event["type"] in {"done", "failed", "cancelled"}:
                    return
        finally:
            job.subscribers.discard(queue)

    return EventSourceResponse(generator())
