"""OpenStreetMap / Overpass tools — the heart of this project.

Why OSM rather than a travel API
--------------------------------
Commercial travel APIs index hotels and flights. They know nothing about a
hut-to-hut traverse. OpenStreetMap, by contrast, contains:

* **Hiking route relations** — the Alta Via 1, Tour du Mont Blanc, GR20, the
  Kungsleden and thousands of lesser-known routes are mapped as `route=hiking`
  relations, tagged with distance, difficulty and waymarking symbol.
* **Alpine huts** — `tourism=alpine_hut` with elevation, bed capacity, phone,
  operator (CAI, SAT, DAV, CAF...) and often a booking website.
* **The stuff that actually decides a day** — drinking water, mountain passes,
  via ferrata, and cable cars. In the Alps a gondola can turn a brutal 1600 m
  ascent into a 20-minute ride, and no travel API surfaces that.

This is the moat. It is also why the difficulty tags below matter: `sac_scale` is
the Swiss Alpine Club's six-level scale, and the difference between T2 (mountain
hiking) and T4 (alpine hiking) is the difference between a walk and a route where
people die.

A note on Overpass QL
---------------------
Bounding boxes are written `(south,west,north,east)`. `out tags center` returns
tags plus a single representative coordinate — much smaller than full geometry,
which matters because every byte lands in the model's context window.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.geo import Coord, decimate, path_length_km
from ..core.http import get_client
from .registry import tool

log = logging.getLogger(__name__)

# Overpass mirrors, tried in order. The main instance is often saturated; the
# community mirrors are frequently faster and equally current.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# Trails and huts change on a timescale of years. Cache hard.
OSM_TTL = 30 * 24 * 3600.0

BBox = list[float]  # [south, west, north, east]


async def _overpass(query: str) -> dict:
    """Run an Overpass QL query, failing over across mirrors."""
    client = get_client()
    last_error: Exception | None = None
    for url in OVERPASS_MIRRORS:
        try:
            return await client.post_json(
                url, data={"data": query}, ttl_seconds=OSM_TTL, max_attempts=2
            )
        except Exception as exc:  # noqa: BLE001 — try the next mirror
            log.info("overpass mirror %s failed: %s", url, exc)
            last_error = exc
    raise RuntimeError(
        f"All Overpass mirrors failed (last error: {last_error}). "
        "The service may be overloaded; try again shortly or use a smaller area."
    )


def _bbox_clause(bbox: BBox) -> str:
    south, west, north, east = (float(v) for v in bbox)
    return f"({south},{west},{north},{east})"


def _position(element: dict) -> tuple[float | None, float | None]:
    """Nodes carry lat/lon directly; ways and relations carry a `center`."""
    if "lat" in element and "lon" in element:
        return element["lat"], element["lon"]
    center = element.get("center") or {}
    return center.get("lat"), center.get("lon")


def _clean(tags: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    """Keep only the tags that inform a decision. Context window is a budget."""
    return {k: tags[k] for k in keys if tags.get(k)}


def _as_float(value: Any) -> float | None:
    """OSM `ele` and `distance` are free-text: '2438', '2438 m', '1,540'."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ".").split()[0])
    except (ValueError, IndexError):
        return None


# Member roles that form the route itself. Everything else — `alternative`,
# `approach`, `excursion`, `connection` — is a side branch that must not be
# counted in the route's length or sampled for its elevation profile.
MAIN_LINE_ROLES = {"", "forward", "backward", "main"}


def _endpoint_key(point: Coord) -> tuple[float, float]:
    """Round to ~0.1 m. Ways in a relation share exact junction nodes, so exact
    matching on rounded coordinates is both correct and O(1)."""
    return (round(point[0], 6), round(point[1], 6))


