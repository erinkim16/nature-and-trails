"""Rendering an `Itinerary` for humans.

Two renderers, one source of truth. Because Phase 2 guarantees a validated
`Itinerary`, both of these are simple, non-defensive transforms — no
`result.get("days", [])`, no "did the model include a budget this time".

That is the practical payoff of the two-phase design: the presentation layer stops
being error-handling code. When the Next.js frontend arrives it becomes a third
renderer over the same object, and this file does not change.
"""

from __future__ import annotations

from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..agent.schemas import Itinerary

# Every string in an `Itinerary` was written by a model. Rich treats square
# brackets as markup, so a hut named "Rifugio [Alto]" or a note containing "[sic]"
# would either vanish or raise. `Text(...)` is literal and needs no escaping, but
# anywhere we build a markup string by interpolation, model text must be escaped.

DIFFICULTY_STYLE = {
    "easy": "green",
    "moderate": "yellow",
    "strenuous": "dark_orange",
    "technical": "red",
}


def _stat_line(itinerary: Itinerary) -> Text:
    parts = [f"{itinerary.total_days} days"]
    if itinerary.total_distance_km:
        parts.append(f"{itinerary.total_distance_km:g} km")
    if itinerary.total_ascent_m:
        parts.append(f"{itinerary.total_ascent_m:,} m ascent")
    text = Text("  ·  ".join(parts), style="bold")
    text.append("     ")
    text.append(
        itinerary.difficulty.upper(),
        style=f"bold {DIFFICULTY_STYLE.get(itinerary.difficulty, 'white')}",
    )
    return text


