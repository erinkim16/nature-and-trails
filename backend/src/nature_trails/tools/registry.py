"""Tool registration and dispatch.

The `@tool` decorator turns an ordinary async Python function into something the
Messages API can call, and records it in a dispatch table.

Design choices worth understanding
----------------------------------

**Schemas are written by hand, not derived from type hints.** Auto-generating a
JSON Schema from a signature is a fun exercise and the wrong call here. The
`description` on each tool and *each parameter* is the single biggest lever on
agent quality — it is the only documentation the model ever sees. Hand-writing
them forces a decision about what the model needs to know ("radius_km: keep under
30 for dense alpine areas or the query will time out"). Generated schemas produce
descriptions like "param: str" and the agent flounders.

**Tools are sorted by name in `tool_definitions()`.** The API renders the request
as tools -> system -> messages, and prompt caching is a *prefix* match. If the tool
list changes byte-order between calls, every cached token after it is invalidated.
Dict ordering in Python is insertion-ordered and depends on import order, which
depends on which module got imported first — sorting removes that whole class of
"why is my cache hit rate zero" bug.

**Failures return data, not exceptions.** When a tool raises, the model must still
receive a `tool_result` (the API rejects an unanswered `tool_use` block). More
importantly, a good error message is *actionable*: "no huts found within 10 km, try
a larger radius" lets the agent recover on the next turn. A stack trace does not.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# Tool results are injected straight back into the context window. A single
# runaway Overpass response can be megabytes, so we cap what we hand back.
MAX_RESULT_CHARS = 60_000


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    properties: dict[str, Any]
    required: list[str]
    fn: Callable[..., Awaitable[Any]]
    strict: bool = False

    def to_api_definition(self) -> dict[str, Any]:
        definition: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": self.properties,
                "required": self.required,
            },
        }
        if self.strict:
            # Strict mode guarantees `tool_use.input` validates exactly against the
            # schema. It requires additionalProperties:false, and every property to
            # appear in `required` (make optionals nullable instead of omitting).
            definition["input_schema"]["additionalProperties"] = False
            definition["strict"] = True
        return definition


REGISTRY: dict[str, ToolSpec] = {}


def tool(
    *,
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
    strict: bool = False,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Register an async function as an agent-callable tool."""

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"tool {name!r} must be an async function")
        if name in REGISTRY:
            raise ValueError(f"duplicate tool name {name!r}")
        REGISTRY[name] = ToolSpec(
            name=name,
            description=inspect.cleandoc(description),
            properties=properties,
            required=required or [],
            fn=fn,
            strict=strict,
        )
        return fn

    return decorator


def tool_definitions() -> list[dict[str, Any]]:
    """All registered tools, in a stable order (see module docstring)."""
    return [REGISTRY[key].to_api_definition() for key in sorted(REGISTRY)]


@dataclass
class ToolOutcome:
    """What a single tool call produced, for both the API and the terminal UI."""

    tool_use_id: str
    name: str
    content: str
    is_error: bool = False
    duration_s: float = 0.0
    summary: str = ""

    def to_result_block(self) -> dict[str, Any]:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": self.tool_use_id,
            "content": self.content,
        }
        if self.is_error:
            block["is_error"] = True
        return block


def _summarise(name: str, payload: Any) -> str:
    """One-line human summary for the live terminal display."""
    if isinstance(payload, dict):
        for key in ("results", "routes", "huts", "amenities", "items", "days"):
            if isinstance(payload.get(key), list):
                return f"{len(payload[key])} {key}"
        if "name" in payload:
            return str(payload["name"])
        if "summary" in payload:
            return str(payload["summary"])[:80]
    if isinstance(payload, list):
        return f"{len(payload)} items"
    return ""


async def dispatch(name: str, arguments: dict[str, Any], tool_use_id: str) -> ToolOutcome:
    """Execute one tool call, converting any failure into an actionable message."""
    started = asyncio.get_running_loop().time()
    spec = REGISTRY.get(name)
    if spec is None:
        return ToolOutcome(
            tool_use_id=tool_use_id,
            name=name,
            content=f"Unknown tool {name!r}. Available: {', '.join(sorted(REGISTRY))}",
            is_error=True,
        )

    try:
        payload = await spec.fn(**arguments)
        content = json.dumps(payload, ensure_ascii=False, default=str)
        truncated = False
        if len(content) > MAX_RESULT_CHARS:
            content = content[:MAX_RESULT_CHARS]
            truncated = True
        if truncated:
            content += (
                '\n\n["TRUNCATED: result exceeded the size limit. Narrow the query — '
                'use a smaller radius, a tighter bounding box, or a stricter filter."]'
            )
        return ToolOutcome(
            tool_use_id=tool_use_id,
            name=name,
            content=content,
            duration_s=asyncio.get_running_loop().time() - started,
            summary=_summarise(name, payload),
        )
    except TypeError as exc:
        # Almost always the model passing a wrong/missing argument.
        return ToolOutcome(
            tool_use_id=tool_use_id,
            name=name,
            content=(
                f"Invalid arguments for {name}: {exc}. "
                f"Expected parameters: {', '.join(spec.properties)}."
            ),
            is_error=True,
            duration_s=asyncio.get_running_loop().time() - started,
        )
    except Exception as exc:  # noqa: BLE001 — deliberate: never kill the loop
        log.warning("tool %s failed", name, exc_info=True)
        return ToolOutcome(
            tool_use_id=tool_use_id,
            name=name,
            content=(
                f"Tool {name} failed: {type(exc).__name__}: {exc}. "
                "This data source may be temporarily unavailable — either retry once, "
                "try a different tool, or proceed and note the gap in your findings."
            ),
            is_error=True,
            duration_s=asyncio.get_running_loop().time() - started,
        )


async def dispatch_all(calls: list[tuple[str, dict[str, Any], str]]) -> list[ToolOutcome]:
    """Run every tool call in a turn concurrently.

    Claude routinely emits 3-6 `tool_use` blocks in one assistant message. Running
    them sequentially would make a research turn take 30s instead of 6s. The
    rate limiter in `core.http` keeps the concurrency polite per host.
    """
    return list(await asyncio.gather(*(dispatch(name, args, tid) for name, args, tid in calls)))
