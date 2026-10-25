"""
Appraisal Engine — Comparable Sales Analysis
============================================
Uses the properties table as a comp pool. For a given subject property:
  1. Find comparable sales within adaptive radius
  2. Weight by distance + time decay + condition
  3. Return weighted average $/sqft → CMV (current market value)
  4. Return ARV (after-repair value) using well-maintained comps

Comp criteria:
  - Adaptive radius: start 0.25mi, expand to 5mi (min 5 comps needed)
  - Sqft tolerance: ±20% of subject
  - Sale age window: 24 months
  - Time decay: e^(-0.04 * months_ago)
"""
from __future__ import annotations

import copy
import datetime
import json
import math
import os
from pathlib import Path
from typing import Any, Optional, Dict, List

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query

from core.gate import require_pro_license
from sanitize import sanitize_number, sanitize_url_path, validate_county_id
from cache import cache, cached

router = APIRouter()

# Default rehab assumption for value-add (tunable). Moderate cosmetic + systems refresh.
DEFAULT_REHAB_PER_SQFT = 40.0


def _db_dsn() -> str:
    """Resolve DATABASE_URL at call time — loud error if unset (no default DSN)."""
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set — the appraisal engine requires an explicit "
            "DSN (export it, e.g. from the repo .env; no default is shipped)."
        )
    return dsn


def get_conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(_db_dsn())


# ── Haversine distance (miles) ──
def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 3958.8  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return R * c


# ── Time decay weight ──
def time_decay_months(sale_date, today=None) -> float:
    """Exponential decay: e^(-0.04 * months). Recent = 1.0, 24mo ago ≈ 0.38."""
    if sale_date is None:
        return 0.0
    if today is None:
        today = datetime.date.today()
    if isinstance(sale_date, datetime.datetime):
        sale_date = sale_date.date()
    delta_days = (today - sale_date).days
    months = max(delta_days / 30.44, 0)
    return math.exp(-0.04 * months)


# ── Distance weight ──
def distance_weight(miles: float) -> float:
    """Closer = higher weight. Exponential decay with 1mi half-life."""
    return math.exp(-math.log(2) * miles)  # half-life at 1 mile


# ── Percentile (linear interpolation) ──
def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] * (hi - k) + s[hi] * (k - lo)


# ── Trim non-arms-length / outlier comps by $/sqft ──
def _trim_priced_comps(comps: list[dict]) -> list[dict]:
    """Keep comps with a positive price/sqft, then drop $/sqft outliers that are
    likely non-arms-length (quitclaims, partial-interest deeds, bad data)."""
    priced = [c for c in comps if c.get("price_per_sqft") and c["price_per_sqft"] > 0]
    if len(priced) < 4:
        return priced  # too few to trim safely
    median = _percentile([c["price_per_sqft"] for c in priced], 50)
    lo, hi = median * 0.6, median * 1.75
    return [c for c in priced if lo <= c["price_per_sqft"] <= hi]