def _stitch_ways(lines: list[list[Coord]]) -> list[list[Coord]]:
    """Chain way geometries end-to-end into ordered polylines.

    Why this is necessary: a route relation's members are ways in arbitrary order
    and arbitrary direction. Naively concatenating their coordinates produces a
    line that teleports between way endpoints — for the Alta Via 1 that inflated a
    125 km route to 457 km, because every jump between disjoint ways was counted
    as travel.

    Worse, out-of-order points make the elevation profile meaningless: ascent
    computed over a line that jumps around a massif is noise.

    So we index every way by its two endpoints and greedily grow chains in both
    directions, reversing ways as needed. Returns one list per connected piece;
    a well-mapped route yields exactly one.
    """
    endpoints: dict[tuple[float, float], list[int]] = {}
    for index, line in enumerate(lines):
        endpoints.setdefault(_endpoint_key(line[0]), []).append(index)
        endpoints.setdefault(_endpoint_key(line[-1]), []).append(index)

    used: set[int] = set()
    segments: list[list[Coord]] = []

    for start_index in range(len(lines)):
        if start_index in used:
            continue
        used.add(start_index)
        chain = list(lines[start_index])

        # Grow off the tail, then off the head.
        for at_tail in (True, False):
            while True:
                anchor = _endpoint_key(chain[-1] if at_tail else chain[0])
                nxt = next((j for j in endpoints.get(anchor, []) if j not in used), None)
                if nxt is None:
                    break
                used.add(nxt)
                piece = list(lines[nxt])
                if at_tail:
                    if _endpoint_key(piece[0]) != anchor:
                        piece.reverse()
                    chain.extend(piece[1:])  # drop the shared junction node
                else:
                    if _endpoint_key(piece[-1]) != anchor:
                        piece.reverse()
                    chain = piece[:-1] + chain

        segments.append(chain)

    return segments


# ---------------------------------------------------------------------------
# Hiking routes
# ---------------------------------------------------------------------------

_NETWORK_MEANING = {
    "iwn": "international (major long-distance route)",
    "nwn": "national",
    "rwn": "regional",
    "lwn": "local",
}


@tool(
    name="find_hiking_routes",
    description="""
    Find named hiking routes (including multi-day and long-distance trails) inside a
    bounding box, using OpenStreetMap route relations.

    This is the primary discovery tool for a trekking trip. Famous routes — Alta Via
    1, Tour du Mont Blanc, GR20, Kungsleden, the John Muir Trail — are mapped here
    along with hundreds of regional routes that barely appear on the English web.

    Read the `network` field: `iwn`/`nwn` marks internationally or nationally
    significant routes and is the best signal for "is this a real trek or a
    waymarked afternoon loop". Read `sac_scale` for difficulty (T1 easy walking
    through T6 extreme alpine); anything T3 or above implies exposure and
    scrambling, and T4+ should not be recommended to a general audience without
    saying so explicitly.

    Note that `length_km` comes from OSM's own tagging when present and is otherwise
    computed from geometry, so treat it as approximate. Use `get_route_geometry`
    followed by `get_elevation` when you need a defensible ascent figure.
    """,
    properties={
        "bbox": {
            "type": "array",
            "description": (
                "Search area as [south, west, north, east] in decimal degrees. Get "
                "this from `geocode_place` or `make_bounding_box`. Keep the area "
                "under roughly 60x60 km or the query may time out."
            ),
            "items": {"type": "number"},
            "minItems": 4,
            "maxItems": 4,
        },
        "min_length_km": {
            "type": "number",
            "description": "Filter out routes shorter than this. Use ~30 to find multi-day treks.",
        },
        "max_length_km": {"type": "number", "description": "Filter out routes longer than this."},
        "limit": {"type": "integer", "description": "Max routes to return (1-60). Default 25."},
    },
    required=["bbox"],
)
async def find_hiking_routes(
    bbox: BBox,
    min_length_km: float | None = None,
    max_length_km: float | None = None,
    limit: int = 25,
) -> dict:
    area = _bbox_clause(bbox)
    query = f"""
    [out:json][timeout:90];
    (
      relation["route"~"^(hiking|foot)$"]["name"]{area};
    );
    out tags center;
    """
    raw = await _overpass(query)

    routes = []
    for element in raw.get("elements", []):
        tags = element.get("tags", {})
        lat, lon = _position(element)
        length = _as_float(tags.get("distance"))

        if min_length_km is not None and (length is None or length < float(min_length_km)):
            continue
        if max_length_km is not None and length is not None and length > float(max_length_km):
            continue

        network = tags.get("network", "")
        routes.append(
            {
                "osm_relation_id": element.get("id"),
                "name": tags.get("name"),
                "ref": tags.get("ref"),
                "length_km": length,
                "network": network,
                "network_meaning": _NETWORK_MEANING.get(network),
                "sac_scale": tags.get("sac_scale"),
                "roundtrip": tags.get("roundtrip"),
                "ascent_m": _as_float(tags.get("ascent")),
                "descent_m": _as_float(tags.get("descent")),
                "operator": tags.get("operator"),
                "website": tags.get("website") or tags.get("url"),
                "description": (tags.get("description") or "")[:300] or None,
                "waymark_symbol": tags.get("osmc:symbol") or tags.get("symbol"),
                "center_lat": lat,
                "center_lon": lon,
            }
        )

    # Rank by significance (international first), then by length descending —
    # the agent should see the headline treks before local loops.
    rank = {"iwn": 0, "nwn": 1, "rwn": 2, "lwn": 3}
    routes.sort(key=lambda r: (rank.get(r["network"], 9), -(r["length_km"] or 0)))
    capped = routes[: max(1, min(int(limit), 60))]

    payload: dict = {"routes": capped, "total_found": len(routes)}
    if not capped:
        payload["hint"] = (
            "No hiking route relations here. Either the area is genuinely unmapped for "
            "routes (common outside Europe — in North America individual trails are often "
            "mapped as ways, not relations), or the bbox is too small. Try a larger bbox, "
            "drop the length filters, or fall back to web_search for named trails."
        )
    return payload


