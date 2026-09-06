"""Deterministic packing-list engine.

The principle this file exists to demonstrate
---------------------------------------------
**Push determinism into tools; leave judgment to the model.**

Asking Claude "what should I pack for the Dolomites in September?" returns a decent
answer. But it is a *different* decent answer every time, it occasionally forgets
the sleeping bag liner that alpine huts require, and it cannot be unit-tested.

Instead, gear selection is a pure function of measurable trip parameters:
elevation, overnight low, precipitation, water availability, hut vs tent, technical
terrain. Those are things the *other* tools can measure. So this module encodes the
rules, returns a weighted and justified list, and the model's job becomes composing
it with context it uniquely understands ("you mentioned you run hot, skip the
midlayer").

Every item carries a `why`. An itinerary that says "bring microspikes" is ignorable;
one that says "bring microspikes: the freezing level averages 2,700 m in late
September and Forcella Ambrizzola is at 2,900 m" is actionable.

Weights are realistic mid-range values in grams, so the engine can report a total
pack weight — the number that actually determines whether a trip is enjoyable.
"""

from __future__ import annotations

from .registry import tool

# Categories are ordered the way people actually pack.
Item = dict


def _item(
    name: str,
    category: str,
    weight_g: int,
    why: str,
    qty: int = 1,
    essential: bool = True,
) -> Item:
    return {
        "item": name,
        "category": category,
        "qty": qty,
        "weight_g": weight_g * qty,
        "essential": essential,
        "why": why,
    }


def _sleeping_bag_rating(overnight_low_c: float, in_huts: bool) -> tuple[str, int, str]:
    """Pick a bag from the expected low, with margin.

    Manufacturers quote a 'comfort' and a 'limit' rating; the limit is where an
    average person survives, not sleeps. Real-world guidance is to choose a bag
    whose comfort rating is ~5 C below the expected low, because tents run only a
    couple of degrees warmer than outside and people underestimate wind chill.
    """
    if in_huts:
        return (
            "sleeping bag liner (silk or synthetic)",
            200,
            "Huts supply blankets or duvets; a liner is mandatory for hygiene at almost every staffed alpine hut and is not optional.",
        )
    target = overnight_low_c - 5
    if target >= 5:
        return (
            "summer sleeping bag, comfort +10 C",
            700,
            f"Overnight lows near {overnight_low_c:.0f} C.",
        )
    if target >= -2:
        return (
            "3-season sleeping bag, comfort 0 C",
            950,
            f"Overnight lows near {overnight_low_c:.0f} C; margin for a cold snap.",
        )
    if target >= -10:
        return (
            "cold-weather bag, comfort -7 C",
            1300,
            f"Overnight lows near {overnight_low_c:.0f} C at altitude.",
        )
    return (
        "winter bag, comfort -15 C or lower",
        1800,
        f"Overnight lows near {overnight_low_c:.0f} C — genuinely cold camping.",
    )