# ── Core: Find comps ──
@cached(ttl=300, key_prefix="comps:")
def find_comps(
    parcel_id: str,
    county_id: str = "hamilton",
    radius_miles: float = 5.0,
    sqft_tolerance: float = 0.20,
    months_window: int = 24,
    min_comps: int = 5,
) -> dict:
    """
    Find comparable sales for a subject property.
    
    Steps:
    1. Get subject property details (lat, lng, sqft, etc.)
    2. Query all properties with sale prices and coordinates
    3. Filter by sqft tolerance, sale age
    4. Calculate distance, apply weights
    5. If too few comps, expand radius iteratively
    """
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Get subject property
        cur.execute("""
            SELECT parcel_id, address, lat, lng, building_sqft, year_built,
                   last_sale_price, last_sale_date, assessed_value
            FROM properties
            WHERE parcel_id = %s AND county_id = %s
        """, (parcel_id, county_id))
        subject = cur.fetchone()
        
        if not subject or subject["lat"] is None:
            return {"error": f"Property {parcel_id} not found or has no coordinates"}
        
        subject_sqft = float(subject["building_sqft"]) if subject["building_sqft"] else None
        subject_lat = float(subject["lat"])
        subject_lng = float(subject["lng"])
        
        # Query potential comps — properties with sale data and coordinates.
        # Bounded three ways (sale-date window + lat/lng bounding box) so a
        # single request never materialises the whole county into Python;
        # the exact haversine/radius filter still runs below.
        lat_pad = (radius_miles + 0.5) / 69.0
        lng_pad = (radius_miles + 0.5) / (69.0 * max(math.cos(math.radians(subject_lat)), 0.01))
        today = datetime.date.today()
        cur.execute("""
            SELECT parcel_id, address, lat, lng, building_sqft, year_built,
                   last_sale_price, last_sale_date, assessed_value
            FROM properties
            WHERE last_sale_price > 1000
              AND lat IS NOT NULL
              AND building_sqft > 0
              AND parcel_id != %s
              AND county_id = %s
              AND last_sale_date >= %s
              AND lat BETWEEN %s AND %s
              AND lng BETWEEN %s AND %s
        """, (parcel_id, county_id,
              today - datetime.timedelta(days=months_window * 31 + 7),
              subject_lat - lat_pad, subject_lat + lat_pad,
              subject_lng - lng_pad, subject_lng + lng_pad))
        
        all_properties = cur.fetchall()
        cur.close()

        today = datetime.date.today()
        
        # Filter and score comps
        comps = []
        for prop in all_properties:
            prop_lat = float(prop["lat"])
            prop_lng = float(prop["lng"])
            prop_sqft = float(prop["building_sqft"]) if prop["building_sqft"] else None
            
            # Distance filter
            dist = haversine_miles(subject_lat, subject_lng, prop_lat, prop_lng)
            if dist > radius_miles:
                continue
            
            # Sqft tolerance filter
            if subject_sqft and prop_sqft:
                if abs(prop_sqft - subject_sqft) / subject_sqft > sqft_tolerance:
                    continue
            elif subject_sqft and not prop_sqft:
                continue  # Skip if we can't compare
            
            # Sale age filter
            sale_date = prop["last_sale_date"]
            if sale_date:
                months_ago = (today - sale_date).days / 30.44
                if months_ago > months_window:
                    continue
            else:
                continue
            
            # Calculate weights
            td_weight = time_decay_months(sale_date, today)
            dist_weight = distance_weight(dist)
            combined_weight = td_weight * dist_weight
            
            price_sqft = float(prop["last_sale_price"]) / prop_sqft if prop_sqft else None
            
            comps.append({
                "parcel_id": prop["parcel_id"],
                "address": prop["address"],
                "distance_miles": round(dist, 2),
                "building_sqft": prop_sqft,
                "year_built": prop["year_built"],
                "last_sale_price": float(prop["last_sale_price"]),
                "last_sale_date": str(sale_date),
                "price_per_sqft": round(price_sqft, 2) if price_sqft else None,
                "time_decay_weight": round(td_weight, 4),
                "distance_weight": round(dist_weight, 4),
                "combined_weight": round(combined_weight, 4),
            })
        
        return {
            "subject": {
                "parcel_id": parcel_id,
                "address": subject["address"],
                "building_sqft": subject_sqft,
                "lat": subject_lat,
                "lng": subject_lng,
            },
            "comps": comps,
            "comp_count": len(comps),
            "radius_miles": radius_miles,
            "min_comps_needed": min_comps,
        }
    finally:
        conn.close()


# ── Quarterly Price Index ──
def load_quarterly_index(county_id: str = "hamilton") -> dict:
    """Load the quarterly median $/sqft index for a county from the DB.

    Falls back to the on-disk JSON snapshot (Hamilton-only) if the table is
    missing. Any failure degrades to an empty index (= no time adjustment),
    never an exception, so an appraisal can still return a result.
    """
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT quarter, median_psf FROM quarterly_price_index "
            "WHERE county_id = %s ORDER BY quarter",
            (county_id,),
        )
        rows = cur.fetchall()
        return {r["quarter"]: float(r["median_psf"]) for r in rows}
    except Exception:
        # Fallback: on-disk snapshot. It has no county dimension and is
        # Hamilton-derived, so only use it for Hamilton; other counties get
        # no adjustment rather than the wrong county's price level.
        if county_id != "hamilton":
            return {}
        index_file = Path(__file__).parents[2] / "reports" / "backtest" / "quarterly_index_latest.json"
        try:
            if index_file.exists():
                with open(index_file) as f:
                    data = json.load(f)
                return {k: v["median_psf"] for k, v in data.items()}
        except Exception:
            pass
        return {}
    finally:
        conn.close()


def _quarter_of_date(dt_val) -> str:
    """Convert a date/datetime to 'YYYY-QN' string."""
    if isinstance(dt_val, str):
        dt_val = datetime.datetime.strptime(dt_val, "%Y-%m-%d").date()
    elif isinstance(dt_val, datetime.datetime):
        dt_val = dt_val.date()
    q = (dt_val.month - 1) // 3 + 1
    return f"{dt_val.year}-Q{q}"


def _adjust_price_by_index(price_sqft: float, sale_quarter: str, index: dict, current_quarter: str) -> float:
    """Adjust a comp's $/sqft by the ratio of current to sale-quarter median."""
    if not index or current_quarter not in index or sale_quarter not in index:
        return price_sqft
    current_psf = index[current_quarter]
    sale_psf = index[sale_quarter]
    if sale_psf <= 0:
        return price_sqft
    ratio = current_psf / sale_psf
    return price_sqft * ratio


