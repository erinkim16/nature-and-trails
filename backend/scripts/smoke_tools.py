"""Live smoke test for the data layer — no Anthropic API key required.

Run after touching a tool, or when the agent behaves oddly and the question is
whether the problem is the model or the data underneath it.

    python scripts/smoke_tools.py

It hits the real OSM and Open-Meteo services, so the first run takes ~30s. The
second is near-instant because everything lands in the SQLite cache.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Windows consoles default to cp1252, which mangles every accented place name in
# the Alps. Force UTF-8 so "Bivacco Tomè" prints correctly.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nature_trails.core.http import close_client  # noqa: E402
from nature_trails.tools import REGISTRY, tool_definitions  # noqa: E402
from nature_trails.tools.packing import build_packing_list  # noqa: E402
from nature_trails.tools.places import geocode_place, get_elevation  # noqa: E402
from nature_trails.tools.trails import (  # noqa: E402
    find_hiking_routes,
    find_mountain_huts,
    find_trail_amenities,
)
from nature_trails.tools.weather import get_climate_normals  # noqa: E402

PASS, FAIL = "  [ok]", "  [FAIL]"


def show(label: str, value) -> None:
    print(f"{label}: {json.dumps(value, ensure_ascii=False, default=str)[:400]}")


async def main() -> int:
    try:
        return await _run_checks()
    finally:
        # Must close inside the SAME event loop that created the client. Closing
        # from a second asyncio.run() raises "Event loop is closed", because
        # httpx's connections are bound to the loop they were opened on.
        await close_client()


async def _run_checks() -> int:
    failures = 0
    print(f"\nRegistered tools ({len(REGISTRY)}): {', '.join(sorted(REGISTRY))}\n")

    # Every definition must be well-formed or the API rejects the whole request.
    for definition in tool_definitions():
        assert definition["input_schema"]["type"] == "object"
        for name in definition["input_schema"]["required"]:
            assert name in definition["input_schema"]["properties"], (
                f"{definition['name']}: required param {name!r} missing from properties"
            )
    print(f"{PASS} all tool schemas well-formed\n")

    # --- 1. Geocoding ----------------------------------------------------
    print("1. geocode_place('Cortina d'Ampezzo, Italy')")
    geo = await geocode_place("Cortina d'Ampezzo, Italy", limit=3)
    if not geo.get("results"):
        print(FAIL, "no geocoding results")
        failures += 1
        return failures
    top = geo["results"][0]
    print(f"{PASS} {top['name'][:70]}  ({top['lat']:.4f}, {top['lon']:.4f})")

    lat, lon = top["lat"], top["lon"]
    bbox = [lat - 0.25, lon - 0.35, lat + 0.25, lon + 0.35]

    # --- 2. Hiking routes (the differentiator) ---------------------------
    print("\n2. find_hiking_routes(Dolomites bbox, min 30 km)")
    routes = await find_hiking_routes(bbox, min_length_km=30)
    found = routes.get("routes", [])
    if found:
        print(f"{PASS} {routes['total_found']} long routes found; top 5:")
        for r in found[:5]:
            length = str(r["length_km"] or "?")
            network = r["network"] or "-"
            print(f"       - {r['name'][:58]:58} {length:>6} km  [{network}]")
    else:
        print(FAIL, routes.get("hint", "no routes"))
        failures += 1

    # --- 3. Alpine huts ---------------------------------------------------
    print("\n3. find_mountain_huts(same bbox)")
    huts = await find_mountain_huts(bbox, types=["alpine_hut", "wilderness_hut"])
    hut_list = huts.get("huts", [])
    if hut_list:
        print(f"{PASS} {huts['total_found']} huts found; highest 5:")
        for h in hut_list[:5]:
            ele = f"{h['ele_m']:.0f} m" if h.get("ele_m") else "?"
            print(f"       - {str(h.get('name'))[:48]:48} {ele:>8}  {h.get('operator') or ''}")
    else:
        print(FAIL, huts.get("hint", "no huts"))
        failures += 1

    # --- 4. Amenities that change a plan ---------------------------------
    print("\n4. find_trail_amenities(drinking_water, cable_car)")
    amen = await find_trail_amenities(bbox, kinds=["drinking_water", "cable_car"], limit=5)
    print(f"{PASS} counts = {amen.get('counts')}")

    # --- 5. Elevation ------------------------------------------------------
    print("\n5. get_elevation(3 points across the massif)")
    elev = await get_elevation([[lat, lon], [lat + 0.1, lon + 0.1], [lat + 0.2, lon + 0.05]])
    print(f"{PASS} elevations {elev.get('elevations_m')} m  ascent={elev.get('ascent_m')} m")

    # --- 6. Climate normals (the tool that matters for real planning) ----
    print("\n6. get_climate_normals(September @ 2500 m)")
    clim = await get_climate_normals(lat, lon, month=9, elevation=2500, years=3)
    if clim.get("mean_temp_max_c") is not None:
        print(
            f"{PASS} Sept @2500m: max {clim['mean_temp_max_c']} C / "
            f"min {clim['mean_temp_min_c']} C, "
            f"{clim['mean_wet_days']} wet days, {clim['mean_snowfall_cm']} cm snow"
        )
    else:
        print(FAIL, clim)
        failures += 1

    # --- 7. Packing engine (pure, no network) ----------------------------
    print("\n7. build_packing_list(6-day hut trip, high point 2900 m)")
    pack = await build_packing_list(
        days=6,
        accommodation="huts",
        max_elevation_m=2900,
        overnight_low_c=2,
        precipitation="moderate",
        snow_possible=True,
        water_availability="scarce",
        technical_terrain=["via_ferrata"],
    )
    print(
        f"{PASS} {pack['item_count']} items, {pack['weight']['total_kg']} kg total "
        f"({pack['weight']['base_weight_kg']} kg base), {len(pack['warnings'])} warnings"
    )
    for w in pack["warnings"]:
        print(f"       ! {w[:110]}")

    print("\n" + ("All checks passed." if not failures else f"{failures} check(s) FAILED."))
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
