"""Weather tools built on Open-Meteo (free, no API key, no attribution burden).

The design point that matters
-----------------------------
Almost every weather integration in a travel app fetches a forecast. But people
plan trips **months** in advance, and no forecast extends past ~16 days. A trip
planner that can only answer "what is the weather" for next week is useless for
its actual job.

So there are two tools:

* `get_weather_forecast` — real forecast, only within the next 16 days.
* `get_climate_normals` — what a given month is *typically* like at this location,
  computed from the last 5 years of reanalysis data. This is the tool the agent
  will use most, and the one that answers "is late June too early for the Alta Via 1?"

Two alpine-specific details this module handles that generic weather code does not:

* **Elevation correction.** Open-Meteo's model grid has its own terrain elevation,
  which can be a kilometre off from a hut on a ridge. Passing `elevation` makes the
  API lapse-rate-correct the temperature. Without it the numbers are valley-floor
  ones, and the advice becomes a fleece for a night at 2900 m.
* **Freezing level.** `freezing_level_height` is the altitude of the 0 degrees C
  isotherm. If it drops below the route's high point, precipitation up there falls
  as snow. This single number decides whether a September traverse needs
  microspikes.
"""

from __future__ import annotations

from datetime import date, timedelta

from ..core.http import get_client
from .registry import tool

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

FORECAST_TTL = 3600.0  # 1 hour — forecasts update ~hourly
ARCHIVE_TTL = 90 * 24 * 3600.0  # historical data never changes

# WMO weather interpretation codes, condensed to what a hiker cares about.
WMO_CODES = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "heavy freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "violent showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}


def _describe(code) -> str | None:
    try:
        return WMO_CODES.get(int(code))
    except (TypeError, ValueError):
        return None


def _mean(values: list) -> float | None:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 1) if clean else None


@tool(
    name="get_weather_forecast",
    description="""
    Actual weather forecast for a location, up to 16 days ahead.

    Only useful for trips starting within about two weeks. For anything further out
    — which is most trip planning — call `get_climate_normals` instead; do not
    present a 16-day forecast as if it covered a trip three months away.

    Always pass `elevation` when the point of interest is a hut, pass or summit. The
    model grid's terrain height can be over 1000 m off in mountains, and without the
    correction you will report valley temperatures for a high camp.

    `freezing_level_m` is the altitude of the 0 degrees C line. Compare it to the
    route's high point: if the freezing level is below it, expect snow and ice up
    there regardless of how pleasant the valley looks.
    """,
    properties={
        "latitude": {"type": "number", "description": "Latitude in decimal degrees."},
        "longitude": {"type": "number", "description": "Longitude in decimal degrees."},
        "elevation": {
            "type": "number",
            "description": (
                "Elevation of the point in metres. Strongly recommended in mountains — "
                "enables lapse-rate correction of temperature. Get it from "
                "`get_elevation` or a hut's `ele_m`."
            ),
        },
        "days": {"type": "integer", "description": "Forecast length, 1-16. Default 7."},
    },
    required=["latitude", "longitude"],
)
async def get_weather_forecast(
    latitude: float, longitude: float, elevation: float | None = None, days: int = 7
) -> dict:
    span = max(1, min(int(days), 16))
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "forecast_days": span,
        "timezone": "auto",
        "daily": ",".join(
            [
                "weather_code",
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_sum",
                "snowfall_sum",
                "precipitation_probability_max",
                "wind_speed_10m_max",
                "wind_gusts_10m_max",
            ]
        ),
        "hourly": "freezing_level_height",
    }
    if elevation is not None:
        params["elevation"] = elevation

    raw = await get_client().get_json(FORECAST_URL, params=params, ttl_seconds=FORECAST_TTL)
    daily = raw.get("daily", {})
    hourly = raw.get("hourly", {})

    # Collapse hourly freezing level into a daily mean (24 samples per day).
    levels = hourly.get("freezing_level_height", [])
    daily_freezing = [
        _mean(levels[i * 24 : (i + 1) * 24]) for i in range(span) if levels[i * 24 : (i + 1) * 24]
    ]

    entries = []
    for i, day in enumerate(daily.get("time", [])):
        entries.append(
            {
                "date": day,
                "conditions": _describe((daily.get("weather_code") or [None])[i]),
                "temp_max_c": (daily.get("temperature_2m_max") or [None])[i],
                "temp_min_c": (daily.get("temperature_2m_min") or [None])[i],
                "precip_mm": (daily.get("precipitation_sum") or [None])[i],
                "snowfall_cm": (daily.get("snowfall_sum") or [None])[i],
                "precip_chance_pct": (daily.get("precipitation_probability_max") or [None])[i],
                "wind_max_kmh": (daily.get("wind_speed_10m_max") or [None])[i],
                "gust_max_kmh": (daily.get("wind_gusts_10m_max") or [None])[i],
                "freezing_level_m": daily_freezing[i] if i < len(daily_freezing) else None,
            }
        )

    return {
        "latitude": latitude,
        "longitude": longitude,
        "model_elevation_m": raw.get("elevation"),
        "elevation_correction_applied": elevation is not None,
        "timezone": raw.get("timezone"),
        "days": entries,
    }