# ── Calculate CMV (Current Market Value) ──
def calculate_cmv(
    parcel_id: str,
    county_id: str = "hamilton",
    radius_miles: float = 5.0,
    min_comps: int = 5,
) -> dict:
    """
    Calculate Current Market Value using weighted comparable sales.
    
    Expands radius if not enough comps found.
    Comps are time-adjusted using the quarterly median $/sqft index.
    """
    result = find_comps(parcel_id, county_id=county_id, radius_miles=radius_miles, min_comps=min_comps)
    
    if "error" in result:
        return result

    # @cached returns the SHARED object; the mutations below (radius fields,
    # per-comp adjusted fields) would corrupt the cache entry for every
    # subsequent caller. Take a private copy first.
    result = copy.deepcopy(result)
    
    comps = result["comps"]

    # If too few comps, try expanding radius. @cached returns the SHARED
    # object here too, so take a private copy before mutating (same reason
    # as the deepcopy above).
    if len(comps) < min_comps:
        for expanded_radius in [8.0, 10.0]:
            result = copy.deepcopy(
                find_comps(parcel_id, county_id=county_id, radius_miles=expanded_radius, min_comps=min_comps)
            )
            comps = result["comps"]
            if len(comps) >= min_comps:
                result["radius_miles"] = expanded_radius
                result["expanded_radius"] = True
                break
            result["radius_miles"] = expanded_radius

    # Drop non-arms-length / outlier sales, keep most-relevant first
    comps = _trim_priced_comps(comps)
    comps.sort(key=lambda c: c["combined_weight"], reverse=True)
    result["comps"] = comps
    result["comp_count"] = len(comps)

    # Load the quarterly price index and time-adjust each comp's $/sqft
    index = load_quarterly_index(county_id)
    today = datetime.date.today()
    current_quarter = _quarter_of_date(today)

    # Apply index adjustment to each comp and track ratios for reporting
    index_adjusted_comps = []
    index_ratios = []
    for c in comps:
        psf = c["price_per_sqft"]
        if psf is None or psf <= 0:
            continue
        sale_q = _quarter_of_date(c["last_sale_date"])
        adjusted_psf = _adjust_price_by_index(psf, sale_q, index, current_quarter)
        ratio = adjusted_psf / psf if psf > 0 else 1.0
        index_ratios.append(ratio)
        c["price_per_sqft_adjusted"] = round(adjusted_psf, 2)
        c["index_ratio"] = round(ratio, 4)
        index_adjusted_comps.append(c)

    # Weighted average of INDEX-ADJUSTED $/sqft
    # (existing time-decay weight stays on top — index handles price-level shift,
    #  time-decay handles recency preference among adjusted comps)
    total_weight = sum(c["combined_weight"] for c in index_adjusted_comps)
    if total_weight == 0:
        return {**result, "error": "No valid comps with price/sqft data"}

    weighted_price_sqft = sum(
        c["price_per_sqft_adjusted"] * c["combined_weight"] for c in index_adjusted_comps
    ) / total_weight

    subject_sqft = result["subject"]["building_sqft"]
    cmv = weighted_price_sqft * subject_sqft if subject_sqft else None

    # Confidence based on comp count and quality
    comp_count = len(comps)
    confidence = min(1.0, comp_count / 10)  # 10+ comps = max confidence

    # Adjust confidence down if radius was expanded
    if result.get("expanded_radius"):
        confidence *= 0.7

    # Adjust confidence based on average distance
    avg_dist = sum(c["distance_miles"] for c in comps) / max(comp_count, 1)
    if avg_dist > 3.0:
        confidence *= 0.8

    # Average index ratio (how much comps were adjusted upward on average)
    avg_index_ratio = sum(index_ratios) / len(index_ratios) if index_ratios else 1.0

    return {
        **result,
        "cmv": round(cmv, 2) if cmv else None,
        "weighted_price_per_sqft": round(weighted_price_sqft, 2),
        "confidence": round(confidence, 2),
        "methodology": "weighted_comparable_sales",
        "index_adjustment": {
            "current_quarter": current_quarter,
            "avg_ratio": round(avg_index_ratio, 4),
            "n_comps_adjusted": len(index_ratios),
        },
    }