@tool(
    name="get_route_geometry",
    description="""
    Fetch the actual line geometry of an OSM hiking route relation, thinned to a
    manageable number of points.

    Use this after `find_hiking_routes` when you need real numbers rather than
    tagged ones. Feed the returned `coordinates` straight into `get_elevation` to
    get a defensible ascent/descent figure and a high point — which is what
    determines whether a route needs crampons in June.

    The returned points follow the route in walking order, so ascent computed from
    them is meaningful rather than noise.

    Read the numbers together. `main_line_length_km` is measured from the stitched
    main line, with `alternative`, `approach` and `excursion` branches excluded —
    those are side trips, not the route. `tagged_distance_km` is what OSM mappers
    recorded. When the two agree, the geometry is sound and its elevation profile
    is trustworthy. When a `warning` field appears they disagree by more than 25%,
    which means the relation is fragmented or partially mapped: prefer the tagged
    distance, and treat any ascent sampled from these points as approximate — say
    so in the itinerary rather than presenting it as measured.
    """,
    properties={
        "osm_relation_id": {
            "type": "integer",
            "description": "The `osm_relation_id` from a `find_hiking_routes` result.",
        },
        "max_points": {
            "type": "integer",
            "description": (
                "How many points to return along the route (10-300). Default 60, "
                "sampled evenly so the shape is preserved.\n\n"
                "Choose this by target SPACING, not by 'more is better'. Aim for "
                "roughly one point every 0.5-1.0 km — that is the density that "
                "matches published guidebook ascent figures. So divide the route "
                "length by ~0.75: a 120 km route wants ~160 points, a 12 km day "
                "stage wants ~20. Coarser under-reports climbing; finer inflates it "
                "with terrain-model noise. `get_elevation` reports the resulting "
                "`spacing_km` and tells you which way it is biased."
            ),
        },
    },
    required=["osm_relation_id"],
)
async def get_route_geometry(osm_relation_id: int, max_points: int = 60) -> dict:
    query = f"""
    [out:json][timeout:120];
    relation({int(osm_relation_id)});
    out geom;
    """
    raw = await _overpass(query)
    elements = raw.get("elements", [])
    if not elements:
        return {"error": f"No relation {osm_relation_id} found."}

    relation = elements[0]
    tags = relation.get("tags", {})
    members = relation.get("members", [])

    # Keep only the main line. A big route relation carries far more than the
    # route: the Alta Via 1 has 391 member ways, of which 134 are tagged
    # `alternative`, 7 `approach` and 2 `excursion`. Including them measured
    # 179 km against a tagged 125 km; main-line-only measures 118 km.
    kept: list[list[Coord]] = []
    excluded: dict[str, int] = {}
    for member in members:
        role = member.get("role", "") or ""
        if role not in MAIN_LINE_ROLES:
            excluded[role] = excluded.get(role, 0) + 1
            continue
        line = [
            (p["lat"], p["lon"])
            for p in (member.get("geometry") or [])
            if p and "lat" in p and "lon" in p
        ]
        if len(line) >= 2:
            kept.append(line)

    if not kept:
        return {
            "error": "Relation has no usable main-line geometry.",
            "hint": (
                "It may be a super-relation whose sections are separate relations, or "
                "mapped entirely as variants. Use the tagged distance from "
                "find_hiking_routes instead."
            ),
            "excluded_member_roles": excluded,
        }

    segments = _stitch_ways(kept)
    segments.sort(key=path_length_km, reverse=True)
    main_line = segments[0]

    sampled = decimate(main_line, max(10, min(int(max_points), 300)))
    main_km = round(path_length_km(main_line), 1)
    tagged_km = _as_float(tags.get("distance"))

    payload: dict = {
        "name": tags.get("name"),
        "osm_relation_id": osm_relation_id,
        "main_line_length_km": main_km,
        "tagged_distance_km": tagged_km,
        "mapped_length_all_segments_km": round(sum(path_length_km(s) for s in segments), 1),
        "connected_segments": len(segments),
        "main_line_point_count": len(main_line),
        "excluded_member_roles": excluded,
        "coordinates": [[round(lat, 5), round(lon, 5)] for lat, lon in sampled],
        "next_step": (
            "Pass `coordinates` to get_elevation for ascent/descent and the high point. "
            "The points are in walking order, so the ascent figure is meaningful."
        ),
    }

    # Surface disagreement rather than hiding it — the model should know when to
    # distrust this number.
    if tagged_km and main_km and abs(main_km - tagged_km) / tagged_km > 0.25:
        payload["warning"] = (
            f"Stitched geometry ({main_km} km) disagrees with the tagged distance "
            f"({tagged_km} km) by more than 25%. The relation is probably fragmented "
            "or partially mapped. Prefer the tagged distance and treat elevation "
            "sampled from these points as approximate."
        )
    if len(segments) > 1:
        payload["note"] = (
            f"The main line is one of {len(segments)} disconnected pieces; the longest "
            "was used. Ascent from these points covers that piece only."
        )
    return payload