@tool(
    name="get_climate_normals",
    description="""
    What a given month is *typically* like at a location, from the last 5 years of
    historical reanalysis data.

    This is the right tool for almost all trip planning, because trips are planned
    months ahead and forecasts only reach 16 days. It answers the questions that
    actually decide a trip:

    - "Is late June too early for the Alta Via 1?" (check `mean_snowfall_cm` and
      whether the freezing level sits below the route's high point)
    - "How many wet days should I expect in Torres del Paine in November?"
    - "Will it freeze at night at 2600 m in September?" (check `mean_temp_min_c`
      with `elevation` set to the hut height)

    Pass `elevation` set to the *high* point of the planned route, not the valley.
    The difference is routinely 10-15 degrees C and completely changes the gear list.

    These are averages: individual years vary a lot, and mountain weather can
    deliver snow in August. Present them as expectations, not guarantees, and pair
    with the `wettest_year` / `driest_year` spread to convey variability.
    """,
    properties={
        "latitude": {"type": "number", "description": "Latitude in decimal degrees."},
        "longitude": {"type": "number", "description": "Longitude in decimal degrees."},
        "month": {"type": "integer", "description": "Month of travel, 1-12."},
        "elevation": {
            "type": "number",
            "description": (
                "Elevation in metres to correct temperatures to. Use the route's high "
                "point or the highest hut, not the valley floor."
            ),
        },
        "years": {
            "type": "integer",
            "description": "How many past years to average over (2-10). Default 5.",
        },
    },
    required=["latitude", "longitude", "month"],
)
async def get_climate_normals(
    latitude: float,
    longitude: float,
    month: int,
    elevation: float | None = None,
    years: int = 5,
) -> dict:
    month = int(month)
    if not 1 <= month <= 12:
        return {"error": f"month must be 1-12, got {month}"}

    lookback = max(2, min(int(years), 10))
    client = get_client()
    # The reanalysis archive lags real time by ~5 days, so start from last year.
    latest_complete = date.today().year - 1

    per_year: list[dict] = []
    for year in range(latest_complete - lookback + 1, latest_complete + 1):
        start = date(year, month, 1)
        end = (
            date(year + 1, 1, 1) - timedelta(days=1)
            if month == 12
            else date(year, month + 1, 1) - timedelta(days=1)
        )
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "timezone": "auto",
            "daily": ",".join(
                [
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "precipitation_sum",
                    "snowfall_sum",
                ]
            ),
        }
        if elevation is not None:
            params["elevation"] = elevation

        try:
            raw = await client.get_json(ARCHIVE_URL, params=params, ttl_seconds=ARCHIVE_TTL)
        except Exception:  # noqa: BLE001 — one bad year should not kill the average
            continue

        daily = raw.get("daily", {})
        precip = [p for p in (daily.get("precipitation_sum") or []) if p is not None]
        per_year.append(
            {
                "year": year,
                "temp_max_c": _mean(daily.get("temperature_2m_max") or []),
                "temp_min_c": _mean(daily.get("temperature_2m_min") or []),
                "total_precip_mm": round(sum(precip), 1) if precip else None,
                # "Wet day" = >1 mm. Below that is drizzle you would walk through.
                "wet_days": sum(1 for p in precip if p >= 1.0),
                "total_snowfall_cm": round(
                    sum(s for s in (daily.get("snowfall_sum") or []) if s is not None), 1
                ),
            }
        )

    if not per_year:
        return {
            "error": "No historical data returned for this location/month.",
            "hint": "Check the coordinates are on land and try again.",
        }

    totals = [y["total_precip_mm"] for y in per_year if y["total_precip_mm"] is not None]
    wettest = max(per_year, key=lambda y: y["total_precip_mm"] or -1)
    driest = min(
        per_year, key=lambda y: y["total_precip_mm"] if y["total_precip_mm"] is not None else 1e9
    )

    return {
        "month": month,
        "years_averaged": [y["year"] for y in per_year],
        "elevation_corrected_to_m": elevation,
        "mean_temp_max_c": _mean([y["temp_max_c"] for y in per_year]),
        "mean_temp_min_c": _mean([y["temp_min_c"] for y in per_year]),
        "mean_precip_mm": round(sum(totals) / len(totals), 1) if totals else None,
        "mean_wet_days": _mean([y["wet_days"] for y in per_year]),
        "mean_snowfall_cm": _mean([y["total_snowfall_cm"] for y in per_year]),
        "wettest_year": {"year": wettest["year"], "precip_mm": wettest["total_precip_mm"]},
        "driest_year": {"year": driest["year"], "precip_mm": driest["total_precip_mm"]},
        "per_year": per_year,
        "caveat": (
            "Averages, not predictions. Mountain weather varies wildly year to year — "
            "quote the wettest/driest spread so the user understands the risk."
        ),
    }
