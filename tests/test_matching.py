"""Unit tests for the pro matching engine (pro/matching.py).

Pure-function coverage: haversine_miles, _score_property, _build_match_query,
and the tier-limit contract. No database required.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pro.matching import (  # noqa: E402
    DEFAULT_REHAB_PER_SQFT,
    TIER_LIMITS,
    _build_match_query,
    _score_property,
    haversine_miles,
)


def test_tier_limits_contract():
    assert TIER_LIMITS == {"basic": 3, "pro": 5, "premium": None}


def test_haversine_zero_distance():
    assert haversine_miles(35.05, -85.31, 35.05, -85.31) == pytest.approx(0.0, abs=1e-6)


def test_haversine_one_degree_latitude():
    # 1 degree of latitude ≈ 69 miles
    d = haversine_miles(35.0, -85.0, 36.0, -85.0)
    assert 68.0 < d < 70.0


def test_score_property_within_bounds():
    row = {
        "assessed_value": 150_000,
        "building_sqft": 1_400,
        "signal_types": ["tax_delinquent", "vacancy"],
    }
    profile = {
        "preferred_signals": ["tax_delinquent"],
        "max_rehab_cost": 60_000,
        "property_types": ["single_family"],
    }
    score = _score_property(row, profile)
    assert 0.0 <= score <= 100.0
    assert score > 0


def test_score_property_no_preferences_is_neutral():
    """No preferred signals + empty profile → still returns a bounded score."""
    row = {"assessed_value": 0, "building_sqft": 0, "signal_types": []}
    score = _score_property(row, {})
    assert 0.0 <= score <= 100.0


def test_score_property_signal_alignment_beats_no_alignment():
    aligned = _score_property(
        {"assessed_value": 100_000, "building_sqft": 1_000, "signal_types": ["probate"]},
        {"preferred_signals": ["probate"]},
    )
    misaligned = _score_property(
        {"assessed_value": 100_000, "building_sqft": 1_000, "signal_types": []},
        {"preferred_signals": ["probate"]},
    )
    assert aligned > misaligned


def test_build_match_query_filters_and_params():
    query, params = _build_match_query(
        county_ids=["hamilton"],
        max_total_investment=250_000,
        target_areas=["37402"],
        property_types=["single_family"],
        limit=50,
    )
    assert "p.county_id = ANY(%s)" in query
    assert "LIMIT %s" in query
    # county ids, rehab $/sqft, budget, zips, limit — order matters for %s
    assert params[0] == ["hamilton"]
    assert params[-1] == 50
    assert 250_000 in params
    assert DEFAULT_REHAB_PER_SQFT in params


def test_build_match_query_land_filter():
    query, _params = _build_match_query(
        county_ids=["hamilton"],
        max_total_investment=100_000,
        property_types=["land"],
    )
    assert "p.building_sqft IS NULL" in query