def render(itinerary: Itinerary, console: Console | None = None) -> None:
    console = console or Console()
    it = itinerary

    console.print()
    console.print(
        Panel(
            Group(
                Text(it.title, style="bold cyan"),
                Text(f"{it.destination}, {it.country}", style="dim"),
                Text(),
                Text(it.summary),
                Text(),
                _stat_line(it),
            ),
            border_style="cyan",
            padding=(1, 2),
        )
    )

    # Season verdict first — it is the judgement most likely to change the trip.
    console.print(
        Panel(
            Text(it.season_assessment),
            title="[bold]Season assessment[/bold]",
            subtitle=f"[dim]best months: {escape(', '.join(it.best_months))}[/dim]",
            border_style="yellow",
            padding=(1, 2),
        )
    )

    console.print()
    console.rule("[bold]Day by day[/bold]", style="dim")

    for day in it.days:
        header = Text(f"Day {day.day}", style="bold white on dark_blue")
        header.append(f"  {day.title}", style="bold")
        if day.date:
            header.append(f"   {day.date}", style="dim")

        body: list = [header, Text()]

        if day.stage:
            s = day.stage
            stage_table = Table.grid(padding=(0, 2))
            stage_table.add_column(style="dim", justify="right")
            stage_table.add_column()
            stage_table.add_row("route", f"{s.start} → {s.end}")
            metrics = []
            if s.distance_km:
                metrics.append(f"{s.distance_km:g} km")
            if s.ascent_m:
                metrics.append(f"+{s.ascent_m:,} m")
            if s.descent_m:
                metrics.append(f"−{s.descent_m:,} m")
            if s.estimated_hours:
                metrics.append(f"~{s.estimated_hours:g} h")
            if s.high_point_m:
                metrics.append(f"high {s.high_point_m:,} m")
            if metrics:
                stage_table.add_row("stats", "   ".join(metrics))
            stage_table.add_row("terrain", s.terrain)
            body.append(stage_table)
            body.append(Text())

        if day.accommodation:
            a = day.accommodation
            label = a.name
            if a.elevation_m:
                label += f"  ({a.elevation_m:,} m)"
            acc = Table.grid(padding=(0, 2))
            acc.add_column(style="dim", justify="right")
            acc.add_column()
            acc.add_row(
                "night",
                f"[bold]{escape(label)}[/bold]  [dim]{a.kind.replace('_', ' ')}[/dim]",
            )
            acc.add_row("booking", a.booking)
            if a.contact:
                acc.add_row("contact", a.contact)
            body.append(acc)
            body.append(Text())

        if day.highlights:
            hl = Text("highlights", style="dim")
            body.append(hl)
            for h in day.highlights:
                body.append(Text(f"  • {h}"))
            body.append(Text())

        weather = Table.grid(padding=(0, 2))
        weather.add_column(style="dim", justify="right")
        weather.add_column()
        weather.add_row("weather", day.weather_note)
        weather.add_row("if it turns", day.bad_weather_alternative)
        body.append(weather)

        console.print(Panel(Group(*body), border_style="dim", padding=(1, 2)))

    # --- Logistics -------------------------------------------------------
    console.print()
    console.rule("[bold]Logistics[/bold]", style="dim")
    access = Table.grid(padding=(0, 2))
    access.add_column(style="dim", justify="right")
    access.add_column()
    access.add_row("getting there", it.getting_there)
    access.add_row("getting back", it.getting_back)
    access.add_row(
        "without a car",
        "[green]feasible[/green]" if it.car_free_feasible else "[red]not realistic[/red]",
    )
    console.print(Panel(access, border_style="dim", padding=(1, 2)))

    if it.permits_and_bookings:
        table = Table(
            title="Book / obtain in advance", title_style="bold", border_style="dim", expand=True
        )
        table.add_column("What", style="bold", ratio=2)
        table.add_column("When", ratio=1)
        table.add_column("How", ratio=3)
        for task in it.permits_and_bookings:
            how = escape(task.how) + (
                f"\n[dim blue]{escape(task.url)}[/dim blue]" if task.url else ""
            )
            table.add_row(escape(task.what), escape(task.when_to_book), how)
        console.print(table)

    # --- Packing ---------------------------------------------------------
    console.print()
    console.rule("[bold]Packing[/bold]", style="dim")
    p = it.packing
    weights = []
    if p.total_weight_kg:
        weights.append(f"total ~{p.total_weight_kg:g} kg")
    if p.base_weight_kg:
        weights.append(f"base ~{p.base_weight_kg:g} kg")
    if weights:
        console.print(Text("  ".join(weights), style="bold"))
        console.print()

    if p.critical_items:
        crit = Table(
            title="What actually matters for this trip",
            title_style="bold",
            border_style="dim",
            expand=True,
        )
        crit.add_column("Item", style="bold", ratio=1)
        crit.add_column("Why", ratio=3)
        for item in p.critical_items:
            crit.add_row(item.item, item.why)
        console.print(crit)

    if p.by_category:
        console.print()
        grid = Table.grid(padding=(0, 3))
        grid.add_column(style="dim", justify="right", vertical="top")
        grid.add_column()
        for cat in p.by_category:
            grid.add_row(cat.category, ", ".join(cat.items))
        console.print(grid)

    # --- Budget ----------------------------------------------------------
    console.print()
    console.rule("[bold]Budget[/bold]", style="dim")
    b = it.budget
    budget_table = Table(border_style="dim", expand=True)
    budget_table.add_column("Category", style="bold")
    budget_table.add_column("Per person", justify="right")
    budget_table.add_column("Note", ratio=3)
    for line in b.lines:
        budget_table.add_row(line.category, f"{line.amount:,.0f} {b.currency}", line.note)
    budget_table.add_section()
    budget_table.add_row(
        "[bold]TOTAL[/bold]", f"[bold]{b.per_person_total:,.0f} {b.currency}[/bold]", ""
    )
    console.print(budget_table)
    if b.fits_user_budget is not None:
        verdict = (
            "[green]Within your stated budget.[/green]"
            if b.fits_user_budget
            else "[red]Over your stated budget.[/red]"
        )
        console.print(verdict)
    console.print(Text(b.assumptions, style="dim"))

    # --- Safety and honesty ----------------------------------------------
    if it.safety_notes:
        console.print()
        console.print(
            Panel(
                Group(*[Text(f"• {n}") for n in it.safety_notes]),
                title="[bold]Safety[/bold]",
                border_style="red",
                padding=(1, 2),
            )
        )

    if it.unknowns:
        console.print(
            Panel(
                Group(*[Text(f"• {u}") for u in it.unknowns]),
                title="[bold]Verify before you commit[/bold]",
                border_style="yellow",
                padding=(1, 2),
            )
        )

    if it.sources:
        console.print(Text("sources: " + "; ".join(it.sources), style="dim"))
    console.print()


