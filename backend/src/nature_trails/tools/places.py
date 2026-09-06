"""Place resolution, elevation and daylight.

These are the foundational tools: almost every other tool needs coordinates, and
the agent cannot invent them. `geocode_place` is deliberately described as the
"call this first" tool so the model does not try to guess a bounding box for
"the Dolomites" from memory.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from ..core.geo import bbox_around, decimate, elevation_stats, path_length_km, sun_times
from ..core.http import get_client
from .registry import tool

NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
OPEN_METEO_ELEVATION = "https://api.open-meteo.com/v1/elevation"

# Place data essentially never changes; cache it for a month.
GEO_TTL = 30 * 24 * 3600.0

# Open-Meteo accepts at most 100 coordinates per elevation request, so long
# profiles are chunked. The cap is 300 rather than higher for two reasons:
# Open-Meteo's free tier returns 429 under bursts, and — more interestingly —
# ascent does not converge as sampling gets finer. Measured on the Alta Via 1:
#
#     60 pts (1.43 km spacing) -> 5,860 m
#    120 pts (0.76 km spacing) -> 6,638 m
#    250 pts (0.38 km spacing) -> 7,275 m
#
# Published figures for the route are 6,000-7,000 m. Finer sampling keeps finding
# more micro-undulation (a coastline-paradox effect that the noise filter damps
# but cannot remove), so "more points" stops meaning "more accurate" and starts
# meaning "inflated". Roughly 0.5-1.0 km spacing matches how guidebooks measure.
ELEVATION_CHUNK = 100
MAX_ELEVATION_POINTS = 300
IDEAL_SPACING_KM = (0.5, 1.0)


@tool(
    name="geocode_place",
    description="""
    Resolve a place name (town, valley, national park, mountain range, trailhead)
    into coordinates and a bounding box.

    CALL THIS FIRST for any destination. Every trail, hut, weather and amenity tool
    needs coordinates, and guessing them from memory produces plausible-looking but
    wrong results — a 0.3 degree error puts you in a different valley.

    Returns several candidates ranked by relevance, each with its OSM type so you
    can tell a `national_park` boundary from a same-named village. For a large area
    (a park or range) prefer the candidate whose bounding box spans the whole area;
    for a trailhead prefer the precise point.
    """,
    properties={
        "query": {
            "type": "string",
            "description": (
                "Place name to look up. Be specific and include the country or region "
                "when a name is ambiguous, e.g. 'Tre Cime di Lavaredo, Italy' rather "
                "than 'Tre Cime'."
            ),
        },
        "limit": {
            "type": "integer",
            "description": "How many candidates to return (1-10). Default 5.",
        },
    },
    required=["query"],
)
async def geocode_place(query: str, limit: int = 5) -> dict:
    client = get_client()
    raw = await client.get_json(
        NOMINATIM_SEARCH,
        params={
            "q": query,
            "format": "jsonv2",
            "limit": max(1, min(int(limit), 10)),
            "addressdetails": 1,
            "extratags": 1,
        },
        ttl_seconds=GEO_TTL,
    )

    results = []
    for item in raw:
        # Nominatim returns boundingbox as [min_lat, max_lat, min_lon, max_lon]
        # (strings). Overpass wants (south, west, north, east). Getting this
        # transposition wrong is a classic silent bug — the query hits the wrong box
        # and simply returns no results.
        try:
            min_lat, max_lat, min_lon, max_lon = (float(v) for v in item["boundingbox"])
            bbox = [min_lat, min_lon, max_lat, max_lon]
        except (KeyError, ValueError, TypeError):
            bbox = None

        results.append(
            {
                "name": item.get("display_name"),
                "lat": float(item["lat"]),
                "lon": float(item["lon"]),
                "category": item.get("category") or item.get("class"),
                "type": item.get("type"),
                "importance": round(float(item.get("importance", 0)), 3),
                "bbox_south_west_north_east": bbox,
                "country": (item.get("address") or {}).get("country"),
            }
        )

    if not results:
        return {
            "results": [],
            "hint": (
                f"No match for {query!r}. Try adding a country, using the local-language "
                "spelling, or naming a nearby town instead of a feature."
            ),
        }
    return {"query": query, "results": results}


@tool(
    name="get_elevation",
    description="""
    Look up ground elevation (metres above sea level) for up to 300 coordinates,
    and compute ascent/descent statistics if the points form a route in order.

    Use this to sanity-check how hard a stage really is. Distance alone is
    misleading in mountains: 12 km with 1400 m of climbing is a full day, 12 km flat
    is a morning. Ascent uses a noise filter so the numbers line up with guidebook
    figures rather than inflating from elevation-model jitter.

    Ascent is scale-dependent, so read `spacing_km` and `accuracy_note` in the
    result before quoting a number. Samples much coarser than 1 km miss whole
    climbs and under-report; samples much finer than 0.5 km pick up terrain-model
    noise and over-report. Aim for one point every 0.5-1.0 km of the section you
    are measuring, and measure a single stage at a time rather than slicing a
    whole-route sample.
    """,
    properties={
        "coordinates": {
            "type": "array",
            "description": (
                "Ordered list of [latitude, longitude] pairs. If you pass a route's "
                "points in walking order, the ascent/descent totals are meaningful. "
                "Up to 300 points are supported; beyond that they are evenly thinned, "
                "preserving shape."
            ),
            "items": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 2,
                "maxItems": 2,
            },
        }
    },
    required=["coordinates"],
)
async def get_elevation(coordinates: list[list[float]]) -> dict:
    coords = [(float(c[0]), float(c[1])) for c in coordinates if len(c) >= 2]
    if not coords:
        return {"error": "No valid coordinates supplied."}

    sampled = decimate(coords, MAX_ELEVATION_POINTS)
    client = get_client()

    # Open-Meteo caps a single elevation request at 100 coordinates. Sample
    # spacing drives accuracy: 100 points across a 120 km route is 1.2 km apart,
    # which smooths away nearly all the climbing and reports a mountain traverse
    # as gently rolling. So we chunk and issue the requests concurrently.
    chunks = [sampled[i : i + ELEVATION_CHUNK] for i in range(0, len(sampled), ELEVATION_CHUNK)]

    async def fetch(chunk: list[tuple[float, float]]) -> list[float]:
        raw = await client.get_json(
            OPEN_METEO_ELEVATION,
            params={
                "latitude": ",".join(f"{lat:.5f}" for lat, _ in chunk),
                "longitude": ",".join(f"{lon:.5f}" for _, lon in chunk),
            },
            ttl_seconds=GEO_TTL,
        )
        return [float(e) for e in raw.get("elevation", [])]

    results = await asyncio.gather(*(fetch(chunk) for chunk in chunks))
    elevations = [value for chunk_result in results for value in chunk_result]

    payload: dict = {
        "sampled_points": len(sampled),
        "spacing_km": round(path_length_km(sampled) / max(len(sampled) - 1, 1), 2),
        "elevations_m": [round(e) for e in elevations],
    }
    if len(elevations) >= 2:
        payload.update(elevation_stats(elevations))
        spacing = payload["spacing_km"]
        low, high = IDEAL_SPACING_KM
        if spacing > high:
            payload["accuracy_note"] = (
                f"Samples are {spacing} km apart, which is coarse: climbs shorter "
                "than that are invisible, so this ascent is an UNDER-estimate. "
                "Request more points for a figure you can defend."
            )
        elif spacing < low:
            payload["accuracy_note"] = (
                f"Samples are {spacing} km apart, which is fine enough that terrain-"
                "model noise inflates the total — treat this ascent as an OVER-"
                "estimate. Around 0.5-1.0 km spacing matches guidebook figures."
            )
        else:
            payload["accuracy_note"] = (
                f"Sample spacing of {spacing} km is in the range that matches "
                "published guidebook ascent figures."
            )
    return payload


@tool(
    name="get_daylight",
    description="""
    Sunrise, sunset and total daylight hours (UTC) for a location across a date
    range.

    This is a hard planning constraint people forget. An 8-hour stage is relaxed in
    the Alps in July (about 15.5 h of light) and genuinely dangerous in Patagonia in
    June (about 8.5 h). Check this before scheduling any long day, and especially
    before planning a summit push or a stage that ends at a hut with a fixed dinner
    time.
    """,
    properties={
        "latitude": {"type": "number", "description": "Latitude in decimal degrees."},
        "longitude": {"type": "number", "description": "Longitude in decimal degrees."},
        "start_date": {"type": "string", "description": "First date, ISO format YYYY-MM-DD."},
        "days": {
            "type": "integer",
            "description": "How many consecutive days to report (1-30). Default 1.",
        },
    },
    required=["latitude", "longitude", "start_date"],
)
async def get_daylight(latitude: float, longitude: float, start_date: str, days: int = 1) -> dict:
    try:
        start = date.fromisoformat(start_date)
    except ValueError:
        return {"error": f"start_date {start_date!r} is not a valid YYYY-MM-DD date."}

    span = max(1, min(int(days), 30))
    return {
        "latitude": latitude,
        "longitude": longitude,
        "note": "Times are UTC. Convert to local time for the destination's timezone.",
        "days": [
            {
                "date": (start + timedelta(days=i)).isoformat(),
                **sun_times(latitude, longitude, start + timedelta(days=i)),
            }
            for i in range(span)
        ],
    }


@tool(
    name="make_bounding_box",
    description="""
    Build a bounding box of a given radius around a point.

    Trail and hut searches take a bounding box. Use this when you have a centre
    point from `geocode_place` but want to control the search area — for example a
    tight 8 km box around a trailhead, or a wide 40 km box to find every hut in a
    massif. Correctly accounts for longitude compression at high latitude.
    """,
    properties={
        "latitude": {"type": "number", "description": "Centre latitude."},
        "longitude": {"type": "number", "description": "Centre longitude."},
        "radius_km": {
            "type": "number",
            "description": (
                "Half-width of the box in km. Keep at or below 30 for dense alpine "
                "regions or Overpass queries will time out; 50-80 is fine for empty "
                "wilderness."
            ),
        },
    },
    required=["latitude", "longitude", "radius_km"],
)
async def make_bounding_box(latitude: float, longitude: float, radius_km: float) -> dict:
    south, west, north, east = bbox_around(latitude, longitude, float(radius_km))
    return {
        "bbox_south_west_north_east": [
            round(south, 5),
            round(west, 5),
            round(north, 5),
            round(east, 5),
        ],
        "radius_km": radius_km,
    }
