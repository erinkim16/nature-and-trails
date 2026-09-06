"""Traveller profile: the handful of facts that always change the plan.

Why this is a questionnaire and not a conversation
--------------------------------------------------
The obvious design is to give the agent a tool for asking the traveller questions
mid-run. That works beautifully in a terminal — the coroutine simply suspends on
`input()` and resumes on the next keystroke — and a genuine architectural problem on
the web, where the agent must pause, hold or persist its whole conversation, and
resume when an answer arrives over the network. On serverless hosting it is not
possible at all.

So this project deliberately collects preferences **once, up front, before the agent
starts**. That covers the facts that are decision-relevant for every trip and almost
never present in a free-text request, costs nothing (no API call, no pause/resume),
and ports to the web as an ordinary HTML form. The agent is told plainly that it has
no channel back to the traveller, so it states its assumptions instead of stalling.

Anything narrower than what is collected here, the agent handles by choosing the
best-supported reading and recording the assumption in the itinerary's `unknowns`.

Why these five and not fifteen
------------------------------
Each one changes the *output*, not just the flavour:

* **Nationality** changes two fields the itinerary schema already has. An American
  needs an America the Beautiful pass and Recreation.gov lotteries; an EU citizen
  gets 30-50% hut discounts and helicopter-rescue cover through alpine club
  reciprocity (CAI/DAV/OeAV); a non-EU visitor faces the Schengen 90-day limit.
  Without this the agent recommends memberships that may not apply.
* **Accommodation** changes the shape of the whole trip and most of the packing
  list — hut trips need no tent, stove or sleeping bag, camping needs all three.
* **Group size** changes hut-booking difficulty (a party of 6 is much harder to
  place than a solo walker) and the per-person cost of anything shared.
* **Exposure comfort** is a safety gate. It decides whether via ferrata and SAC T4
  terrain are on the table at all.
* **Date flexibility** unlocks the single most valuable thing this agent can say:
  "shift two weeks and this goes from marginal to excellent."

Anything narrower than this, the agent infers and flags rather than asks about.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

ACCOMMODATION_CHOICES = ["huts", "camping", "mix", "comfortable", "no preference"]
EXPOSURE_CHOICES = ["avoid", "some", "comfortable"]
FLEXIBILITY_CHOICES = ["fixed", "a few days", "flexible"]


class TravellerProfile(BaseModel):
    """What we know about the traveller before research starts."""

    nationality: str = Field(default="", description="Passport country, or empty if not given.")
    accommodation: str = Field(default="no preference")
    group_size: int = Field(default=1)
    exposure_comfort: str = Field(default="some")
    date_flexibility: str = Field(default="a few days")

    def is_default(self) -> bool:
        return self == TravellerProfile()

    def as_prompt_block(self) -> str:
        """Render for the research prompt, with the implications spelled out.

        The agent gets told *why* each answer matters, because a bare
        "nationality: Canadian" invites it to be ignored.
        """
        if self.is_default():
            return ""

        lines = ["Traveller profile (established before research):"]

        if self.nationality:
            lines.append(
                f"- Passport / nationality: {self.nationality}. Use this for permit "
                "eligibility, national park passes, visa limits, and alpine club "
                "reciprocity — recommend only memberships and passes that actually "
                "apply to this traveller, and price them accordingly."
            )
        else:
            lines.append(
                "- Nationality: not given. Do not assume one. Where a permit, park "
                "pass or club membership depends on it, say which nationalities the "
                "advice applies to."
            )

        if self.accommodation != "no preference":
            meaning = {
                "huts": "staffed mountain huts/refuges — no tent, stove or sleeping bag needed",
                "camping": "tent throughout — carrying shelter, sleep system and cooking gear",
                "mix": "a mix of huts and camping",
                "comfortable": "hotels or guesthouses, day-hiking from a base",
            }[self.accommodation]
            lines.append(f"- Accommodation: {self.accommodation} ({meaning}).")

        lines.append(
            f"- Group size: {self.group_size}."
            + (
                " Larger parties are much harder to place in huts — check capacity "
                "and flag it as a booking risk."
                if self.group_size >= 4
                else ""
            )
        )

        exposure = {
            "avoid": (
                "wants to AVOID exposure. Do not include via ferrata, SAC T4+ terrain, "
                "or unprotected scrambling. If the obvious route requires it, say so "
                "and route around it."
            ),
            "some": (
                "is comfortable with some exposure — waymarked mountain paths and short "
                "protected sections. Flag anything at SAC T4 or above explicitly."
            ),
            "comfortable": (
                "is comfortable with exposure and happy on via ferrata and scrambling. "
                "Still state the grade and the gear each protected section needs."
            ),
        }[self.exposure_comfort]
        lines.append(f"- Exposure: {exposure}")

        flexibility = {
            "fixed": (
                "Dates are FIXED. Do not propose shifting them; plan the best trip "
                "possible in that window and be honest about its limitations."
            ),
            "a few days": (
                "Dates can move by a few days. Use that if it materially improves "
                "conditions or hut availability."
            ),
            "flexible": (
                "Dates are FLEXIBLE. If a different window is clearly better, say so "
                "directly and explain what it buys."
            ),
        }[self.date_flexibility]
        lines.append(f"- Timing: {flexibility}")

        return "\n".join(lines)


# (attribute, question, choices or None, default, why-it-matters shown to the user)
INTAKE_QUESTIONS: list[tuple[str, str, list[str] | None, str, str]] = [
    (
        "nationality",
        "Passport / nationality",
        None,
        "",
        "decides park passes, permit lotteries, visa limits and alpine-club discounts",
    ),
    (
        "accommodation",
        "Where would you rather sleep",
        ACCOMMODATION_CHOICES,
        "no preference",
        "changes the whole trip shape and most of the packing list",
    ),
    (
        "group_size",
        "How many people",
        None,
        "1",
        "larger parties are much harder to book into huts",
    ),
    (
        "exposure_comfort",
        "Comfort with exposure and scrambling",
        EXPOSURE_CHOICES,
        "some",
        "gates whether via ferrata and technical terrain are on the table",
    ),
    (
        "date_flexibility",
        "How fixed are your dates",
        FLEXIBILITY_CHOICES,
        "a few days",
        "lets the agent tell you if a different window is much better",
    ),
]
