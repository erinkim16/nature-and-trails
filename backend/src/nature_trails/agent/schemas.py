"""The itinerary contract.

This is the most important file in the project for its *shape*, not its logic.

These Pydantic models are the boundary between "the model wrote something" and
"the system has data". Phase 2 of the agent is a pair of API calls constrained to
`ItineraryCore` and `ItineraryLogistics`, whose results merge into `Itinerary` —
so whatever comes back is guaranteed to have a `days` list, each day guaranteed to
have a `title`, and so on. That means:

* the CLI renderer can be dumb (no defensive `if "days" in result`)
* the HTTP API in `api.py` returns these models directly, and FastAPI generates
  the OpenAPI spec from them
* the Next.js frontend can generate TypeScript types from that OpenAPI spec, so
  the itinerary shape stays in sync across three languages from one definition
* an itinerary can be persisted to Postgres without writing a parser

Schema design notes
-------------------
**Every field is required.** Structured outputs work best with a strict schema
(`additionalProperties: false`, everything in `required`). Optionality is expressed
as `str | None`, not as a missing key. That way the model must make a *decision*
about every field — including deciding it does not know — rather than quietly
omitting things.

**No `dict[str, X]` fields.** A free-form mapping compiles to a schema with open
`additionalProperties`, which fights strict mode. Where a mapping is natural
(packing categories), it is modelled as a list of objects with an explicit key.

**`unknowns` is deliberate.** An agent that cannot say "I could not verify this"
will invent it instead. Giving the model a first-class place to record gaps makes
honesty the path of least resistance, and gives the user a to-do list.

**The schema is split in two, and that is not cosmetic.** Structured outputs
compile the schema into a constrained decoding grammar with a hard size limit.
The merged `Itinerary` exceeds it. See `ItineraryCore` below for the measurements.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Difficulty = Literal["easy", "moderate", "strenuous", "technical"]
AccommodationKind = Literal[
    "alpine_hut",
    "wilderness_hut",
    "mountain_refuge",
    "campsite",
    "wild_camp",
    "guesthouse",
    "hotel",
    "hostel",
    "none",
]


class Stage(BaseModel):
    """One day's walking. `None` on a rest or travel day."""

    start: str = Field(description="Where the day starts.")
    end: str = Field(description="Where the day ends.")
    distance_km: float | None = Field(description="Walking distance in km.")
    ascent_m: int | None = Field(description="Cumulative ascent in metres.")
    descent_m: int | None = Field(description="Cumulative descent in metres.")
    estimated_hours: float | None = Field(
        description="Realistic walking time including breaks, not the Naismith minimum."
    )
    high_point_m: int | None = Field(description="Highest elevation reached, in metres.")
    terrain: str = Field(
        description=(
            "What the ground is actually like, including SAC scale or equivalent "
            "difficulty rating, exposure, and any cable/ladder sections."
        )
    )


class Accommodation(BaseModel):
    name: str = Field(description="Name of the hut, campsite or hotel.")
    kind: AccommodationKind
    elevation_m: int | None = Field(description="Elevation in metres, if known.")
    booking: str = Field(
        description=(
            "How and when to book, and how far ahead it fills. Say so explicitly if "
            "booking is mandatory."
        )
    )
    contact: str | None = Field(description="Phone, website or email if known.")


class Day(BaseModel):
    day: int = Field(description="Day number, starting at 1.")
    date: str | None = Field(description="ISO date YYYY-MM-DD if the user gave firm dates.")
    title: str = Field(description="Short, concrete title for the day.")
    stage: Stage | None = Field(description="The day's walk. Null for travel/rest days.")
    accommodation: Accommodation | None = Field(description="Where the night is spent.")
    highlights: list[str] = Field(description="Two to four specific things worth seeing or doing.")
    weather_note: str = Field(
        description="What weather to expect on this day at this altitude, and its implications."
    )
    bad_weather_alternative: str = Field(
        description=(
            "What to do instead if the weather turns. Mountain itineraries without a "
            "bad-weather plan are how people get hurt."
        )
    )


class BookingTask(BaseModel):
    what: str = Field(description="What must be booked or obtained.")
    when_to_book: str = Field(description="How far in advance, e.g. 'as soon as possible'.")
    how: str = Field(description="The mechanism: online portal, phone, lottery, in person.")
    url: str | None = Field(description="Booking URL if known. Null rather than guessed.")