# ── Calculate ARV (After Repair Value) + value-add ──
def calculate_arv(
    parcel_id: str,
    county_id: str = "hamilton",
    radius_miles: float = 5.0,
    min_comps: int = 5,
    rehab_per_sqft: float = DEFAULT_REHAB_PER_SQFT,
) -> dict:
    """
    After-Repair Value from the top of the comparable-sales distribution
    (renovated / best-condition homes), then derive investor value-add.

      ARV       = 75th-percentile comp $/sqft × subject sqft   (floored at CMV)
      value_add = ARV − CMV − estimated rehab cost

    Reuses the CMV comp pool (already county-, radius-, sqft-, recency-filtered
    and outlier-trimmed), so ARV and CMV stay consistent.
    """
    cmv_result = calculate_cmv(parcel_id, county_id=county_id, radius_miles=radius_miles, min_comps=min_comps)

    if "error" in cmv_result:
        return cmv_result

    cmv = cmv_result.get("cmv")
    subject_sqft = cmv_result["subject"]["building_sqft"]
    psf_values = [c["price_per_sqft"] for c in cmv_result.get("comps", []) if c.get("price_per_sqft")]

    arv = None
    arv_method = "insufficient_data"
    if len(psf_values) >= 3 and subject_sqft:
        arv = _percentile(psf_values, 75) * subject_sqft
        arv_method = "top_quartile_comps"
        # ARV represents good condition — never below as-is CMV
        if cmv and arv < cmv:
            arv, arv_method = cmv, "cmv_floor"
    elif cmv and subject_sqft:
        # Fallback when too few comps to take a meaningful quartile
        arv, arv_method = cmv * 1.10, "cmv_uplift"

    result = {
        **cmv_result,
        "arv": round(arv, 2) if arv else None,
        "arv_method": arv_method,
        "arv_per_sqft": round(arv / subject_sqft, 2) if arv and subject_sqft else None,
    }

    # ── Value-add: ARV − CMV − estimated rehab ──
    if arv and cmv and subject_sqft:
        rehab_estimate = round(rehab_per_sqft * subject_sqft, 2)
        value_add = round(arv - cmv - rehab_estimate, 2)
        cost_basis = cmv + rehab_estimate
        result.update({
            "rehab_per_sqft": rehab_per_sqft,
            "rehab_estimate": rehab_estimate,
            "value_add": value_add,
            "value_add_pct": round(value_add / cost_basis * 100, 1) if cost_basis else None,
        })
    else:
        result.update({
            "rehab_estimate": None,
            "value_add": None,
            "value_add_pct": None,
        })

    return result


# ── Full appraisal (CMV + ARV) ──
def full_appraisal(
    parcel_id: str,
    county_id: str = "hamilton",
    radius_miles: float = 5.0,
    min_comps: int = 5,
    rehab_per_sqft: float = DEFAULT_REHAB_PER_SQFT,
) -> dict:
    """Return CMV, ARV and value-add in a single call."""
    return calculate_arv(
        parcel_id,
        county_id=county_id,
        radius_miles=radius_miles,
        min_comps=min_comps,
        rehab_per_sqft=rehab_per_sqft,
    )


# ── FastAPI Endpoints ──
@router.get("/{parcel_id}", dependencies=[Depends(require_pro_license)])
def get_appraisal(
    parcel_id: str,
    radius: Optional[float] = None,
    min_comps: int = Query(default=5, ge=1, le=50),
    county_id: str = Query(default="hamilton"),
    rehab_per_sqft: Optional[float] = None,
) -> Dict[str, Any]:
    # ── Input sanitization ─────────────────────────────────────────
    county_id = validate_county_id(county_id)
    radius_val = sanitize_number(radius, min_val=0.0, max_val=25.0, default=5.0)
    rehab_val = sanitize_number(rehab_per_sqft, min_val=0.0, max_val=300.0, default=DEFAULT_REHAB_PER_SQFT)
    parcel_id = sanitize_url_path(parcel_id)
    result = full_appraisal(
        parcel_id,
        county_id=county_id,
        radius_miles=radius_val,
        min_comps=min_comps,
        rehab_per_sqft=rehab_val,
    )
    
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    
    return result


@router.get("/{parcel_id}/comps")
def get_comps(
    parcel_id: str,
    radius: Optional[float] = None,
    sqft_tolerance: float = Query(default=0.20, ge=0.05, le=0.50),
    months: int = Query(default=24, ge=1, le=60),
) -> Dict[str, Any]:
    """Get raw comparable sales for a property.

    NB: return annotation must match find_comps' dict shape — a List[Dict]
    annotation here made FastAPI's response_model validation raise on every
    successful call.
    """
    radius_val = sanitize_number(radius, min_val=0.25, max_val=25.0, default=5.0)
    parcel_id = sanitize_url_path(parcel_id)
    result = find_comps(
        parcel_id,
        radius_miles=radius_val,
        sqft_tolerance=sqft_tolerance,
        months_window=months,
    )
    
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    
    return result