# ---------------------------------------------------------------------------
# Accommodation: huts, refuges, bivouacs, campsites
# ---------------------------------------------------------------------------

_HUT_PRESETS = {
    "alpine_hut": 'node["tourism"="alpine_hut"]{a};way["tourism"="alpine_hut"]{a};',
    "wilderness_hut": 'node["tourism"="wilderness_hut"]{a};way["tourism"="wilderness_hut"]{a};',
    "camp_site": 'node["tourism"="camp_site"]{a};way["tourism"="camp_site"]{a};',
    "shelter": 'node["amenity"="shelter"]{a};',
    "hostel": 'node["tourism"="hostel"]{a};way["tourism"="hostel"]{a};',
    "guest_house": 'node["tourism"~"^(guest_house|chalet)$"]{a};way["tourism"~"^(guest_house|chalet)$"]{a};',
    "hotel": 'node["tourism"="hotel"]{a};way["tourism"="hotel"]{a};',
}

_HUT_TAGS = [
    "name",
    "tourism",
    "ele",
    "capacity",
    "beds",
    "phone",
    "contact:phone",
    "website",
    "contact:website",
    "email",
    "operator",
    "seasonal",
    "opening_hours",
    "reservation",
    "shower",
    "internet_access",
    "fireplace",
    "drinking_water",
    "shelter_type",
    "backcountry",
]


