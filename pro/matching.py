"""
Investor Deal Matching Engine (Pro)
===================================
Takes an investor profile and returns ranked deal matches from the
properties + distress_signals tables.

De facto pro: every query here reads the pro cut tables (users,
investor_profiles) and its only importer is pro/investor_router.py, so it
moved from api/core/matching.py to pro/ in Task 2.2. api/ must not reference
this module.

Scoring phases:
  1. Hard filters (budget, county, target areas)
  2. Soft scoring (equity spread, signal alignment, rehab fit, type match)
  3. Tier-based result limits (basic=3, pro=5, premium=unlimited)
  4. Close-calls fallback: relax target_areas when zero matches
"""
from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

from api.core import market as market_scoring
from api.core.queries import row_to_property

# Tier → max results mapping
TIER_LIMITS: Dict[str, Optional[int]] = {
    "basic": 3,
    "pro": 5,
    "premium": None,  # unlimited
}

# Default rehab cost per sqft (from appraisal.py)
DEFAULT_REHAB_PER_SQFT = 40.0


def get_conn() -> psycopg2.extensions.connection:
    """Return a new psycopg2 connection (DSN read at call time, never import time)."""
    return psycopg2.connect(os.getenv("DATABASE_URL"))


# ── Distance helpers (miles) ─────────────────────────────────────

