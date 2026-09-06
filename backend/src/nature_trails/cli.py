"""Command-line interface.

This file is intentionally thin. It does three things: parse arguments, subclass
`Observer` to draw the agent's work as it happens, and hand the result to a
renderer. All the intelligence lives behind `TripPlanner.plan()`.

That thinness is the point. `api.py` is the same three things — parse a request,
subclass `Observer` to push Server-Sent Events, hand the result to a JSON
serialiser — and adding it required no change to `agent/`, `tools/` or `core/`.
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt
from rich.text import Text

from .agent import AgentRefusal, Observer, TripPlanner, Usage, itinerary_json
from .agent.intake import INTAKE_QUESTIONS, TravellerProfile
from .agent.loop import PlanResult
from .core.config import get_settings
from .render import render, to_markdown
from .tools import REGISTRY, ToolOutcome

# Windows terminals default to cp1252, which cannot encode most Alpine place names.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(
    add_completion=False,
    help="Plan wilderness trips: national parks, long-distance hikes, alpine traverses.",
)
console = Console()


class TerminalObserver(Observer):
    """Draws the agent's reasoning and tool use as it streams."""

    def __init__(self, show_thinking: bool = True) -> None:
        self.show_thinking = show_thinking
        self._in_thinking = False
        self._in_text = False

    def _close_stream(self) -> None:
        if self._in_thinking or self._in_text:
            console.print()
            self._in_thinking = self._in_text = False

    def on_phase(self, name: str) -> None:
        self._close_stream()
        console.print()
        console.rule(f"[bold cyan]{name}[/bold cyan]", style="cyan")

    def on_turn(self, number: int, of_max: int) -> None:
        self._close_stream()
        console.print(f"\n[dim]turn {number}/{of_max}[/dim]")

    def on_thinking(self, text: str) -> None:
        if not self.show_thinking:
            return
        if not self._in_thinking:
            self._close_stream()
            console.print("[dim italic]thinking[/dim italic]")
            self._in_thinking = True
        console.print(text, end="", style="dim italic", markup=False, highlight=False)

    def on_text(self, text: str) -> None:
        if not self._in_text:
            self._close_stream()
            self._in_text = True
        console.print(text, end="", markup=False, highlight=False)

    def on_tool_start(self, name: str) -> None:
        self._close_stream()
        console.print(f"  [cyan]→[/cyan] {name}")

    def on_tool_results(self, outcomes: list[ToolOutcome]) -> None:
        self._close_stream()
        for o in outcomes:
            mark = "[red]✗[/red]" if o.is_error else "[green]✓[/green]"
            detail = o.summary or ("error" if o.is_error else "")
            console.print(
                f"  {mark} [bold]{o.name}[/bold] [dim]{o.duration_s:.1f}s[/dim] [dim]{detail}[/dim]"
            )

    def on_usage(self, usage: Usage) -> None:
        pass  # printed once at the end; per-turn noise is not useful

    def on_warning(self, message: str) -> None:
        self._close_stream()
        console.print(f"[yellow]! {message}[/yellow]")


