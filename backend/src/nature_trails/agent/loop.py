"""The agentic loop, and the two-phase orchestration around it.

Read this file to understand how an agent actually works. Everything else in the
project is either a tool it can call or a way to display what it did.

The loop in one paragraph
-------------------------
The request goes to the model along with a list of tools. It replies with either a
final answer (`stop_reason == "end_turn"`) or a set of tool calls
(`stop_reason == "tool_use"`). On tool calls the loop executes them, appends the
results to the conversation, and sends the whole thing back. Repeat until the model
stops calling tools. That is the entire mechanism — there is no hidden magic, and the "autonomy"
people describe is just this loop plus good tool descriptions.

Three details that are easy to get wrong
----------------------------------------
1. **All tool results go back in ONE user message.** The model emits several
   `tool_use` blocks per turn so they can run in parallel. Replying with one message
   per result presents a conversation in which parallel calls were serialised, and
   the model stops making them. The agent then gets slower every turn, with nothing
   in the logs to explain it.

2. **Every `tool_use` block must be answered**, including failures. An unanswered
   block is an API error on the next request. So `registry.dispatch` converts
   exceptions into `tool_result` blocks with `is_error: true` and a message that
   tells the model how to recover.

3. **Echo the assistant content back verbatim.** The loop appends `final.content`
   — the whole block list — not just the text. This preserves thinking blocks, which the
   API needs on subsequent turns of the same conversation.

Why a manual loop instead of the SDK's `tool_runner`
----------------------------------------------------
The SDK ships `client.beta.messages.tool_runner`, which does all of the above in
about five lines, and it is the right default in general. Three requirements here
need per-turn control it does not expose: the streamed progress display, per-tool
timing, and a hard turn ceiling. Switching to the tool runner later is a contained
refactor.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from anthropic import AsyncAnthropic

from ..core.config import get_settings
from ..core.http import close_client
from ..tools import ToolOutcome, dispatch_all, tool_definitions
from .intake import TravellerProfile
from .prompts import (
    ASSEMBLY_CORE_SYSTEM,
    ASSEMBLY_LOGISTICS_SYSTEM,
    RESEARCH_SYSTEM,
    assembly_prompt,
    research_prompt,
)
from .schemas import Itinerary, ItineraryCore, ItineraryLogistics

# Anthropic's server-side web search. It runs on Anthropic's infrastructure — no
# execution loop on our side — and covers everything OpenStreetMap structurally
# cannot know: permit systems, hut opening dates, lift timetables, closures,
# flight prices. `max_uses` bounds the cost of a single run.
WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 8,
}

# Claude Opus 5 list pricing, USD per million tokens. Cache reads are 0.1x input
# and cache writes 1.25x. Used only for the local spend estimate the CLI prints —
# check the current rates before trusting these for anything that matters.
PRICE_INPUT = 5.00
PRICE_OUTPUT = 25.00
PRICE_CACHE_READ = 0.50
PRICE_CACHE_WRITE = 6.25


@dataclass
class Usage:
    """Running token totals across every request in a plan."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests: int = 0

    def add(self, usage: Any) -> None:
        self.requests += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

    @property
    def estimated_cost_usd(self) -> float:
        return round(
            (
                self.input_tokens * PRICE_INPUT
                + self.output_tokens * PRICE_OUTPUT
                + self.cache_read_tokens * PRICE_CACHE_READ
                + self.cache_write_tokens * PRICE_CACHE_WRITE
            )
            / 1_000_000,
            4,
        )


class Observer:
    """Callbacks for whatever is watching the agent work.

    The loop knows nothing about terminals, web sockets or logging — it just calls
    these. `cli.TerminalObserver` draws a live terminal view; `api.QueueObserver`
    turns the same callbacks into Server-Sent Events. Both exist, neither required
    a change here — that is the whole reason the agent core stays UI-agnostic.
    """

    def on_phase(self, name: str) -> None: ...
    def on_turn(self, number: int, of_max: int) -> None: ...
    def on_thinking(self, text: str) -> None: ...
    def on_text(self, text: str) -> None: ...
    def on_tool_start(self, name: str) -> None: ...
    def on_tool_results(self, outcomes: list[ToolOutcome]) -> None: ...
    def on_web_search(self, query: str) -> None: ...
    def on_usage(self, usage: Usage) -> None: ...
    def on_warning(self, message: str) -> None: ...