def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Haversine distance in miles between two lat/lng pairs."""
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


# ── Hard-filter query ────────────────────────────────────────────

def _build_match_query(
    county_ids: List[str],
    max_total_investment: float,
    target_areas: Optional[List[str]] = None,
    property_types: Optional[List[str]] = None,
    max_rehab_cost: Optional[float] = None,
    limit: int = 200,
) -> tuple[str, list]:
    """Build the SQL query for candidate properties.

    Hard filters:
      - county_id IN (…)
      - assessed_value + estimated_rehab <= max_total_investment
      - target_areas: postal_code match when specified
    """
    conditions: list[str] = []
    params: list[Any] = []

    # County filter
    conditions.append("p.county_id = ANY(%s)")
    params.append(county_ids)

    # Budget filter: assessed_value + estimated_rehab <= max_total_investment
    # estimated_rehab = building_sqft * DEFAULT_REHAB_PER_SQFT (when sqft known)
    # For land or unknown sqft, use max_rehab_cost or default
    rehab_expr = (
        "COALESCE(p.building_sqft, 0) * %s"
    )
    conditions.append(
        "COALESCE(p.assessed_value, 0) + "
        f"{rehab_expr} <= %s"
    )
    params.append(DEFAULT_REHAB_PER_SQFT)
    params.append(max_total_investment)

    # Target areas — match postal_code against known zip codes
    if target_areas and len(target_areas) > 0:
        conditions.append("p.postal_code = ANY(%s)")
        params.append(target_areas)

    # Property types — filter by building_sqft ranges
    if property_types:
        type_conditions = []
        for pt in property_types:
            if pt == "single_family":
                type_conditions.append(
                    "p.building_sqft IS NOT NULL AND p.building_sqft <= 5000"
                )
            elif pt == "multi_unit":
                type_conditions.append(
                    "p.building_sqft IS NOT NULL AND p.building_sqft > 5000 AND p.building_sqft <= 20000"
                )
            elif pt == "land":
                type_conditions.append("p.building_sqft IS NULL")
        if type_conditions:
            conditions.append("(" + " OR ".join(type_conditions) + ")")

    # Distress-scored properties only (skip green-field properties)
    conditions.append("p.distress_score > 0")

    where_sql = "WHERE " + " AND ".join(conditions) if conditions else ""

    query = f"""
        SELECT p.*,
               COALESCE(
                   ARRAY_AGG(DISTINCT ds.signal_type) FILTER (WHERE ds.signal_type IS NOT NULL),
                   ARRAY[]::text[]
               ) AS signal_types
        FROM properties p
        LEFT JOIN distress_signals ds ON ds.property_id = p.id
        {where_sql}
        GROUP BY p.id
        ORDER BY p.deal_score DESC
        LIMIT %s
    """
    params.append(limit)
    return query, params


# ── Scoring ───────────────────────────────────────────────────────

def _score_property(
    row: dict,
    profile: dict,
) -> float:
    """Score a single property against an investor profile.

    Returns a composite score (higher = better match):
      - equity_spread_score  (0-40)
      - signal_alignment      (0-25)
      - rehab_fit            (0-20)
      - type_match           (0-15)
    """
    assessed = float(row.get("assessed_value") or 0)
    sqft = float(row.get("building_sqft") or 0)
    rehab_cost = sqft * DEFAULT_REHAB_PER_SQFT if sqft > 0 else (profile.get("max_rehab_cost") or 50000)
    total_cost = assessed + rehab_cost

    # ARV estimate: assessed_value + rehab_cost (simplified)
    arv = assessed * 1.3 if assessed > 0 else 0  # 30% value-add assumption
    equity_spread = (arv - total_cost) / arv if arv > 0 else 0
    equity_spread_score = min(max(equity_spread, 0) * 40, 40)

    # Signal alignment
    preferred = set(profile.get("preferred_signals") or [])
    signals = set(row.get("signal_types") or [])
    if preferred:
        signal_overlap = len(preferred & signals) / len(preferred)
    else:
        # No preference — reward any distress signal
        signal_overlap = 0.5 if signals else 0
    signal_score = signal_overlap * 25

    # Rehab cost fit — closer to max_rehab_cost without exceeding = better
    max_rehab = profile.get("max_rehab_cost") or 50000
    if rehab_cost <= max_rehab:
        rehab_fit = 1.0 - (rehab_cost / max_rehab)  # 1.0 = very cheap, 0.0 = at budget
    else:
        rehab_fit = 0.0
    rehab_score = rehab_fit * 20

    # Property type match
    property_types = set(profile.get("property_types") or ["single_family"])
    has_building = sqft > 0
    type_match_score = 15.0 if property_types else 7.5  # neutral if no preference
    if has_building and sqft <= 5000 and "single_family" in property_types:
        type_match_score = 15.0
    elif has_building and sqft > 5000 and "multi_unit" in property_types:
        type_match_score = 15.0
    elif not has_building and "land" in property_types:
        type_match_score = 15.0

    return equity_spread_score + signal_score + rehab_score + type_match_score


# ── Matching core ─────────────────────────────────────────────────

def score_leads(
    profile: Dict[str, Any],
    *,
    broaden: bool = False,
) -> List[dict]:
    """Score candidate properties against a profile dict; return ranked deals.

    The engine behind both the pro MCP ``score_leads`` tool and
    ``match_deals_for_investor``. ``profile`` keys (all optional unless
    noted): ``deal_type``, ``max_total_investment``, ``min_equity_pct``,
    ``target_areas`` (postal codes), ``property_types``,
    ``max_rehab_cost``, ``preferred_signals``, ``county_ids``, ``tier``
    (basic | pro | premium — result-limit tier). ``min_equity_pct`` is
    accepted for API compatibility but not yet applied — no equity data is
    populated, so there is nothing to filter on (see the note in
    ``match_deals_for_investor``).

    Args:
        profile: the criteria dict described above.
        broaden: if True, skip the target_areas filter (close-calls mode).
    """
    conn = get_conn()
    try:
        tier = profile.get("tier") or "premium"

        # 1. Query candidates under the hard filters
        query, params = _build_match_query(
            county_ids=profile.get("county_ids") or ["hamilton"],
            max_total_investment=float(profile.get("max_total_investment") or 0),
            target_areas=profile.get("target_areas") if not broaden else None,
            property_types=profile.get("property_types"),
            max_rehab_cost=profile.get("max_rehab_cost"),
        )

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            candidates = cur.fetchall()

        if not candidates:
            return []

        # 2. Score and rank
        scored: List[tuple[float, dict]] = []
        for c in candidates:
            cdict = dict(c)
            score = _score_property(cdict, profile)
            scored.append((score, cdict))

        scored.sort(key=lambda x: x[0], reverse=True)

        # NOTE: min_equity_pct is accepted for API compatibility but not yet
        # applied — deal_score is a 0-100 composite (and currently
        # unpopulated), not an equity percentage. Equity filtering lands with
        # the deal-score wiring; until then, results rank by match score.

        # 3. Apply tier limit
        max_results = TIER_LIMITS.get(tier)
        if max_results is not None:
            scored = scored[:max_results]

        # 4. Convert to property dicts
        results = []
        for score, cdict in scored:
            prop = row_to_property(cdict)
            prop["match_score"] = round(score, 2)
            results.append(prop)

        return results

    finally:
        conn.close()


# ── Investor entry point ──────────────────────────────────────────

def match_deals_for_investor(
    investor_id: str,
    *,
    broaden: bool = False,
) -> List[dict]:
    """Find and rank deals matching an investor's stored profile.

    Args:
        investor_id: users.id (uuid) of the investor
        broaden: if True and zero matches, relax target_areas filter

    Returns:
        List of matched deal dicts ranked by score.
    """
    conn = get_conn()
    try:
        # 1. Load investor + profile
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT u.id, u.email, u.tier, u.subscription_status,
                       ip.deal_type, ip.max_total_investment,
                       ip.min_equity_pct, ip.target_areas,
                       ip.property_types, ip.max_rehab_cost,
                       ip.preferred_signals, ip.county_ids
                FROM users u
                JOIN investor_profiles ip ON ip.investor_id = u.id
                WHERE u.id = %s::uuid
            """, (investor_id,))
            row = cur.fetchone()
            if not row:
                return []

        # Build profile dict
        profile = {
            "deal_type": row["deal_type"],
            "max_total_investment": float(row["max_total_investment"] or 0),
            "min_equity_pct": float(row["min_equity_pct"] or 20),
            "target_areas": row["target_areas"] or [],
            "property_types": row["property_types"] or ["single_family"],
            "max_rehab_cost": float(row["max_rehab_cost"]) if row["max_rehab_cost"] else None,
            "preferred_signals": row["preferred_signals"] or [],
            "county_ids": row["county_ids"] or ["hamilton"],
        }
        profile["tier"] = row["tier"]

        # 2. Score through the shared core
        results = score_leads(profile, broaden=broaden)
        if not results and not broaden:
            # Close-calls mode: retry without target_areas
            return match_deals_for_investor(investor_id, broaden=True)
        return results

    finally:
        conn.close()