@tool(
    name="build_packing_list",
    description="""
    Generate a complete, weighted packing list from measured trip parameters.

    This is a deterministic rules engine, not a guess — the same inputs always give
    the same list, and every item comes with the reason it is there. Call it once
    you have established the trip's real numbers from the other tools:
    `max_elevation_m` from `get_elevation`, `overnight_low_c` from
    `get_climate_normals` or `get_weather_forecast` (with elevation correction),
    `water_availability` from `find_trail_amenities` with kind `drinking_water`,
    and `technical_terrain` from route `sac_scale` or the presence of via ferrata
    or glaciers.

    Do not invent the inputs. If you have not measured a value, go and measure it —
    a packing list built on a guessed overnight low is how people end up cold at
    2,800 m.

    Afterwards, layer your own judgment on top of the returned list: adjust for
    anything the user told you about themselves (they sleep cold, they already own
    a shell, they are sharing gear with a partner), and call out the two or three
    items that genuinely matter most for this specific trip.
    """,
    properties={
        "days": {"type": "integer", "description": "Number of days on the trail."},
        "accommodation": {
            "type": "string",
            "description": (
                "Where the user sleeps: 'huts' (staffed alpine huts/refuges), "
                "'camping' (tent), 'wilderness_huts' (unstaffed shelters, no bedding "
                "or food), or 'basecamp' (day hikes from a hotel or campsite)."
            ),
        },
        "max_elevation_m": {
            "type": "number",
            "description": "Highest point reached on the trip, in metres.",
        },
        "overnight_low_c": {
            "type": "number",
            "description": (
                "Expected overnight low in Celsius AT SLEEPING ALTITUDE, not in the "
                "valley. Get this from get_climate_normals with `elevation` set."
            ),
        },
        "precipitation": {
            "type": "string",
            "description": "Expected rain: 'low', 'moderate' or 'high'.",
        },
        "snow_possible": {
            "type": "boolean",
            "description": (
                "True if the freezing level is expected near or below the route's high "
                "point, or if there is meaningful mean snowfall for the month."
            ),
        },
        "water_availability": {
            "type": "string",
            "description": (
                "'plentiful' (streams/springs throughout), 'limited' (sources at huts "
                "only), or 'scarce' (dry limestone karst, long waterless sections)."
            ),
        },
        "technical_terrain": {
            "type": "array",
            "description": (
                "Any of: 'via_ferrata', 'glacier', 'scrambling', 'exposed_ridge'. "
                "Omit or leave empty for straightforward hiking."
            ),
            "items": {"type": "string"},
        },
    },
    required=["days", "accommodation", "max_elevation_m", "overnight_low_c"],
)
async def build_packing_list(
    days: int,
    accommodation: str,
    max_elevation_m: float,
    overnight_low_c: float,
    precipitation: str = "moderate",
    snow_possible: bool = False,
    water_availability: str = "limited",
    technical_terrain: list[str] | None = None,
) -> dict:
    days = max(1, int(days))
    accommodation = (accommodation or "huts").lower()
    precipitation = (precipitation or "moderate").lower()
    water_availability = (water_availability or "limited").lower()
    technical = {t.lower() for t in (technical_terrain or [])}
    in_huts = accommodation in {"huts", "wilderness_huts"}

    items: list[Item] = []
    warnings: list[str] = []

    # --- Pack ------------------------------------------------------------
    if accommodation == "basecamp":
        items.append(_item("daypack, 25-35 L", "pack", 900, "Day hikes from a fixed base."))
    elif in_huts:
        items.append(
            _item(
                "backpack, 35-45 L",
                "pack",
                1400,
                "Hut trips need no tent or cookset, so a smaller pack is enough.",
            )
        )
    else:
        items.append(
            _item(
                "backpack, 55-65 L",
                "pack",
                1900,
                "Carrying shelter, sleep system and cooking gear.",
            )
        )
    items.append(
        _item("pack rain cover or liner bag", "pack", 100, "A wet sleeping bag is a trip-ender.")
    )

    # --- Footwear & clothing --------------------------------------------
    boot = (
        "B1 stiff-soled boots"
        if snow_possible or "glacier" in technical
        else "broken-in hiking boots or trail shoes"
    )
    items.append(
        _item(
            boot,
            "footwear",
            1200,
            "Ankle support and grip on loose alpine ground."
            if "boots" in boot
            else "Primary footwear.",
        )
    )
    items.append(
        _item(
            "hiking socks",
            "footwear",
            60,
            "One pair per two days, plus a dry sleeping pair.",
            qty=min(days // 2 + 1, 4),
        )
    )
    if in_huts:
        items.append(
            _item(
                "lightweight indoor shoes",
                "footwear",
                250,
                "Boots are banned inside huts; many supply clogs but sizes run out.",
                essential=False,
            )
        )

    items.append(
        _item(
            "base layer top (merino or synthetic)",
            "clothing",
            180,
            "Wicks and stays warm when damp. Never cotton.",
            qty=2,
        )
    )
    items.append(_item("hiking trousers or shorts", "clothing", 300, "Quick-drying."))
    items.append(
        _item(
            "insulating midlayer (fleece or light down)",
            "clothing",
            350,
            "Core warmth for breaks and evenings.",
        )
    )
    items.append(
        _item(
            "waterproof shell jacket",
            "clothing",
            350,
            f"Rain risk assessed as {precipitation}; also your primary windproof.",
        )
    )
    items.append(_item("underwear", "clothing", 50, "Quick-drying.", qty=min(days, 4)))

    if precipitation == "high":
        items.append(
            _item(
                "waterproof overtrousers",
                "clothing",
                250,
                "Sustained rain expected; wet legs plus wind is how hypothermia starts.",
            )
        )
    if max_elevation_m >= 2000 or overnight_low_c <= 8:
        items.append(
            _item(
                "warm hat",
                "clothing",
                60,
                f"High point {max_elevation_m:.0f} m; temperature drops roughly 6.5 C per 1000 m.",
            )
        )
        items.append(
            _item(
                "lightweight gloves",
                "clothing",
                70,
                "Hands lose dexterity fast in wind at altitude.",
            )
        )
    if overnight_low_c <= 0 or snow_possible:
        items.append(
            _item(
                "insulated jacket (down or synthetic)",
                "clothing",
                450,
                f"Overnight lows around {overnight_low_c:.0f} C.",
            )
        )

    # --- Sleep system ----------------------------------------------------
    bag_name, bag_weight, bag_why = _sleeping_bag_rating(overnight_low_c, in_huts)
    items.append(_item(bag_name, "sleep", bag_weight, bag_why))
    if accommodation == "camping":
        items.append(_item("tent (share weight if in a pair)", "sleep", 1800, "Shelter."))
        items.append(
            _item(
                "sleeping mat, R-value 3+",
                "sleep",
                500,
                "Ground steals more heat than air does; R-value matters more than bag rating.",
            )
        )
    elif accommodation == "wilderness_huts":
        items.append(
            _item(
                "sleeping mat (thin)",
                "sleep",
                350,
                "Unstaffed huts often have bare wooden platforms.",
                essential=False,
            )
        )
    if in_huts:
        items.append(
            _item(
                "earplugs",
                "sleep",
                10,
                "Hut dormitories sleep 20+ people in one room. This is not a joke item.",
            )
        )

    # --- Water & food ----------------------------------------------------
    capacity = {"plentiful": 1.5, "limited": 2.0, "scarce": 3.0}.get(water_availability, 2.0)
    items.append(
        _item(
            f"water capacity, {capacity:.1f} L total",
            "water",
            int(capacity * 120),
            f"Water availability assessed as {water_availability}.",
        )
    )
    if water_availability in {"plentiful", "limited"}:
        items.append(
            _item(
                "water filter or purification tablets",
                "water",
                90,
                "Lets you drink from streams and springs instead of carrying more weight.",
                essential=False,
            )
        )
    if water_availability == "scarce":
        warnings.append(
            "Water is scarce on this route. Confirm each day's water source before setting off; carrying 3 L is heavy but not optional on dry karst terrain."
        )

    if accommodation == "camping":
        items.append(_item("stove + fuel", "food", 400, "No hut meals available."))
        items.append(_item("pot, spork, lighter", "food", 250, "Cooking kit."))
        items.append(
            _item("dinner + breakfast rations", "food", 700, "Per day on the trail.", qty=days)
        )
    elif accommodation == "wilderness_huts":
        items.append(
            _item(
                "stove + fuel",
                "food",
                400,
                "Unstaffed huts provide shelter only — no food, no kitchen.",
            )
        )
        items.append(_item("dinner + breakfast rations", "food", 700, "Per day.", qty=days))
    items.append(
        _item(
            "trail snacks / lunch",
            "food",
            400,
            "Per day; huts sell lunch but not always on the route.",
            qty=days,
        )
    )

    # --- Navigation, safety, essentials ---------------------------------
    items.append(
        _item(
            "paper map + compass",
            "safety",
            150,
            "Phones die, get wet, and lose signal. This is the backup that always works.",
        )
    )
    items.append(
        _item(
            "offline GPS maps on phone",
            "safety",
            0,
            "Download the region before you leave — there is no data coverage on most of the route.",
        )
    )
    items.append(
        _item(
            "headlamp + spare batteries",
            "safety",
            120,
            "Essential even for day trips: a twisted ankle turns a 6-hour day into a night walk.",
        )
    )
    items.append(
        _item(
            "first aid kit incl. blister care",
            "safety",
            250,
            "Blisters are the single most common trip-ender.",
        )
    )
    items.append(
        _item("power bank, 10000 mAh", "safety", 200, "Huts often have no or paid charging.")
    )
    items.append(
        _item(
            "emergency bivvy / survival bag",
            "safety",
            100,
            "Weighs nothing, buys hours if you are stuck out.",
        )
    )
    items.append(
        _item("whistle", "safety", 10, "Alpine distress signal: six blasts a minute, repeat.")
    )

    if max_elevation_m >= 2000:
        items.append(
            _item(
                "sunglasses, category 3-4",
                "safety",
                30,
                f"UV rises ~10% per 1000 m; at {max_elevation_m:.0f} m it is intense, and worse over snow.",
            )
        )
        items.append(
            _item(
                "sunscreen SPF 50 + lip balm",
                "safety",
                120,
                "Altitude and reflected light burn faster than people expect.",
            )
        )
    if max_elevation_m >= 3000:
        warnings.append(
            f"Route reaches {max_elevation_m:.0f} m. Plan for altitude: ascend gradually, hydrate, and know the symptoms of AMS (headache, nausea, poor sleep)."
        )

    # --- Terrain-specific -------------------------------------------------
    if snow_possible:
        items.append(
            _item(
                "microspikes",
                "traction",
                400,
                "Old snow patches on north-facing traverses stay hard and icy into summer.",
            )
        )
        items.append(
            _item("gaiters", "traction", 200, "Keeps snow and scree out of boots.", essential=False)
        )
    if "via_ferrata" in technical:
        items.append(
            _item(
                "via ferrata set (energy-absorbing lanyard)",
                "technical",
                550,
                "Mandatory on protected routes; a plain sling will not survive a fall.",
            )
        )
        items.append(
            _item("climbing harness", "technical", 350, "Required with the via ferrata set.")
        )
        items.append(
            _item(
                "helmet",
                "technical",
                350,
                "Rockfall from parties above is the main hazard on ferratas.",
            )
        )
        items.append(
            _item(
                "via ferrata gloves",
                "technical",
                80,
                "Steel cable shreds bare hands.",
                essential=False,
            )
        )
        warnings.append(
            "This route includes via ferrata. That is protected climbing, not hiking — it needs the full kit above and a head for exposure. If the user has no experience, recommend a guide or an alternative route."
        )
    if "glacier" in technical:
        items.append(_item("crampons (matched to boots)", "technical", 900, "Glacier travel."))
        items.append(_item("ice axe", "technical", 500, "Self-arrest."))
        items.append(
            _item(
                "rope, harness, prusiks, screws",
                "technical",
                2500,
                "Crevasse rescue kit — useless without training.",
            )
        )
        warnings.append(
            "This route crosses glaciated terrain. Do not present this as a hiking option: it requires roped travel, crevasse-rescue training and a partner, or a certified mountain guide."
        )
    if "scrambling" in technical or "exposed_ridge" in technical:
        items.append(
            _item("helmet", "technical", 350, "Exposed or loose ground with rockfall risk.")
        )
        items.append(
            _item(
                "trekking poles",
                "technical",
                500,
                "Saves knees on descent and aids balance on scree.",
                essential=False,
            )
        )
    else:
        items.append(
            _item(
                "trekking poles",
                "gear",
                500,
                "Reduce knee load on descent by roughly 25%.",
                essential=False,
            )
        )

    # --- Hut logistics (the stuff nobody writes down) --------------------
    if accommodation == "huts":
        items.append(
            _item(
                "cash in local currency",
                "logistics",
                50,
                "Many alpine huts still do not take cards, and there are no ATMs above the valley.",
            )
        )
        items.append(
            _item(
                "quick-dry towel",
                "logistics",
                120,
                "Huts charge for showers and rarely supply towels.",
                essential=False,
            )
        )
        items.append(
            _item(
                "hut reservation confirmations (printed or offline)",
                "logistics",
                20,
                "No signal to pull up an email at 2,500 m.",
            )
        )
        warnings.append(
            "Staffed huts must be booked in advance for summer — popular routes fill months ahead. Confirm opening dates for the travel month; most Alpine huts run late June to late September only."
        )
    items.append(
        _item("ID / passport", "logistics", 30, "Required at huts, and for alpine club discounts.")
    )
    items.append(
        _item(
            "travel + mountain rescue insurance",
            "logistics",
            0,
            "Helicopter rescue in the Alps is billed to you and runs into thousands of euros. Alpine club membership (CAI/DAV/OeAV) usually includes cover and hut discounts.",
        )
    )

    # --- Totals -----------------------------------------------------------
    total_g = sum(i["weight_g"] for i in items)
    essential_g = sum(i["weight_g"] for i in items if i["essential"])
    consumable_g = sum(i["weight_g"] for i in items if i["category"] in {"food", "water"})

    by_category: dict[str, list[Item]] = {}
    for entry in items:
        by_category.setdefault(entry["category"], []).append(entry)

    return {
        "trip": {
            "days": days,
            "accommodation": accommodation,
            "max_elevation_m": max_elevation_m,
            "overnight_low_c": overnight_low_c,
            "precipitation": precipitation,
            "snow_possible": snow_possible,
            "water_availability": water_availability,
            "technical_terrain": sorted(technical),
        },
        "items_by_category": by_category,
        "item_count": len(items),
        "weight": {
            "total_kg": round(total_g / 1000, 1),
            "essential_only_kg": round(essential_g / 1000, 1),
            "base_weight_kg": round((total_g - consumable_g) / 1000, 1),
            "note": (
                "Base weight excludes food and water. Under 10 kg base is light, "
                "10-14 kg is normal for a hut trip, over 16 kg will hurt on day three."
            ),
        },
        "warnings": warnings,
    }
