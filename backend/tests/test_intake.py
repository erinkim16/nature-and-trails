"""Tests for the traveller profile collected by the up-front intake.

The profile is the only channel the traveller has into the agent — there is no
mid-run questioning — so what matters is that each answer arrives with its
*consequences* spelled out. A bare "nationality: Canadian" invites the model to
ignore it; the assertions below pin down that the implications travel with it.
"""

from __future__ import annotations

import pytest

from nature_trails.agent.intake import INTAKE_QUESTIONS, TravellerProfile

pytestmark = pytest.mark.asyncio


async def test_default_profile_adds_nothing_to_the_prompt():
    """No answers means no prompt noise, and no invented preferences."""
    assert TravellerProfile().is_default()
    assert TravellerProfile().as_prompt_block() == ""


async def test_nationality_reaches_the_prompt_with_its_implications():
    block = TravellerProfile(nationality="Canadian").as_prompt_block()
    assert "Canadian" in block
    # A bare label invites the model to ignore it; the consequences must be stated.
    assert "park pass" in block.lower()
    assert "reciprocity" in block.lower()


async def test_missing_nationality_forbids_assuming_one():
    block = TravellerProfile(accommodation="camping").as_prompt_block()
    assert "not given" in block
    assert "Do not assume" in block


async def test_avoiding_exposure_forbids_via_ferrata():
    block = TravellerProfile(exposure_comfort="avoid").as_prompt_block()
    assert "via ferrata" in block.lower()
    assert "avoid" in block.lower()


async def test_fixed_dates_forbid_proposing_a_shift():
    fixed = TravellerProfile(date_flexibility="fixed").as_prompt_block()
    flexible = TravellerProfile(date_flexibility="flexible").as_prompt_block()
    assert "FIXED" in fixed
    assert "Do not propose shifting" in fixed
    assert "FLEXIBLE" in flexible


async def test_large_group_is_flagged_as_a_booking_risk():
    assert "booking risk" in TravellerProfile(group_size=6).as_prompt_block()
    assert "booking risk" not in TravellerProfile(group_size=2).as_prompt_block()


async def test_accommodation_choice_explains_the_gear_consequence():
    block = TravellerProfile(accommodation="huts").as_prompt_block()
    assert "no tent" in block.lower()


async def test_intake_questions_match_profile_fields():
    """A question with no matching field would be silently discarded."""
    fields = set(TravellerProfile.model_fields)
    for attr, _question, choices, default, why in INTAKE_QUESTIONS:
        assert attr in fields, f"{attr} is not a TravellerProfile field"
        assert why, f"{attr} has no explanation for the user"
        if choices:
            assert default in choices, f"{attr} default {default!r} is not among its choices"

    # Every default must actually construct, or the intake crashes on first run.
    defaults = {
        attr: (int(default) if attr == "group_size" else default)
        for attr, _q, _c, default, _w in INTAKE_QUESTIONS
    }
    assert TravellerProfile(**defaults).is_default()
