"""Tool package.

Importing this module has a side effect: it imports every tool module, which runs
the `@tool` decorators and populates `registry.REGISTRY`. Nothing else needs to
know which modules exist — `agent.loop` just imports this package and asks the
registry for definitions.

Adding a new tool is therefore a two-line change: write the decorated function in
a module, and make sure the module is imported here.
"""

from . import packing, places, trails, weather  # noqa: F401 — imported for side effects
from .registry import REGISTRY, ToolOutcome, dispatch, dispatch_all, tool, tool_definitions

__all__ = [
    "REGISTRY",
    "ToolOutcome",
    "dispatch",
    "dispatch_all",
    "tool",
    "tool_definitions",
]
