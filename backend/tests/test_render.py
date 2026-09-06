"""Tests for the renderers.

Rendering code is where crashes hide: it is rarely exercised during development,
and a bad Rich markup string or a None where a number was expected only shows up
at the very end of an expensive agent run. These tests push a full `Itinerary`
through both renderers.

The `sparse` variant matters as much as the full one — it covers rest days with no
stage, missing elevations and empty optional lists, which is what real output looks
like when the agent could not verify something.
"""

from __future__ import annotations

import io

from conftest import make_itinerary
from rich.console import Console

from nature_trails.agent.schemas import BookingTask, Budget, Itinerary, Packing
from nature_trails.render import render, to_markdown


def _sparse_itinerary() -> Itinerary:
    """The shape real output takes when much could not be verified."""
    return make_itinerary().model_copy(
        update={
            "total_distance_km": None,
            "total_ascent_m": None,
            "best_months": [],
            "permits_and_bookings": [],
            "safety_notes": [],
            "unknowns": [],
            "sources": [],
            "packing": Packing(
                total_weight_kg=None,
                base_weight_kg=None,
                critical_items=[],
                by_category=[],
            ),
            "budget": Budget(
                currency="USD",
                per_person_total=0.0,
                fits_user_budget=None,
                lines=[],
                assumptions="Not estimated.",
            ),
        }
    )


def _render_to_string(itinerary: Itinerary) -> str:
    buffer = io.StringIO()
    console = Console(file=buffer, width=100, force_terminal=False, no_color=True)
    render(itinerary, console)
    return buffer.getvalue()


def test_terminal_render_includes_the_key_facts():
    out = _render_to_string(make_itinerary())
    assert "Alta Via 1" in out
    assert "Rifugio Biella" in out
    assert "STRENUOUS" in out
    assert "Season assessment" in out
    assert "Verify before you commit" in out


def test_terminal_render_survives_missing_optional_data():
    """Rest days, absent budgets and empty lists must not crash the renderer."""
    out = _render_to_string(_sparse_itinerary())
    assert "Rest and acclimatisation" in out


def test_markdown_export_is_wellformed():
    md = to_markdown(make_itinerary())
    assert md.startswith("# Alta Via 1")
    assert "### Day 1 — Lago di Braies to Rifugio Biella" in md
    assert "### Day 2 — Rest and acclimatisation" in md
    assert "| Category | Per person (EUR) | Note |" in md
    assert "Sleeping bag liner" in md
    assert "## Verify before you commit" in md


def test_markdown_export_handles_sparse_data():
    md = to_markdown(_sparse_itinerary())
    assert "# Alta Via 1" in md
    assert "## Verify before you commit" not in md


def test_rich_markup_in_model_text_is_not_interpreted():
    """Model output containing square brackets must not be parsed as Rich markup."""
    out = _render_to_string(make_itinerary())
    assert "[brackets]" in out


def test_brackets_survive_every_markup_interpolation_site():
    """Every field spliced into a markup string must be escaped.

    Rich reads `[...]` as a style tag. An unescaped hut name like "Rifugio [Alto]"
    would silently disappear from the output — or raise on an unknown style. These
    are the three places `terminal.py` builds markup by interpolation.
    """
    base = make_itinerary()
    day = base.days[0]
    hostile = base.model_copy(
        update={
            "best_months": ["July [peak]"],
            "days": [
                day.model_copy(
                    update={
                        "accommodation": day.accommodation.model_copy(
                            update={"name": "Rifugio [Alto]"}
                        )
                    }
                ),
                base.days[1],
            ],
            "permits_and_bookings": [
                BookingTask(
                    what="Permit [lottery]",
                    when_to_book="January [strict]",
                    how="Apply via [portal]",
                    url="https://example.invalid/a[b]",
                )
            ],
        }
    )

    out = _render_to_string(hostile)
    assert "Rifugio [Alto]" in out
    assert "July [peak]" in out
    assert "Permit [lottery]" in out
    assert "[strict]" in out
