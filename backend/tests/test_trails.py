"""Tests for OSM route-geometry stitching.

These exist because of a real bug found against live data: `get_route_geometry`
reported 456.9 km for the Alta Via 1, which OSM tags as 125 km. Two causes,
both covered here:

1. Member ways were concatenated into one coordinate list, so every jump between
   two disjoint ways was counted as travel.
2. `alternative` / `approach` / `excursion` members were included, inflating the
   length further and polluting the elevation profile with side branches.

The real relation has 391 members: 248 main line, 134 alternative, 7 approach,
2 excursion.
"""

from __future__ import annotations

from nature_trails.core.geo import path_length_km
from nature_trails.tools.trails import MAIN_LINE_ROLES, _stitch_ways


def test_stitches_ways_given_in_order():
    lines = [
        [(46.0, 12.0), (46.1, 12.0)],
        [(46.1, 12.0), (46.2, 12.0)],
        [(46.2, 12.0), (46.3, 12.0)],
    ]
    segments = _stitch_ways(lines)
    assert len(segments) == 1
    assert segments[0][0] == (46.0, 12.0)
    assert segments[0][-1] == (46.3, 12.0)
    # Shared junction nodes must not be duplicated.
    assert len(segments[0]) == 4


def test_stitches_ways_given_out_of_order():
    """OSM member order is arbitrary; the chain must still come out connected."""
    lines = [
        [(46.2, 12.0), (46.3, 12.0)],
        [(46.0, 12.0), (46.1, 12.0)],
        [(46.1, 12.0), (46.2, 12.0)],
    ]
    segments = _stitch_ways(lines)
    assert len(segments) == 1
    assert len(segments[0]) == 4


def test_stitches_ways_with_reversed_direction():
    """Ways are not consistently oriented; the stitcher must flip them."""
    lines = [
        [(46.0, 12.0), (46.1, 12.0)],
        [(46.2, 12.0), (46.1, 12.0)],  # reversed relative to its neighbour
        [(46.2, 12.0), (46.3, 12.0)],
    ]
    segments = _stitch_ways(lines)
    assert len(segments) == 1
    coords = segments[0]
    assert coords[0] == (46.0, 12.0)
    assert coords[-1] == (46.3, 12.0)


def test_no_phantom_segment_between_disjoint_pieces():
    """The bug that produced 456.9 km.

    Two ways 100 km apart must yield two segments, not one line with a 100 km
    jump joining them.
    """
    near = [(46.0, 12.0), (46.01, 12.0)]
    far = [(47.0, 12.0), (47.01, 12.0)]
    segments = _stitch_ways([near, far])

    assert len(segments) == 2
    total = sum(path_length_km(s) for s in segments)
    assert total < 5, "stitched length must not include the gap between pieces"

    naive = path_length_km(near + far)
    assert naive > 100, "the naive concatenation this replaced was badly wrong"


def test_longest_segment_wins():
    short = [(46.0, 12.0), (46.01, 12.0)]
    long_a = [(47.0, 12.0), (47.2, 12.0)]
    long_b = [(47.2, 12.0), (47.4, 12.0)]
    segments = _stitch_ways([short, long_a, long_b])
    segments.sort(key=path_length_km, reverse=True)
    assert len(segments[0]) == 3  # the two long ways joined


def test_single_way_is_returned_unchanged():
    lines = [[(46.0, 12.0), (46.1, 12.0)]]
    assert _stitch_ways(lines) == [[(46.0, 12.0), (46.1, 12.0)]]


def test_main_line_roles_exclude_side_branches():
    """Roles seen on the real Alta Via 1 relation."""
    assert "" in MAIN_LINE_ROLES
    assert "forward" in MAIN_LINE_ROLES
    assert "backward" in MAIN_LINE_ROLES
    for side_branch in ("alternative", "approach", "excursion", "connection"):
        assert side_branch not in MAIN_LINE_ROLES