def _collect_profile() -> TravellerProfile:
    """The up-front intake: five things the agent cannot look up and always needs.

    Deliberately not an API call. This is plain input(), so it costs nothing and
    has no bearing on the agent loop — which is also why the same profile object
    will drop straight into a web form later.
    """
    console.print()
    console.print(
        Panel(
            Text.from_markup(
                "A few quick questions before I start. Press [bold]Enter[/bold] to "
                "accept the value in brackets.\n"
                "[dim]These are the answers that change the plan most, and the ones I "
                "cannot look up.[/dim]"
            ),
            title="[bold cyan]Before we start[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
    )

    answers: dict[str, object] = {}
    for attr, question, choices, default, why in INTAKE_QUESTIONS:
        console.print(f"\n  [dim]{why}[/dim]")
        label = f"  [bold]{question}[/bold]"
        try:
            if choices:
                answers[attr] = Prompt.ask(label, choices=choices, default=default)
            elif attr == "group_size":
                answers[attr] = IntPrompt.ask(label, default=int(default))
            else:
                answers[attr] = Prompt.ask(label, default=default, show_default=bool(default))
        except (EOFError, KeyboardInterrupt):
            # Ctrl+D or Ctrl+C part-way through. Keep what was answered, default
            # the rest, and carry on rather than dumping a traceback.
            console.print("\n  [dim]skipping the rest of the questions[/dim]")
            break

    return TravellerProfile(**answers)


def _slug(text: str, limit: int = 50) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned[:limit] or "itinerary"


def _print_usage(result: PlanResult) -> None:
    u = result.usage
    console.print(
        f"[dim]{result.turns} turns · {result.tool_calls} tool calls · "
        f"{u.requests} API requests[/dim]"
    )
    console.print(
        f"[dim]tokens: {u.input_tokens:,} in / {u.output_tokens:,} out · "
        f"cache {u.cache_read_tokens:,} read, {u.cache_write_tokens:,} written[/dim]"
    )
    console.print(f"[dim]estimated cost: ${u.estimated_cost_usd:.3f}[/dim]")
    if u.cache_read_tokens == 0 and u.requests > 2:
        console.print(
            "[yellow]No cache reads. Something volatile is changing in the "
            "tools+system prefix between calls.[/yellow]"
        )


def _key_problem(key: str | None) -> str | None:
    """Catch the two ways a key is 'set' but useless, before spending a round trip.

    An unset key is obvious. A key left as the placeholder from `.env.example` is
    the confusing case: it is truthy, so a naive check passes and the API returns an
    opaque 401 instead of naming the actual problem.
    """
    if not key or not key.strip():
        return "No ANTHROPIC_API_KEY found."
    stripped = key.strip()
    if stripped.strip("'\"") != stripped:
        return "Your ANTHROPIC_API_KEY has quotes around it — remove them."
    if "..." in stripped or stripped in {"sk-ant-", "your-key-here"}:
        return "ANTHROPIC_API_KEY is still the placeholder from .env.example."
    if not stripped.startswith("sk-ant-"):
        return (
            "ANTHROPIC_API_KEY does not look like an Anthropic key "
            "(it should start with 'sk-ant-')."
        )
    return None


async def _run(
    request: str,
    show_thinking: bool,
    out_dir: Path | None,
    profile: TravellerProfile,
) -> int:
    settings = get_settings()
    problem = _key_problem(settings.anthropic_api_key)
    if problem:
        console.print(
            f"[red]{problem}[/red]\n\n"
            "1. Copy [bold].env.example[/bold] to [bold].env[/bold] at the repo root\n"
            "2. Get a key at [dim]https://console.anthropic.com/settings/keys[/dim]\n"
            "   (a new account needs credits added under Billing first)\n"
            "3. Paste it after [bold]ANTHROPIC_API_KEY=[/bold] with no quotes\n"
        )
        return 1

    planner = TripPlanner(observer=TerminalObserver(show_thinking), profile=profile)
    try:
        result = await planner.plan(request)
    except AgentRefusal as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    finally:
        # Both HTTP clients must close on the loop that created them.
        await planner.aclose()

    render(result.itinerary, console)
    _print_usage(result)

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        base = f"{stamp}-{_slug(result.itinerary.title)}"
        md_path = out_dir / f"{base}.md"
        json_path = out_dir / f"{base}.json"
        md_path.write_text(to_markdown(result.itinerary), encoding="utf-8")
        json_path.write_text(itinerary_json(result.itinerary), encoding="utf-8")
        console.print(f"\n[green]saved[/green] {md_path}")
        console.print(f"[green]saved[/green] {json_path}")

    return 0


@app.command()
def plan(
    request: str = typer.Argument(
        ...,
        help=(
            "What you want, in plain English. Include destination, dates or month, "
            "budget, fitness and interests."
        ),
    ),
    out: Path | None = typer.Option(
        None, "--out", "-o", help="Directory to save Markdown + JSON versions."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Hide the model's reasoning while it works."
    ),
    no_intake: bool = typer.Option(
        False,
        "--no-intake",
        help=(
            "Skip the intake questions; infer everything from your request. Implied "
            "when stdin is not a terminal, so piped and scripted runs never stall."
        ),
    ),
) -> None:
    """Plan a trip.

    Example:

      trails plan "6 days hut-to-hut in the Dolomites in September, moderate
      fitness, budget 1200 EUR, love via ferrata and big views"
    """
    # A non-TTY stdin (a pipe, cron, CI) cannot answer the intake questions.
    # Prompting anyway would read EOF, so treat it as --no-intake.
    if not no_intake and sys.stdin.isatty():
        profile = _collect_profile()
    else:
        profile = TravellerProfile()

    raise typer.Exit(
        asyncio.run(_run(request, show_thinking=not quiet, out_dir=out, profile=profile))
    )


@app.command()
def tools() -> None:
    """List the tools the agent can call, and what each is for."""
    from rich.table import Table

    table = Table(title="Registered tools", border_style="dim", expand=True)
    table.add_column("Tool", style="bold cyan", ratio=1)
    table.add_column("Parameters", style="dim", ratio=1)
    table.add_column("Purpose", ratio=3)
    for name in sorted(REGISTRY):
        spec = REGISTRY[name]
        first_para = spec.description.strip().split("\n\n")[0].replace("\n", " ")
        params = ", ".join(f"{p}*" if p in spec.required else p for p in spec.properties)
        table.add_row(name, params, first_para)
    console.print(table)
    console.print("[dim]* = required parameter[/dim]")
    console.print(
        f"[dim]{len(REGISTRY)} local tools, plus Anthropic's server-side web_search.[/dim]"
    )


if __name__ == "__main__":
    app()