@tool(
    name="find_mountain_huts",
    description="""
    Find places to sleep in the mountains: staffed alpine huts (rifugi, refuges,
    Hütten), unstaffed wilderness huts and bivouacs, campsites, shelters and
    valley-floor guesthouses.

    This is the accommodation tool for wilderness trips — the equivalent of a hotel
    search, but for terrain where hotels do not exist. Alpine huts are the entire
    basis of hut-to-hut trekking in Europe, and OSM often carries the operator
    (CAI, SAT, DAV, CAF, OeAV), bed capacity, phone and booking website.

    Critical planning facts to surface to the user:
    - Staffed huts are strictly **seasonal** (typically late June to late September
      in the Alps) and **must be booked in advance** in summer — popular huts on the
      Alta Via routes fill months ahead.
    - `ele` (elevation) tells you whether a hut is a valley base or a high camp.
    - A `wilderness_hut` is usually unstaffed and free, with no food and no booking.

    Where OSM lacks opening dates or booking details, follow up with `web_search`
    on the hut's name plus operator.
    """,
    properties={
        "bbox": {
            "type": "array",
            "description": "Search area as [south, west, north, east].",
            "items": {"type": "number"},
            "minItems": 4,
            "maxItems": 4,
        },
        "types": {
            "type": "array",
            "description": (
                "Which kinds of accommodation to include. Options: alpine_hut, "
                "wilderness_hut, camp_site, shelter, hostel, guest_house, hotel. "
                "Defaults to alpine_hut + wilderness_hut + camp_site + shelter, which "
                "is what matters for a backcountry trip."
            ),
            "items": {"type": "string"},
        },
        "limit": {"type": "integer", "description": "Max results (1-80). Default 40."},
    },
    required=["bbox"],
)
async def find_mountain_huts(bbox: BBox, types: list[str] | None = None, limit: int = 40) -> dict:
    area = _bbox_clause(bbox)
    selected = types or ["alpine_hut", "wilderness_hut", "camp_site", "shelter"]
    clauses = "".join(_HUT_PRESETS[t].replace("{a}", area) for t in selected if t in _HUT_PRESETS)
    if not clauses:
        return {
            "error": f"No valid types in {selected!r}.",
            "valid_types": sorted(_HUT_PRESETS),
        }

    raw = await _overpass(f"[out:json][timeout:90];({clauses});out tags center;")

    huts = []
    for element in raw.get("elements", []):
        tags = element.get("tags", {})
        if not tags.get("name") and tags.get("tourism") != "wilderness_hut":
            continue  # unnamed hotels/campsites are noise; unnamed bivouacs are not
        lat, lon = _position(element)
        entry = _clean(tags, _HUT_TAGS)
        entry["ele_m"] = _as_float(tags.get("ele"))
        entry.pop("ele", None)
        entry["lat"], entry["lon"] = lat, lon
        entry["osm_id"] = element.get("id")
        huts.append(entry)

    # Highest first: on a traverse the high huts are the ones that constrain the plan.
    huts.sort(key=lambda h: -(h.get("ele_m") or 0))
    capped = huts[: max(1, min(int(limit), 80))]

    payload: dict = {"huts": capped, "total_found": len(huts)}
    if not capped:
        payload["hint"] = (
            "Nothing found. Widen the bbox, or add 'hostel'/'guest_house'/'hotel' to "
            "`types` if this is a valley-based rather than hut-based trip."
        )
    else:
        payload["reminder"] = (
            "Staffed huts are seasonal and require advance booking. Verify opening "
            "dates for the user's travel month before committing to an itinerary."
        )
    return payload


# ---------------------------------------------------------------------------
# Amenities that decide a day
# ---------------------------------------------------------------------------

_AMENITY_PRESETS = {
    "drinking_water": 'node["amenity"="drinking_water"]{a};node["natural"="spring"]["drinking_water"="yes"]{a};',
    "peak": 'node["natural"="peak"]["name"]{a};',
    "viewpoint": 'node["tourism"="viewpoint"]{a};',
    "mountain_pass": 'node["mountain_pass"="yes"]{a};node["natural"="saddle"]["name"]{a};',
    "via_ferrata": 'way["highway"="via_ferrata"]{a};',
    "cable_car": 'node["aerialway"="station"]{a};way["aerialway"~"^(cable_car|gondola|chair_lift|mixed_lift)$"]{a};',
    "glacier": 'way["natural"="glacier"]{a};',
    "toilets": 'node["amenity"="toilets"]{a};',
    "waterfall": 'node["waterway"="waterfall"]{a};',
    "lake": 'way["natural"="water"]["name"]{a};',
}

_AMENITY_TAGS = [
    "name",
    "natural",
    "tourism",
    "amenity",
    "aerialway",
    "ele",
    "mountain_pass",
    "operator",
    "seasonal",
    "opening_hours",
    "website",
    "prominence",
]


