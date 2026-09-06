"""Shared test fixtures.

`make_itinerary()` lives here because two test modules need a realistic
`Itinerary`: the renderer tests push it through the display layer, and the loop
tests need the two halves the assembly phase produces.
"""

from __future__ import annotations

from nature_trails.agent.schemas import (
    Accommodation,
    BookingTask,
    Budget,
    BudgetLine,
    Day,
    Itinerary,
    ItineraryCore,
    ItineraryLogistics,
    Packing,
    PackingCategory,
    PackingItem,
    Stage,
)


def make_itinerary() -> Itinerary:
    """A realistic itinerary, including a rest day with no stage."""
    return Itinerary(
        title="Alta Via 1: Dolomites Traverse",
        destination="Dolomiti Bellunesi",
        country="Italy",
        summary="A classic hut-to-hut traverse with [brackets] and an apostrophe's quote.",
        total_days=2,
        difficulty="strenuous",
        difficulty_explanation="Sustained 1,000 m+ ascent days on exposed ground.",
        total_distance_km=28.4,
        total_ascent_m=2350,
        season_assessment="Late September is workable but marginal; huts close around the 20th.",
        best_months=["July", "August", "early September"],
        days=[
            Day(
                day=1,
                date="2026-09-12",
                title="Lago di Braies to Rifugio Biella",
                stage=Stage(
                    start="Lago di Braies",
                    end="Rifugio Biella",
                    distance_km=8.2,
                    ascent_m=980,
                    descent_m=120,
                    estimated_hours=4.5,
                    high_point_m=2327,
                    terrain="SAC T2 with a steep scree ramp below Forcella Sora Forno.",
                ),
                accommodation=Accommodation(
                    name="Rifugio Biella",
                    kind="alpine_hut",
                    elevation_m=2327,
                    booking="Book by email months ahead; fills for September weekends.",
                    contact="+39 0436 866991",
                ),
                highlights=["Lago di Braies at dawn", "Croda del Becco"],
                weather_note="Highs near 9 C at hut altitude, overnight around 1 C.",
                bad_weather_alternative="Stay low and take the lake circuit instead.",
            ),
            Day(
                day=2,
                date=None,
                title="Rest and acclimatisation",
                stage=None,
                accommodation=None,
                highlights=[],
                weather_note="Unsettled.",
                bad_weather_alternative="Descend to Cortina by bus.",
            ),
        ],
        getting_there="Fly to Venice, train to Calalzo, bus to Lago di Braies.",
        getting_back="Bus from Belluno to Venice.",
        car_free_feasible=True,
        permits_and_bookings=[
            BookingTask(
                what="Rifugio Biella bed",
                when_to_book="As soon as possible",
                how="Email the hut directly",
                url="https://example.invalid/biella",
            ),
            BookingTask(
                what="Alpine club membership",
                when_to_book="Before departure",
                how="CAI online",
                url=None,
            ),
        ],
        packing=Packing(
            total_weight_kg=11.9,
            base_weight_kg=9.2,
            critical_items=[
                PackingItem(
                    item="Sleeping bag liner",
                    why="Mandatory at every staffed hut on this route.",
                ),
                PackingItem(
                    item="Microspikes",
                    why="Freezing level averages 2,700 m; the route tops out at 2,900 m.",
                ),
            ],
            by_category=[
                PackingCategory(category="clothing", items=["shell jacket", "warm hat"]),
                PackingCategory(category="safety", items=["headlamp", "first aid kit"]),
            ],
        ),
        budget=Budget(
            currency="EUR",
            per_person_total=780.0,
            fits_user_budget=True,
            lines=[
                BudgetLine(category="huts", amount=420.0, note="2 nights half board"),
                BudgetLine(category="transport", amount=360.0, note="Flights excluded"),
            ],
            assumptions="Assumes half board and shared dormitories.",
        ),
        safety_notes=["Forcella Sora Forno holds old snow into July."],
        unknowns=["2026 hut closing dates were not published at time of research."],
        sources=["OpenStreetMap", "Open-Meteo"],
    )


def split_itinerary() -> tuple[ItineraryCore, ItineraryLogistics]:
    """The two halves the assembly phase produces, derived from the full object.

    Deriving them by field name rather than hand-writing them means this also
    asserts that the split covers every field of `Itinerary` — if a field belonged
    to neither half, construction here would fail.
    """
    full = make_itinerary()
    core = ItineraryCore(**{k: getattr(full, k) for k in ItineraryCore.model_fields})
    logistics = ItineraryLogistics(**{k: getattr(full, k) for k in ItineraryLogistics.model_fields})
    return core, logistics