@dataclass
class PlanResult:
    itinerary: Itinerary
    brief: str
    usage: Usage = field(default_factory=Usage)
    turns: int = 0
    tool_calls: int = 0


class AgentRefusal(RuntimeError):
    """Raised when the model declines the request outright."""


class TripPlanner:
    def __init__(
        self,
        observer: Observer | None = None,
        profile: TravellerProfile | None = None,
    ) -> None:
        self.settings = get_settings()
        self.observer = observer or Observer()
        # Rendered once. It is volatile per run, so it lives in the user message
        # rather than the system prompt, which must stay byte-stable to cache.
        self.profile_block = profile.as_prompt_block() if profile else ""
        # A single shared client: connection reuse is most of the latency win.
        self.client = AsyncAnthropic(api_key=self.settings.anthropic_api_key)
        self.usage = Usage()

    async def aclose(self, close_shared_http: bool = True) -> None:
        """Close this planner's Anthropic client, and optionally the shared one.

        This must happen on the same loop that opened them. Calling it from a
        second `asyncio.run()` raises "Event loop is closed", because httpx binds
        connections to the loop that created them.

        `close_shared_http` exists because the OSM/Open-Meteo client in
        `core.http` is a **process-wide singleton**. In the CLI, one planner owns
        the process, so closing it on the way out is right. In a server, several
        planners run concurrently — and one of them closing the shared client
        would break every other in-flight request. The API therefore passes
        `False` and closes the shared client once, at application shutdown.
        """
        await self.client.close()
        if close_shared_http:
            await close_client()

    # ------------------------------------------------------------------
    # Phase 1: research (the agentic loop)
    # ------------------------------------------------------------------
    async def research(self, request: str) -> tuple[str, int, int]:
        """Run the tool-calling loop until the model stops calling tools.

        Returns (research_brief, turns_used, tool_calls_made).
        """
        self.observer.on_phase("Research")

        tools = [*tool_definitions(), WEB_SEARCH_TOOL]
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": research_prompt(request, profile=self.profile_block)}
        ]

        # The cache breakpoint sits at the end of the system prompt, so the whole
        # stable prefix — tools, then system — is cached. Everything volatile
        # (today's date, the request) lives in `messages`, after the breakpoint.
        # Render order is always tools -> system -> messages, and caching is a
        # prefix match, so a single volatile byte up here would invalidate all of it.
        system = [
            {
                "type": "text",
                "text": RESEARCH_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        transcript: list[str] = []
        tool_calls = 0
        turn = 0
        # Set once web_search spins up a server-side execution container; see below.
        container_id: str | None = None

        while turn < self.settings.max_turns:
            turn += 1
            self.observer.on_turn(turn, self.settings.max_turns)

            request: dict[str, Any] = {
                "model": self.settings.model,
                "max_tokens": 32000,
                "system": system,
                "tools": tools,
                "messages": messages,
                # Adaptive lets the model choose its own reasoning depth per turn.
                # `display` is set explicitly because the default is "omitted",
                # which would make the terminal look frozen during a long think.
                "thinking": {"type": "adaptive", "display": "summarized"},
                "output_config": {"effort": self.settings.research_effort},
            }
            # The modern web_search tool does dynamic filtering by running code in
            # a server-side container. Once that container exists, every follow-up
            # request in the conversation must name it, or the API rejects the
            # turn with "container_id is required when there are pending tool uses
            # generated by code execution with tools".
            if container_id:
                request["container"] = container_id

            async with self.client.messages.stream(**request) as stream:
                async for event in stream:
                    self._relay(event)
                final = await stream.get_final_message()

            container = getattr(final, "container", None)
            if container is not None and getattr(container, "id", None):
                container_id = container.id

            self.usage.add(final.usage)
            self.observer.on_usage(self.usage)

            if final.stop_reason == "refusal":
                detail = getattr(final, "stop_details", None)
                raise AgentRefusal(
                    f"The model declined this request "
                    f"(category: {getattr(detail, 'category', 'unknown')})."
                )

            if final.stop_reason == "max_tokens":
                self.observer.on_warning(
                    "Response hit the max_tokens ceiling and was truncated mid-thought."
                )

            # `pause_turn` means a server-side tool (web search) hit its internal
            # iteration limit mid-turn. Send the turn straight back to resume it;
            # there are no tool results for us to supply.
            if final.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": final.content})
                continue

            text = "".join(b.text for b in final.content if b.type == "text")

            if final.stop_reason != "tool_use":
                transcript.append(text)
                break

            # Only `tool_use` blocks are ours. Server tools appear as
            # `server_tool_use` / `web_search_tool_result` and are already resolved.
            calls = [b for b in final.content if b.type == "tool_use"]
            if not calls:
                transcript.append(text)
                break

            if text.strip():
                transcript.append(text)

            messages.append({"role": "assistant", "content": final.content})

            outcomes = await dispatch_all([(b.name, dict(b.input), b.id) for b in calls])
            tool_calls += len(outcomes)
            self.observer.on_tool_results(outcomes)

            # THE critical line: every result in a single user message.
            messages.append({"role": "user", "content": [o.to_result_block() for o in outcomes]})
        else:
            self.observer.on_warning(
                f"Hit the {self.settings.max_turns}-turn ceiling. "
                "Assembling from what was gathered so far."
            )

        brief = "\n\n".join(t for t in transcript if t.strip())
        return brief, turn, tool_calls

    def _relay(self, event: Any) -> None:
        """Translate raw stream events into observer callbacks."""
        etype = getattr(event, "type", None)

        if etype == "content_block_start":
            block = event.content_block
            if block.type == "tool_use":
                self.observer.on_tool_start(block.name)
            elif block.type == "server_tool_use":
                self.observer.on_tool_start("web_search")

        elif etype == "content_block_delta":
            delta = event.delta
            dtype = getattr(delta, "type", None)
            if dtype == "thinking_delta":
                self.observer.on_thinking(delta.thinking)
            elif dtype == "text_delta":
                self.observer.on_text(delta.text)

    # ------------------------------------------------------------------
    # Phase 2: assembly (a single constrained call — not an agent)
    # ------------------------------------------------------------------
    async def _assemble_part(self, schema: type, system: str, request: str, brief: str):
        """One constrained call: no tools, no loop, no exploration.

        `output_format` constrains the response to the Pydantic schema and the SDK
        validates it, so `parsed_output` is a real model instance rather than a
        dict we hope has the right keys.
        """
        response = await self.client.messages.parse(
            model=self.settings.model,
            max_tokens=16000,
            system=system,
            messages=[
                {"role": "user", "content": assembly_prompt(request, brief, self.profile_block)}
            ],
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.settings.assembly_effort},
            output_format=schema,
        )
        self.usage.add(response.usage)

        parsed = response.parsed_output
        if parsed is None:  # pragma: no cover — schema violation
            raise RuntimeError(
                f"The model returned output that did not match the {schema.__name__} schema."
            )
        return parsed

    async def assemble(self, request: str, brief: str) -> Itinerary:
        """Turn the research brief into a validated `Itinerary`.

        Two constrained calls rather than one, for a hard API reason: structured
        outputs compile the schema into a decoding grammar, and the full
        `Itinerary` exceeds the size limit —

            400 invalid_request_error: The compiled grammar is too large

        Splitting it in half fixes that with no loss of fidelity (see
        `schemas.ItineraryCore`). The two halves read the same brief and neither
        depends on the other, so they run concurrently — the split costs latency
        only in tokens, not in wall-clock.

        Separating assembly from research at all is the difference between "the
        model wrote something" and "the system has data".
        """
        self.observer.on_phase("Assembly")

        core, logistics = await asyncio.gather(
            self._assemble_part(ItineraryCore, ASSEMBLY_CORE_SYSTEM, request, brief),
            self._assemble_part(ItineraryLogistics, ASSEMBLY_LOGISTICS_SYSTEM, request, brief),
        )

        self.observer.on_usage(self.usage)
        return Itinerary.from_parts(core, logistics)

    # ------------------------------------------------------------------
    async def plan(self, request: str) -> PlanResult:
        brief, turns, tool_calls = await self.research(request)
        if not brief.strip():
            raise RuntimeError(
                "Research phase produced no brief. Check your API key and connectivity."
            )
        itinerary = await self.assemble(request, brief)
        return PlanResult(
            itinerary=itinerary,
            brief=brief,
            usage=self.usage,
            turns=turns,
            tool_calls=tool_calls,
        )


def itinerary_json(itinerary: Itinerary) -> str:
    return json.dumps(itinerary.model_dump(mode="json"), indent=2, ensure_ascii=False)
