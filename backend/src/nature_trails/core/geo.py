"""Coordinate maths. Pure functions, no I/O, no dependencies.

Kept deliberately dependency-free (no shapely/geopy) — every function here is a
dozen lines of trigonometry, and they are the easiest things in the project to
unit-test. Anything the model would otherwise have to estimate in its head lives
here instead, computed exactly.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

EARTH_RADIUS_KM = 6371.0088

Coord = tuple[float, float]  # (lat, lon)
BBox = tuple[float, float, float, float]  # (south, west, north, east) — Overpass order


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bbox_around(lat: float, lon: float, radius_km: float) -> BBox:
    """Axis-aligned bounding box of `radius_km` around a point.

    Longitude degrees shrink towards the poles, hence the cos(lat) term.
    Without it, a 25 km box in Svalbard would come out roughly 3x too wide.
    """
    dlat = radius_km / 111.32
    # Guard against division blowing up at the poles.
    cos_lat = max(math.cos(math.radians(lat)), 1e-6)
    dlon = radius_km / (111.32 * cos_lat)
    return (
        max(lat - dlat, -90.0),
        max(lon - dlon, -180.0),
        min(lat + dlat, 90.0),
        min(lon + dlon, 180.0),
    )


def bbox_center(bbox: BBox) -> Coord:
    south, west, north, east = bbox
    return ((south + north) / 2.0, (west + east) / 2.0)


def path_length_km(coords: list[Coord]) -> float:
    """Total along-path distance for an ordered list of (lat, lon) points."""
    if len(coords) < 2:
        return 0.0
    return sum(
        haversine_km(a[0], a[1], b[0], b[1]) for a, b in zip(coords, coords[1:], strict=False)
    )


def decimate(coords: list[Coord], max_points: int) -> list[Coord]:
    """Evenly thin a coordinate list, always keeping the first and last point.

    Elevation APIs cap how many points a single call may request. Naive truncation
    would silently drop the second half of a route; even sampling preserves shape.
    """
    if max_points < 2 or len(coords) <= max_points:
        return coords
    step = (len(coords) - 1) / (max_points - 1)
    out = [coords[round(i * step)] for i in range(max_points)]
    out[-1] = coords[-1]
    return out


def elevation_stats(elevations: list[float], noise_threshold_m: float = 8.0) -> dict[str, float]:
    """Cumulative ascent/descent from an elevation profile.

    The `noise_threshold_m` argument is the important part. Public elevation models
    (SRTM and friends) carry a few metres of per-sample noise. Summing every
    positive delta turns that noise into hundreds of metres of phantom climbing —
    a flat lakeside path can come out as "600 m ascent".

    So we only bank a climb once it has accumulated past the noise floor, then
    reset. This is the same technique GPS watches use, and it brings numbers into
    line with published guidebook figures.
    """
    clean = [e for e in elevations if e is not None]
    if len(clean) < 2:
        return {"ascent_m": 0.0, "descent_m": 0.0, "min_m": 0.0, "max_m": 0.0}

    ascent = descent = 0.0
    anchor = clean[0]
    for current in clean[1:]:
        delta = current - anchor
        if delta >= noise_threshold_m:
            ascent += delta
            anchor = current
        elif delta <= -noise_threshold_m:
            descent += -delta
            anchor = current

    return {
        "ascent_m": round(ascent),
        "descent_m": round(descent),
        "min_m": round(min(clean)),
        "max_m": round(max(clean)),
    }


def naismith_hours(distance_km: float, ascent_m: float, fitness: str = "average") -> float:
    """Estimated walking time via Naismith's rule with a Tranter-style adjustment.

    Naismith (1892): 1 hour per 5 km, plus 1 hour per 600 m of ascent. It is the
    standard baseline in British and Alpine hillwalking, and it deliberately
    ignores descent, rest and terrain — so we scale it by fitness and add a
    fatigue penalty for long days, which is where the raw rule is most optimistic.
    """
    base = distance_km / 5.0 + ascent_m / 600.0
    factor = {"fast": 0.8, "average": 1.0, "relaxed": 1.3}.get(fitness, 1.0)
    hours = base * factor
    # Naismith under-predicts badly past ~6 hours; add 10% per hour beyond that.
    if hours > 6:
        hours += (hours - 6) * 0.10
    return round(hours, 1)


def sun_times(lat: float, lon: float, day: date) -> dict[str, str | float]:
    """Sunrise, sunset and daylight hours (UTC) via the NOAA solar algorithm.

    Daylight is a hard constraint in trip planning that people routinely forget:
    an 8-hour stage is fine in Chamonix in July (~15.5 h of light) and dangerous in
    Patagonia in June (~8.5 h). Returned times are UTC — the renderer localises.
    """
    # Fractional year (radians)
    n = day.timetuple().tm_yday
    gamma = 2 * math.pi / 365.0 * (n - 1 + 0.5)

    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )

    lat_rad = math.radians(lat)
    # 90.833 deg accounts for atmospheric refraction and the sun's angular radius.
    cos_ha = math.cos(math.radians(90.833)) / (math.cos(lat_rad) * math.cos(decl)) - math.tan(
        lat_rad
    ) * math.tan(decl)

    if cos_ha > 1:
        return {
            "sunrise_utc": None,
            "sunset_utc": None,
            "daylight_hours": 0.0,
            "note": "polar night",
        }
    if cos_ha < -1:
        return {
            "sunrise_utc": None,
            "sunset_utc": None,
            "daylight_hours": 24.0,
            "note": "midnight sun",
        }

    ha = math.degrees(math.acos(cos_ha))
    sunrise_min = 720 - 4 * (lon + ha) - eqtime
    sunset_min = 720 - 4 * (lon - ha) - eqtime

    def _fmt(minutes: float) -> str:
        base = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return (base + timedelta(minutes=minutes)).strftime("%H:%M")

    return {
        "sunrise_utc": _fmt(sunrise_min),
        "sunset_utc": _fmt(sunset_min),
        "daylight_hours": round((sunset_min - sunrise_min) / 60.0, 1),
    }