class BudgetLine(BaseModel):
    category: str = Field(description="e.g. huts, transport, food, gear rental, permits.")
    amount: float = Field(description="Estimated cost per person in the stated currency.")
    note: str = Field(description="What drives this number, and how confident it is.")


class Budget(BaseModel):
    currency: str = Field(description="ISO code, e.g. EUR, USD, CAD.")
    per_person_total: float
    fits_user_budget: bool | None = Field(
        description="Whether this fits the budget the user stated. Null if they gave none."
    )
    lines: list[BudgetLine]
    assumptions: str = Field(description="What the estimate assumes (season, sharing, board).")


class PackingItem(BaseModel):
    item: str
    why: str = Field(description="The specific reason for THIS trip, referencing real numbers.")


class PackingCategory(BaseModel):
    category: str
    items: list[str]


class Packing(BaseModel):
    total_weight_kg: float | None
    base_weight_kg: float | None = Field(description="Excluding food and water.")
    critical_items: list[PackingItem] = Field(
        description="The three to six items that genuinely matter for this specific trip."
    )
    by_category: list[PackingCategory]


class ItineraryCore(BaseModel):
    """Phase 2a: the narrative and the day-by-day plan.

    Split out from `Itinerary` for a concrete API reason. Structured outputs
    compile the JSON Schema into a constrained decoding grammar, and that grammar
    has a size limit — the full `Itinerary` returns:

        400 invalid_request_error: The compiled grammar is too large

    Measured while building this (see `docs/ARCHITECTURE.md` §5.1): the limit is
    driven by *structure* (nested models, fields, unions), not by prose. Field
    descriptions are essentially free — a variant with 400-character descriptions
    still compiled. So the fix is to split the request, not to strip the guidance
    that makes the output good.
    """

    title: str = Field(description="Evocative but accurate, e.g. 'Alta Via 1: Dolomites Traverse'.")
    destination: str
    country: str
    summary: str = Field(
        description="Two to three sentences on what this trip is and who it suits."
    )

    total_days: int
    difficulty: Difficulty
    difficulty_explanation: str = Field(
        description="Why this rating, in terms of daily ascent, exposure and technical sections."
    )
    total_distance_km: float | None
    total_ascent_m: int | None

    season_assessment: str = Field(
        description=(
            "Whether the requested dates are a good idea, using real climate figures. "
            "This is the single most consequential judgement in the plan — say plainly "
            "if the timing is wrong."
        )
    )
    best_months: list[str] = Field(description="Months this trip is genuinely in condition.")

    days: list[Day]


class ItineraryLogistics(BaseModel):
    """Phase 2b: everything that is not the day-by-day narrative.

    The second half of the split. Generated by its own constrained call, which
    runs concurrently with `ItineraryCore` — both read the same research brief and
    neither depends on the other's output, so there is no reason to serialise them.
    """

    getting_there: str = Field(description="Realistic route from a major airport to the trailhead.")
    getting_back: str = Field(
        description="How to return from the finish — the hard half of a point-to-point traverse."
    )
    car_free_feasible: bool = Field(description="Whether the trip works without renting a car.")

    permits_and_bookings: list[BookingTask]
    packing: Packing
    budget: Budget

    safety_notes: list[str] = Field(
        description="Concrete, route-specific hazards. Not generic advice."
    )
    unknowns: list[str] = Field(
        description=(
            "Anything that could not be verified with the tools and should be confirmed "
            "before departure. Be honest and specific — this is more useful than a "
            "confident guess."
        )
    )
    sources: list[str] = Field(
        description="Where the facts came from: OpenStreetMap, Open-Meteo, specific web pages."
    )


class Itinerary(ItineraryCore, ItineraryLogistics):
    """A complete, day-by-day wilderness trip plan.

    This is the type the rest of the application sees — renderers, the CLI, the
    JSON export, and eventually the FastAPI response model. It is assembled by
    merging the two halves above, and is deliberately **never** sent to the API as
    an `output_format`: that is exactly what blows the grammar limit.

    Defining it by inheritance rather than by re-declaring the fields keeps one
    source of truth. Add a field to whichever half it belongs to and it appears
    here automatically.
    """

    @classmethod
    def from_parts(cls, core: ItineraryCore, logistics: ItineraryLogistics) -> Itinerary:
        return cls(**core.model_dump(), **logistics.model_dump())
