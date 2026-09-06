"""Tests for CLI-level guards.

The API-key check earns a test because its failure mode is a bad first experience:
a key left as the `.env.example` placeholder is truthy, so a naive `if not key`
passes it through and the user gets an opaque 401 from the API instead of being
told what is actually wrong.
"""

from __future__ import annotations

import pytest

from nature_trails.cli import _key_problem, _slug


@pytest.mark.parametrize(
    "key",
    [
        None,
        "",
        "   ",
        "sk-ant-...",
        "sk-ant-",
        "your-key-here",
        "'sk-ant-api03-real'",
        '"sk-ant-api03-real"',
        "sk-proj-wrongprovider",
        "hello",
    ],
)
def test_bad_keys_are_caught_before_an_api_call(key):
    assert _key_problem(key) is not None


def test_a_plausible_key_passes():
    assert _key_problem("sk-ant-api03-Abc123DefGhi456") is None


def test_surrounding_whitespace_is_tolerated():
    """Copy-pasting a key from the console often brings a trailing newline."""
    assert _key_problem("  sk-ant-api03-Abc123  ") is None


def test_quoted_key_gets_a_specific_message():
    """.env files take raw values; quotes are a common and confusing mistake."""
    assert "quotes" in _key_problem('"sk-ant-api03-Abc123"')


def test_slug_makes_safe_filenames():
    assert _slug("Alta Via 1: Dolomites Traverse") == "alta-via-1-dolomites-traverse"
    assert _slug("Cortina d'Ampezzo / Tre Cime") == "cortina-d-ampezzo-tre-cime"
    assert _slug("!!!") == "itinerary"
    assert len(_slug("x" * 200)) <= 50
