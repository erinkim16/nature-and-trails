"""Tests for the coordinate maths.

These are the cheapest tests in the project and they protect the numbers the whole
itinerary rests on. If `elevation_stats` is wrong, every difficulty rating is wrong.
"""

from __future__ import annotations

from datetime import date

import pytest

from nature_trails.core.geo import (
    bbox_around,
    decimate,
    elevation_stats,
    haversine_km,
    naismith_hours,
    path_length_km,
    sun_times,
)


def test_haversine_known_distance():
    # Cortina d'Ampezzo to Innsbruck: ~98 km great-circle (not the ~130 km by road).
    d = haversine_km(46.5405, 12.1357, 47.2692, 11.4041)
    assert 95 < d < 102


def test_haversine_is_zero_for_same_point():
    assert haversine_km(46.5, 12.1, 46.5, 12.1) == pytest.approx(0.0, abs=1e-9)


def test_bbox_widens_with_latitude():
    """Longitude degrees shrink toward the poles, so the box must widen."""
    _, west_eq, _, east_eq = bbox_around(0.0, 0.0, 25.0)
    _, west_hi, _, east_hi = bbox_around(70.0, 0.0, 25.0)
    assert (east_hi - west_hi) > (east_eq - west_eq) * 2


def test_bbox_is_ordered_south_west_north_east():
    south, west, north, east = bbox_around(46.5, 12.1, 10.0)
    assert south < north
    assert west < east


def test_decimate_preserves_endpoints():
    coords = [(float(i), float(i)) for i in range(500)]
    out = decimate(coords, 50)
    assert len(out) == 50
    assert out[0] == coords[0]
    assert out[-1] == coords[-1]


def test_decimate_is_a_noop_when_under_limit():
    coords = [(1.0, 1.0), (2.0, 2.0)]
    assert decimate(coords, 50) == coords


def test_elevation_noise_does_not_become_phantom_ascent():
    """The whole reason `noise_threshold_m` exists.

    A dead-flat path sampled by a noisy elevation model must report ~0 ascent,
    not the sum of every 3 m wobble.
    """
    flat_but_noisy = [1000, 1003, 998, 1002, 999, 1001, 1000] * 10
    stats = elevation_stats([float(e) for e in flat_but_noisy])
    assert stats["ascent_m"] == 0
    assert stats["descent_m"] == 0


def test_elevation_captures_real_climb():
    profile = [float(e) for e in range(1000, 2500, 25)]
    stats = elevation_stats(profile)
    assert stats["ascent_m"] == pytest.approx(1475, abs=30)
    assert stats["descent_m"] == 0
    assert stats["max_m"] == 2475


def test_elevation_handles_short_input():
    assert elevation_stats([])["ascent_m"] == 0
    assert elevation_stats([1200.0])["ascent_m"] == 0


def test_path_length_accumulates():
    coords = [(46.0, 12.0), (46.1, 12.0), (46.2, 12.0)]
    assert path_length_km(coords) == pytest.approx(22.2, abs=1.0)
    assert path_length_km([(46.0, 12.0)]) == 0.0


def test_naismith_penalises_ascent_more_than_distance():
    flat = naismith_hours(12, 100)
    steep = naismith_hours(12, 1400)
    assert steep > flat * 1.8


def test_naismith_fitness_scaling():
    assert naismith_hours(20, 1000, "fast") < naismith_hours(20, 1000, "average")
    assert naismith_hours(20, 1000, "relaxed") > naismith_hours(20, 1000, "average")


def test_daylight_summer_vs_winter_in_the_alps():
    summer = sun_times(46.54, 12.14, date(2026, 6, 21))
    winter = sun_times(46.54, 12.14, date(2026, 12, 21))
    assert summer["daylight_hours"] > 15
    assert winter["daylight_hours"] < 9
    assert summer["sunrise_utc"] < summer["sunset_utc"]


def test_daylight_midnight_sun_above_the_arctic_circle():
    result = sun_times(78.2, 15.6, date(2026, 6, 21))  # Svalbard
    assert result["daylight_hours"] == 24.0
    assert result["note"] == "midnight sun"


def test_daylight_polar_night():
    result = sun_times(78.2, 15.6, date(2026, 12, 21))
    assert result["daylight_hours"] == 0.0
    assert result["note"] == "polar night"