@tool(
    name="find_trail_amenities",
    description="""
    Find features along and around a route that shape how a day actually goes:
    drinking water, named peaks, viewpoints, mountain passes, via ferrata, cable
    cars and lifts, glaciers, waterfalls and lakes.

    Two of these change plans more than anything else:

    - **`cable_car`** — in the Alps and Dolomites a lift can remove 1200 m of
      ascent and turn an impossible day into a comfortable one. Always check for
      lifts before declaring a stage too hard. They are seasonal, so check
      `seasonal`/`opening_hours` and verify with `web_search`.
    - **`drinking_water`** — determines how much water the user must carry, which
      feeds directly into `build_packing_list`. A dry limestone massif like the
      Dolomites has very few natural sources; a granite range may have many.

    `via_ferrata` and `glacier` are safety signals: either implies technical gear
    (harness, via ferrata set, or rope and crampons) and should be flagged loudly
    if it intersects a suggested route.
    """,
    properties={
        "bbox": {
            "type": "array",
            "description": "Search area as [south, west, north, east].",
            "items": {"type": "number"},
            "minItems": 4,
            "maxItems": 4,
        },
        "kinds": {
            "type": "array",
            "description": (
                "Which features to look for. Options: drinking_water, peak, viewpoint, "
                "mountain_pass, via_ferrata, cable_car, glacier, toilets, waterfall, "
                "lake. Request only what you need — each kind adds result volume."
            ),
            "items": {"type": "string"},
        },
        "limit": {"type": "integer", "description": "Max results per kind (1-60). Default 20."},
    },
    required=["bbox", "kinds"],
)
async def find_trail_amenities(bbox: BBox, kinds: list[str], limit: int = 20) -> dict:
    area = _bbox_clause(bbox)
    valid = [k for k in kinds if k in _AMENITY_PRESETS]
    if not valid:
        return {
            "error": f"No valid kinds in {kinds!r}.",
            "valid_kinds": sorted(_AMENITY_PRESETS),
        }

    cap = max(1, min(int(limit), 60))
    grouped: dict[str, list[dict]] = {}

    # One Overpass call per kind, so results stay attributable to the kind that
    # produced them. The rate limiter keeps this polite.
    for kind in valid:
        clause = _AMENITY_PRESETS[kind].replace("{a}", area)
        raw = await _overpass(f"[out:json][timeout:90];({clause});out tags center;")
        items = []
        for element in raw.get("elements", []):
            tags = element.get("tags", {})
            lat, lon = _position(element)
            entry = _clean(tags, _AMENITY_TAGS)
            entry["ele_m"] = _as_float(tags.get("ele"))
            entry.pop("ele", None)
            entry["lat"], entry["lon"] = lat, lon
            items.append(entry)
        # Named and high things first — that is what a planner cares about.
        items.sort(key=lambda x: (x.get("name") is None, -(x.get("ele_m") or 0)))
        grouped[kind] = items[:cap]

    return {
        "amenities": grouped,
        "counts": {k: len(v) for k, v in grouped.items()},
    }


@tool(
    name="find_trailhead_access",
    description="""
    Find how to physically reach a trailhead: car parks, bus stops, railway
    stations and cable-car base stations.

    This is the most under-planned part of wilderness travel and the most common
    reason a trip falls apart. A route can be perfect and still be unreachable
    without a car, or start 900 m above the nearest bus stop. Check this for the
    start and end of every multi-day route — point-to-point traverses in particular
    need a plan for getting back to the start.

    Returns public transport separately from parking so you can answer "is this
    doable without renting a car?" directly.
    """,
    properties={
        "bbox": {
            "type": "array",
            "description": (
                "Area around the trailhead as [south, west, north, east]. Use a small "
                "box (5-15 km) centred on the route start."
            ),
            "items": {"type": "number"},
            "minItems": 4,
            "maxItems": 4,
        },
        "limit": {"type": "integer", "description": "Max results per category. Default 15."},
    },
    required=["bbox"],
)
async def find_trailhead_access(bbox: BBox, limit: int = 15) -> dict:
    area = _bbox_clause(bbox)
    cap = max(1, min(int(limit), 40))

    queries = {
        "parking": f'node["amenity"="parking"]{area};way["amenity"="parking"]{area};',
        "bus_stop": f'node["highway"="bus_stop"]{area};node["public_transport"="station"]["bus"="yes"]{area};',
        "rail_station": f'node["railway"~"^(station|halt)$"]{area};',
        "cable_car_station": f'node["aerialway"="station"]{area};',
    }

    out: dict[str, list[dict]] = {}
    for category, clause in queries.items():
        raw = await _overpass(f"[out:json][timeout:60];({clause});out tags center;")
        items = []
        for element in raw.get("elements", []):
            tags = element.get("tags", {})
            lat, lon = _position(element)
            items.append(
                {
                    "name": tags.get("name"),
                    "operator": tags.get("operator"),
                    "fee": tags.get("fee"),
                    "capacity": tags.get("capacity"),
                    "access": tags.get("access"),
                    "network": tags.get("network"),
                    "lat": lat,
                    "lon": lon,
                }
            )
        items.sort(key=lambda x: x.get("name") is None)
        out[category] = items[:cap]

    has_transit = bool(out["bus_stop"] or out["rail_station"] or out["cable_car_station"])
    return {
        "access": out,
        "counts": {k: len(v) for k, v in out.items()},
        "car_free_feasible": has_transit,
        "note": (
            "OSM coverage of rural bus routes is patchy and schedules are not included. "
            "Confirm timetables with web_search before relying on a car-free plan."
        ),
    }