def to_markdown(itinerary: Itinerary) -> str:
    """Portable Markdown export — for sharing, or for feeding the web app later."""
    it = itinerary
    out: list[str] = [f"# {it.title}", ""]
    out.append(f"**{it.destination}, {it.country}**")
    out.append("")
    out.append(it.summary)
    out.append("")

    stats = [f"{it.total_days} days"]
    if it.total_distance_km:
        stats.append(f"{it.total_distance_km:g} km")
    if it.total_ascent_m:
        stats.append(f"{it.total_ascent_m:,} m ascent")
    stats.append(f"difficulty: **{it.difficulty}**")
    out.append(" · ".join(stats))
    out.append("")
    out.append(f"> {it.difficulty_explanation}")
    out.append("")

    out.append("## Season")
    out.append("")
    out.append(it.season_assessment)
    out.append("")
    out.append(f"*Best months: {', '.join(it.best_months)}*")
    out.append("")

    out.append("## Day by day")
    out.append("")
    for day in it.days:
        heading = f"### Day {day.day} — {day.title}"
        if day.date:
            heading += f" ({day.date})"
        out.append(heading)
        out.append("")
        if day.stage:
            s = day.stage
            bits = [f"**{s.start} → {s.end}**"]
            metrics = []
            if s.distance_km:
                metrics.append(f"{s.distance_km:g} km")
            if s.ascent_m:
                metrics.append(f"+{s.ascent_m:,} m")
            if s.descent_m:
                metrics.append(f"−{s.descent_m:,} m")
            if s.estimated_hours:
                metrics.append(f"~{s.estimated_hours:g} h")
            if s.high_point_m:
                metrics.append(f"high point {s.high_point_m:,} m")
            if metrics:
                bits.append(" · ".join(metrics))
            out.append("  \n".join(bits))
            out.append("")
            out.append(f"*Terrain:* {s.terrain}")
            out.append("")
        if day.accommodation:
            a = day.accommodation
            label = a.name + (f" ({a.elevation_m:,} m)" if a.elevation_m else "")
            out.append(f"*Night:* **{label}** — {a.kind.replace('_', ' ')}")
            out.append("")
            out.append(f"*Booking:* {a.booking}")
            if a.contact:
                out.append(f"  \n*Contact:* {a.contact}")
            out.append("")
        if day.highlights:
            for h in day.highlights:
                out.append(f"- {h}")
            out.append("")
        out.append(f"*Weather:* {day.weather_note}")
        out.append("")
        out.append(f"*If it turns:* {day.bad_weather_alternative}")
        out.append("")

    out.append("## Getting there and back")
    out.append("")
    out.append(f"**Out:** {it.getting_there}")
    out.append("")
    out.append(f"**Back:** {it.getting_back}")
    out.append("")
    out.append(f"**Car-free:** {'feasible' if it.car_free_feasible else 'not realistic'}")
    out.append("")

    if it.permits_and_bookings:
        out.append("## Book in advance")
        out.append("")
        out.append("| What | When | How |")
        out.append("|---|---|---|")
        for t in it.permits_and_bookings:
            how = t.how + (f" — {t.url}" if t.url else "")
            out.append(f"| {t.what} | {t.when_to_book} | {how} |")
        out.append("")

    out.append("## Packing")
    out.append("")
    p = it.packing
    if p.total_weight_kg or p.base_weight_kg:
        out.append(f"*Total ~{p.total_weight_kg or '?'} kg · base ~{p.base_weight_kg or '?'} kg*")
        out.append("")
    for item in p.critical_items:
        out.append(f"- **{item.item}** — {item.why}")
    out.append("")
    for cat in p.by_category:
        out.append(f"**{cat.category}:** {', '.join(cat.items)}")
        out.append("")

    out.append("## Budget")
    out.append("")
    b = it.budget
    out.append(f"| Category | Per person ({b.currency}) | Note |")
    out.append("|---|---:|---|")
    for line in b.lines:
        out.append(f"| {line.category} | {line.amount:,.0f} | {line.note} |")
    out.append(f"| **Total** | **{b.per_person_total:,.0f}** | |")
    out.append("")
    out.append(f"*{b.assumptions}*")
    out.append("")

    if it.safety_notes:
        out.append("## Safety")
        out.append("")
        for n in it.safety_notes:
            out.append(f"- {n}")
        out.append("")

    if it.unknowns:
        out.append("## Verify before you commit")
        out.append("")
        for u in it.unknowns:
            out.append(f"- {u}")
        out.append("")

    if it.sources:
        out.append("---")
        out.append("")
        out.append(f"*Sources: {'; '.join(it.sources)}*")

    return "\n".join(out)
