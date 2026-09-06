"""Tests for the packing rules engine.

This is the payoff of making gear selection deterministic instead of asking the
model: it is testable. These assertions encode real mountain-safety rules, so a
regression here is caught in milliseconds rather than discovered at 2,800 m.
"""

from __future__ import annotations

import pytest

from nature_trails.tools.packing import build_packing_list

pytestmark = pytest.mark.asyncio


def names(result) -> list[str]:
    return [
        item["item"].lower() for items in result["items_by_category"].values() for item in items
    ]


async def test_hut_trip_requires_a_liner_not_a_sleeping_bag():
    """Alpine huts supply bedding; a liner is mandatory, a bag is dead weight."""
    result = await build_packing_list(
        days=5, accommodation="huts", max_elevation_m=2600, overnight_low_c=3
    )
    joined = " ".join(names(result))
    assert "liner" in joined
    assert "sleeping bag, comfort" not in joined


async def test_camping_gets_a_bag_matched_to_the_overnight_low():
    warm = await build_packing_list(
        days=3, accommodation="camping", max_elevation_m=1500, overnight_low_c=12
    )
    cold = await build_packing_list(
        days=3, accommodation="camping", max_elevation_m=3200, overnight_low_c=-8
    )
    assert "summer" in " ".join(names(warm))
    assert "cold-weather" in " ".join(names(cold)) or "winter" in " ".join(names(cold))


async def test_camping_includes_shelter_and_stove_but_huts_do_not():
    camp = await build_packing_list(
        days=3, accommodation="camping", max_elevation_m=2000, overnight_low_c=5
    )
    hut = await build_packing_list(
        days=3, accommodation="huts", max_elevation_m=2000, overnight_low_c=5
    )
    assert any("tent" in n for n in names(camp))
    assert any("stove" in n for n in names(camp))
    assert not any("tent" in n for n in names(hut))
    assert not any("stove" in n for n in names(hut))


async def test_wilderness_huts_need_a_stove_but_no_tent():
    """Unstaffed huts give shelter only — no food, no bedding, no kitchen."""
    result = await build_packing_list(
        days=4, accommodation="wilderness_huts", max_elevation_m=1800, overnight_low_c=2
    )
    listed = names(result)
    assert any("stove" in n for n in listed)
    assert not any("tent" in n for n in listed)


async def test_snow_triggers_traction():
    result = await build_packing_list(
        days=4,
        accommodation="huts",
        max_elevation_m=2900,
        overnight_low_c=-2,
        snow_possible=True,
    )
    assert any("microspikes" in n for n in names(result))


async def test_via_ferrata_requires_the_full_kit_and_a_loud_warning():
    result = await build_packing_list(
        days=3,
        accommodation="huts",
        max_elevation_m=2800,
        overnight_low_c=4,
        technical_terrain=["via_ferrata"],
    )
    listed = " ".join(names(result))
    assert "via ferrata set" in listed
    assert "harness" in listed
    assert "helmet" in listed
    assert any("via ferrata" in w.lower() for w in result["warnings"])


async def test_glacier_warns_that_this_is_not_hiking():
    result = await build_packing_list(
        days=5,
        accommodation="huts",
        max_elevation_m=3600,
        overnight_low_c=-6,
        technical_terrain=["glacier"],
    )
    listed = " ".join(names(result))
    assert "crampons" in listed
    assert "ice axe" in listed
    warning = " ".join(result["warnings"]).lower()
    assert "guide" in warning or "training" in warning


async def test_scarce_water_increases_capacity_and_warns():
    scarce = await build_packing_list(
        days=4,
        accommodation="huts",
        max_elevation_m=2400,
        overnight_low_c=6,
        water_availability="scarce",
    )
    plentiful = await build_packing_list(
        days=4,
        accommodation="huts",
        max_elevation_m=2400,
        overnight_low_c=6,
        water_availability="plentiful",
    )
    # `names()` lowercases, so the litre suffix arrives as "l".
    assert "3.0 l" in " ".join(names(scarce))
    assert "1.5 l" in " ".join(names(plentiful))
    assert any("water" in w.lower() for w in scarce["warnings"])


async def test_high_altitude_adds_sun_protection_and_ams_warning():
    result = await build_packing_list(
        days=6, accommodation="camping", max_elevation_m=4200, overnight_low_c=-10
    )
    listed = " ".join(names(result))
    assert "sunglasses" in listed
    assert "sunscreen" in listed
    assert any("altitude" in w.lower() or "ams" in w.lower() for w in result["warnings"])


async def test_food_scales_with_trip_length():
    short = await build_packing_list(
        days=2, accommodation="camping", max_elevation_m=1500, overnight_low_c=8
    )
    long = await build_packing_list(
        days=10, accommodation="camping", max_elevation_m=1500, overnight_low_c=8
    )
    assert long["weight"]["total_kg"] > short["weight"]["total_kg"]


async def test_base_weight_excludes_consumables():
    result = await build_packing_list(
        days=7, accommodation="camping", max_elevation_m=2000, overnight_low_c=4
    )
    w = result["weight"]
    assert w["base_weight_kg"] < w["total_kg"]


async def test_output_is_deterministic():
    """The reason this is a rules engine and not a prompt."""
    kwargs = dict(
        days=6,
        accommodation="huts",
        max_elevation_m=2900,
        overnight_low_c=2,
        snow_possible=True,
        water_availability="scarce",
        technical_terrain=["via_ferrata"],
    )
    first = await build_packing_list(**kwargs)
    second = await build_packing_list(**kwargs)
    assert first == second


async def test_every_item_carries_a_reason():
    """An itinerary that says 'bring microspikes' is ignorable; one that says why is not."""
    result = await build_packing_list(
        days=5, accommodation="huts", max_elevation_m=2700, overnight_low_c=1
    )
    for items in result["items_by_category"].values():
        for item in items:
            assert item["why"].strip(), f"{item['item']} has no justification"
