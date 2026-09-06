"""Tests for the agentic loop, using a fake Anthropic client.

These are the tests that matter most, because the loop's invariants are silent
when broken. Nothing crashes if tool results come back in separate messages — the
agent just quietly stops making parallel calls and gets slower forever. A test is
the only thing that catches it.

The fake client is deliberately tiny: `messages.stream()` returns an object that
supports `async with`, `async for` and `get_final_message()`. That is the entire
surface the loop touches.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import split_itinerary

from nature_trails.agent.loop import AgentRefusal, TripPlanner
from nature_trails.agent.schemas import Itinerary, ItineraryCore, ItineraryLogistics
from nature_trails.core.config import get_settings

pytestmark = pytest.mark.asyncio


# --- fake API objects -------------------------------------------------------


def usage(inp: int = 100, out: int = 50, cache_read: int = 0, cache_write: int = 0):
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )


def text(content: str):
    return SimpleNamespace(type="text", text=content)


def tool_use(name: str, args: dict, block_id: str):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=block_id)


def message(content: list, stop_reason: str, container_id: str | None = None):
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=usage(),
        # Present once web_search has started a server-side execution container.
        container=SimpleNamespace(id=container_id) if container_id else None,
    )


class FakeStream:
    def __init__(self, final):
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def _empty():
            return
            yield  # pragma: no cover

        return _empty()

    async def get_final_message(self):
        return self._final


class FakeMessages:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.stream_calls: list[dict] = []
        self.parse_calls: list[dict] = []

    def stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        # Repeat the last scripted response if the loop keeps going.
        final = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return FakeStream(final)

    async def parse(self, **kwargs):
        self.parse_calls.append(kwargs)
        # Return whichever half the caller asked for, so the merge is exercised.
        core, logistics = split_itinerary()
        parsed = core if kwargs["output_format"] is ItineraryCore else logistics
        return SimpleNamespace(usage=usage(), parsed_output=parsed)


class FakeClient:
    def __init__(self, responses: list):
        self.messages = FakeMessages(responses)

    async def close(self):
        pass


@pytest.fixture
def make_planner(monkeypatch):
    created: list[TripPlanner] = []

    def _make(responses: list, max_turns: int = 8) -> TripPlanner:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        get_settings.cache_clear()
        planner = TripPlanner()
        planner.client = FakeClient(responses)
        planner.settings.max_turns = max_turns
        created.append(planner)
        return planner

    yield _make
    get_settings.cache_clear()


# --- tests ------------------------------------------------------------------


async def test_loop_stops_on_end_turn_and_returns_the_brief(make_planner):
    planner = make_planner([message([text("The research brief.")], "end_turn")])
    brief, turns, calls = await planner.research("Dolomites in September")
    assert brief == "The research brief."
    assert turns == 1
    assert calls == 0


async def test_all_tool_results_return_in_a_single_user_message(make_planner):
    """The invariant that silently degrades the agent when broken.

    Claude emitted two tool_use blocks in one turn. Both results must come back in
    ONE user message, or we teach it that parallel calls get serialised.
    """
    planner = make_planner(
        [
            message(
                [
                    tool_use(
                        "make_bounding_box",
                        {"latitude": 46.5, "longitude": 12.1, "radius_km": 20},
                        "t1",
                    ),
                    tool_use(
                        "get_daylight",
                        {"latitude": 46.5, "longitude": 12.1, "start_date": "2026-09-10"},
                        "t2",
                    ),
                ],
                "tool_use",
            ),
            message([text("Done researching.")], "end_turn"),
        ]
    )
    brief, turns, calls = await planner.research("Dolomites")

    assert calls == 2
    assert turns == 2
    assert brief == "Done researching."

    # Inspect the messages handed to the SECOND request.
    second_request = planner.client.messages.stream_calls[1]
    msgs = second_request["messages"]

    user_messages = [m for m in msgs if m["role"] == "user"]
    # One original request + exactly one results message. Not two.
    assert len(user_messages) == 2

    results = user_messages[-1]["content"]
    assert len(results) == 2
    assert {r["tool_use_id"] for r in results} == {"t1", "t2"}
    assert all(r["type"] == "tool_result" for r in results)
    assert all(not r.get("is_error") for r in results)


async def test_failed_tool_still_produces_an_answered_block(make_planner):
    """Every tool_use must be answered or the next request is an API error."""
    planner = make_planner(
        [
            message([tool_use("make_bounding_box", {"nonsense": 1}, "bad")], "tool_use"),
            message([text("Recovered.")], "end_turn"),
        ]
    )
    await planner.research("anything")

    results = planner.client.messages.stream_calls[1]["messages"][-1]["content"]
    assert len(results) == 1
    assert results[0]["tool_use_id"] == "bad"
    assert results[0]["is_error"] is True
    # The message must tell the model how to recover, not just that it broke.
    assert "Expected parameters" in results[0]["content"]


async def test_unknown_tool_is_reported_not_raised(make_planner):
    planner = make_planner(
        [
            message([tool_use("teleport", {}, "x")], "tool_use"),
            message([text("ok")], "end_turn"),
        ]
    )
    await planner.research("anything")
    results = planner.client.messages.stream_calls[1]["messages"][-1]["content"]
    assert results[0]["is_error"] is True
    assert "Unknown tool" in results[0]["content"]


async def test_turn_cap_stops_a_runaway_loop(make_planner):
    """Without this ceiling, a confused agent bills indefinitely."""
    planner = make_planner(
        [
            message(
                [
                    tool_use(
                        "make_bounding_box", {"latitude": 1, "longitude": 1, "radius_km": 5}, "t"
                    )
                ],
                "tool_use",
            )
        ],
        max_turns=4,
    )
    _, turns, calls = await planner.research("loop forever")
    assert turns == 4
    assert calls == 4


async def test_pause_turn_resumes_without_tool_results(make_planner):
    """Server-side tools pause mid-turn; we resend rather than supply results."""
    planner = make_planner(
        [
            message([text("searching...")], "pause_turn"),
            message([text("Finished.")], "end_turn"),
        ]
    )
    brief, turns, calls = await planner.research("permits for Machu Picchu")
    assert brief == "Finished."
    assert calls == 0

    msgs = planner.client.messages.stream_calls[1]["messages"]
    assert msgs[-1]["role"] == "assistant"


async def test_container_id_is_threaded_through_later_turns(make_planner):
    """web_search does dynamic filtering via a server-side code container.

    Once that container exists, every subsequent request must name it or the API
    rejects the turn with "container_id is required when there are pending tool
    uses generated by code execution with tools". Found against the live API.
    """
    planner = make_planner(
        [
            message(
                [
                    tool_use(
                        "make_bounding_box", {"latitude": 1, "longitude": 1, "radius_km": 5}, "t"
                    )
                ],
                "tool_use",
                container_id="container_abc123",
            ),
            message([text("done")], "end_turn"),
        ]
    )
    await planner.research("hut opening dates")

    first, second = planner.client.messages.stream_calls
    assert "container" not in first, "no container exists before the first response"
    assert second["container"] == "container_abc123"


async def test_no_container_key_when_web_search_never_runs(make_planner):
    """Sending a container id that does not exist is its own error."""
    planner = make_planner([message([text("brief")], "end_turn")])
    await planner.research("Banff in July")
    assert "container" not in planner.client.messages.stream_calls[0]


async def test_refusal_raises(make_planner):
    planner = make_planner([message([], "refusal")])
    with pytest.raises(AgentRefusal):
        await planner.research("something the model declines")


async def test_request_shape_is_cache_friendly(make_planner):
    """Tools and system must be byte-stable, with the cache breakpoint at the end.

    Caching is a prefix match over tools -> system -> messages. If anything
    volatile creeps above the breakpoint, the hit rate silently goes to zero.
    """
    planner = make_planner([message([text("brief")], "end_turn")])
    await planner.research("Banff in July")

    call = planner.client.messages.stream_calls[0]

    system = call["system"]
    assert system[-1]["cache_control"] == {"type": "ephemeral"}

    names = [t["name"] for t in call["tools"] if "name" in t]
    local = [n for n in names if n != "web_search"]
    assert local == sorted(local), "tool order must be deterministic for cache stability"
    assert "web_search" in names

    # Today's date is volatile and must live in messages, never in system.
    assert "Today's date" not in system[-1]["text"]
    assert "Today's date" in call["messages"][0]["content"]

    assert call["thinking"] == {"type": "adaptive", "display": "summarized"}


async def test_assembly_uses_two_constrained_calls_and_merges_them(make_planner):
    """Phase 2 is two calls, neither of them the full Itinerary.

    The whole `Itinerary` schema exceeds the structured-output grammar limit and
    returns `400 ... compiled grammar is too large`. Sending it is a regression
    that only shows up against the live API, so the shape is asserted here.
    """
    planner = make_planner([message([text("brief")], "end_turn")])
    result = await planner.assemble("Banff in July", "the brief")

    assert isinstance(result, Itinerary)
    assert result.title == "Alta Via 1: Dolomites Traverse"  # came from the core half
    assert result.budget.currency == "EUR"  # came from the logistics half

    calls = planner.client.messages.parse_calls
    assert len(calls) == 2
    assert {c["output_format"].__name__ for c in calls} == {
        "ItineraryCore",
        "ItineraryLogistics",
    }
    # Never the merged model — that is exactly what blows the grammar limit.
    assert all(c["output_format"] is not Itinerary for c in calls)
    # Assembly is not an agent: no tools in either call.
    assert all("tools" not in c for c in calls)


async def test_split_covers_every_itinerary_field():
    """Neither half may drop a field. Add one to `Itinerary` and forget to put it
    in a half, and assembly would silently stop producing it."""
    covered = set(ItineraryCore.model_fields) | set(ItineraryLogistics.model_fields)
    assert covered == set(Itinerary.model_fields)


async def test_usage_accumulates_across_turns(make_planner):
    planner = make_planner(
        [
            message(
                [
                    tool_use(
                        "make_bounding_box", {"latitude": 1, "longitude": 1, "radius_km": 5}, "t"
                    )
                ],
                "tool_use",
            ),
            message([text("done")], "end_turn"),
        ]
    )
    await planner.research("x")
    assert planner.usage.requests == 2
    assert planner.usage.input_tokens == 200
    assert planner.usage.estimated_cost_usd > 0
