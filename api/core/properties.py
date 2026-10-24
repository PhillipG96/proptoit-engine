"""
Properties endpoints — thin HTTP wrapper around core.queries.
"""
import csv
import io
import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from . import queries
from cache import cache
from sanitize import (
    sanitize_query_param, sanitize_list_param, sanitize_number,
    sanitize_url_path, validate_county_id,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
def list_properties(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="distressScore"),
    sort_dir: str = Query(default="desc"),
    distress_types: Optional[str] = Query(default=None),
    min_assessed_value: Optional[float] = Query(default=None),
    max_assessed_value: Optional[float] = Query(default=None),
    address: Optional[str] = Query(default=None),
    parcel_id: Optional[str] = Query(default=None),
    property_category: Optional[str] = Query(default=None),
    county_id: Optional[str] = Query(default="hamilton"),
) -> Dict[str, Any]:
    # ── Input sanitization ─────────────────────────────────────────
    # Validate sort_by / sort_dir
    valid_sort_fields = {
        "distressScore", "assessedValue", "lastSalePrice",
        "yearBuilt", "buildingSqft", "address", "county_id",
    }
    if sort_by not in valid_sort_fields:
        sort_by = "distressScore"
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"
    # Sanitize numeric parameters
    limit = int(sanitize_number(limit, min_val=1, max_val=1000, default=100))
    offset = int(sanitize_number(offset, min_val=0, default=0))
    # Sanitize distress types list
    distress_list = sanitize_list_param(distress_types, max_items=10) if distress_types else None
    # Sanitize text fields
    address_clean = sanitize_query_param(address, max_length=255) if address else None
    # Sanitize parcel_id
    parcel_id_clean = sanitize_url_path(parcel_id) if parcel_id else None
    # Validate county_id
    county_id = validate_county_id(county_id)
    # Sanitize property_category
    property_category_clean = sanitize_query_param(property_category, max_length=50) if property_category else None
    # Sanitize numeric values
    min_val = sanitize_number(min_assessed_value, min_val=0, max_val=100_000_000, default=None)
    max_val = sanitize_number(max_assessed_value, min_val=0, max_val=100_000_000, default=None)
    try:
        result = queries.search_properties(
            limit=limit, offset=offset, sort_by=sort_by, sort_dir=sort_dir,
            distress_types=distress_list, min_assessed_value=min_val,
            max_assessed_value=max_val, address=address_clean, parcel_id=parcel_id_clean,
            property_category=property_category_clean, county_id=county_id,
            cache=cache,
        )
    except Exception:
        logger.error("List properties error", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    return {
        "success": True, "data": result["data"],
        "pagination": {"total": result["total"], "limit": result["limit"], "offset": result["offset"]},
    }


@router.get("/distressed")
def list_distressed(
    limit: int = Query(default=500, ge=1, le=2000),
    county_id: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    # ── Input sanitization ─────────────────────────────────────────
    limit = int(sanitize_number(limit, min_val=1, max_val=2000, default=500))
    county_id = validate_county_id(county_id)
    try:
        result = queries.list_distressed(limit=limit, county_id=county_id)
    except Exception:
        logger.error("List distressed properties error", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    return {"success": True, "data": result}


@router.get("/compare")
def compare_properties_endpoint(
    parcel_ids: str = Query(default=""),
    county_id: Optional[str] = Query(default="hamilton"),
) -> Dict[str, Any]:
    # ── Input sanitization ─────────────────────────────────────────
    ids = [sanitize_url_path(p.strip()) for p in parcel_ids.split(",") if p.strip()]
    ids = [p for p in ids if p]
    if not ids:
        raise HTTPException(status_code=400, detail="No parcel_ids provided")
    if len(ids) > 4:
        raise HTTPException(status_code=400, detail="Maximum 4 properties allowed for comparison")
    county_id = validate_county_id(county_id)
    try:
        result = queries.compare_properties(ids, county_id=county_id)
    except Exception:
        logger.error("Compare properties error", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
    if len(result) != len(ids):
        missing = set(ids) - {r["parcelId"] for r in result}
        raise HTTPException(status_code=404, detail=f"Properties not found: {missing}")
    return {"success": True, "data": result}


@router.get("/top-deals")
def top_deals(
    limit: int = Query(default=50, ge=1, le=200),
    min_score: int = Query(default=50, ge=0, le=100),
    county_id: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """Get the highest-scoring properties for deal hunting."""
    # ── Input sanitization ─────────────────────────────────────────
    limit = int(sanitize_number(limit, min_val=1, max_val=200, default=50))
    min_score = int(sanitize_number(min_score, min_val=0, max_val=100, default=50))
    county_id = validate_county_id(county_id)
    try:
        result = queries.get_top_deals(limit=limit, min_score=min_score, county_id=county_id)
    except Exception:
        logger.error("Top deals error", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    return {
        "success": True,
        "data": result["data"],
        "pagination": {"total": result["total"], "limit": limit, "offset": 0},
    }


@router.get("/export/csv")
def export_csv(
    limit: int = Query(default=1000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="distressScore"),
    sort_dir: str = Query(default="desc"),
    distress_types: Optional[str] = Query(default=None),
    min_assessed_value: Optional[float] = Query(default=None),
    max_assessed_value: Optional[float] = Query(default=None),
    address: Optional[str] = Query(default=None),
    parcel_id: Optional[str] = Query(default=None),
    property_category: Optional[str] = Query(default=None),
    county_id: Optional[str] = Query(default="hamilton"),
) -> StreamingResponse:
    # ── Input sanitization ─────────────────────────────────────────
    valid_sort_fields = {
        "distressScore", "assessedValue", "lastSalePrice",
        "yearBuilt", "buildingSqft", "address", "county_id",
    }
    if sort_by not in valid_sort_fields:
        sort_by = "distressScore"
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"
    limit = int(sanitize_number(limit, min_val=1, max_val=5000, default=1000))
    offset = int(sanitize_number(offset, min_val=0, default=0))
    distress_list = sanitize_list_param(distress_types, max_items=10) if distress_types else None
    address_clean = sanitize_query_param(address, max_length=255) if address else None
    parcel_id_clean = sanitize_url_path(parcel_id) if parcel_id else None
    county_id = validate_county_id(county_id)
    property_category_clean = sanitize_query_param(property_category, max_length=50) if property_category else None
    min_val = sanitize_number(min_assessed_value, min_val=0, max_val=100_000_000, default=None)
    max_val = sanitize_number(max_assessed_value, min_val=0, max_val=100_000_000, default=None)
    try:
        result = queries.search_properties(
            limit=limit, offset=offset, sort_by=sort_by, sort_dir=sort_dir,
            distress_types=distress_list, min_assessed_value=min_val,
            max_assessed_value=max_val, address=address_clean, parcel_id=parcel_id_clean,
            property_category=property_category_clean, county_id=county_id,
        )
    except Exception:
        logger.error("CSV export error", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_FIELDS)
    for p in result["data"]:
        writer.writerow([
            p.get("parcelId"), p.get("address"), p.get("city"), p.get("postalCode"),
            p.get("propertyCategory"),
            p.get("ownerOfRecord"), p.get("ownerMailingAddress"),
            p.get("assessedValue"), p.get("lastSalePrice"), p.get("lastSaleDate"),
            p.get("yearBuilt"), p.get("buildingSqft"), p.get("lotSizeSqft"),
            p.get("bedrooms"), p.get("bathrooms"),
            p.get("taxAmount"), p.get("distressScore"), p.get("ownerOccupant"),
            p.get("preForeclosureFlag"), p.get("taxDelinquentFlag"), p.get("codeViolationFlag"),
            p.get("probateFlag"), p.get("fsboFlag"), p.get("vacancyHeuristic"),
            p.get("geocoords", {}).get("lat") if p.get("geocoords") else None,
            p.get("geocoords", {}).get("lng") if p.get("geocoords") else None,
            p.get("equityEstimate"),
        ])
    buf.seek(0)

    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=proptoit_properties.csv"},
    )


@router.get("/categories")
def property_categories(
    county_id: Optional[str] = Query(default="hamilton"),
) -> Dict[str, Any]:
    """Get property category distribution for dashboard stats."""
    try:
        result = queries.count_properties_by_category(county_id=county_id)
    except Exception:
        logger.error("Property categories error", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
    return {"success": True, "data": result}


@router.get("/counties")
def list_counties() -> Dict[str, Any]:
    """List all available counties with property/distress counts."""
    try:
        result = queries.count_counties()
    except Exception:
        logger.error("Counties list error", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
    return {"success": True, "data": result}


# ── Category-specific endpoints (view-backed) ──

def _category_list(
    category: str,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="distressScore"),
    sort_dir: str = Query(default="desc"),
    distress_types: Optional[str] = Query(default=None),
    min_assessed_value: Optional[float] = Query(default=None),
    max_assessed_value: Optional[float] = Query(default=None),
    address: Optional[str] = Query(default=None),
    parcel_id: Optional[str] = Query(default=None),
    county_id: Optional[str] = Query(default="hamilton"),
) -> Dict[str, Any]:
    # ── Input sanitization ─────────────────────────────────────────
    # Validate category
    valid_categories = {"residential", "commercial", "exempt", "agricultural"}
    if category not in valid_categories:
        raise HTTPException(status_code=400, detail=f"Invalid category: {category}")
    # Validate sort fields
    valid_sort_fields = {
        "distressScore", "assessedValue", "lastSalePrice",
        "yearBuilt", "buildingSqft", "address", "county_id",
    }
    if sort_by not in valid_sort_fields:
        sort_by = "distressScore"
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"
    # Sanitize numeric parameters
    limit = int(sanitize_number(limit, min_val=1, max_val=1000, default=100))
    offset = int(sanitize_number(offset, min_val=0, default=0))
    # Sanitize distress types
    distress_list = sanitize_list_param(distress_types, max_items=10) if distress_types else None
    # Sanitize text fields
    address_clean = sanitize_query_param(address, max_length=255) if address else None
    parcel_id_clean = sanitize_url_path(parcel_id) if parcel_id else None
    # Validate county_id
    county_id = validate_county_id(county_id)
    # Sanitize numeric values
    min_val = sanitize_number(min_assessed_value, min_val=0, max_val=100_000_000, default=None)
    max_val = sanitize_number(max_assessed_value, min_val=0, max_val=100_000_000, default=None)
    try:
        result = queries.search_properties_by_category(
            category, limit=limit, offset=offset, sort_by=sort_by, sort_dir=sort_dir,
            distress_types=distress_list, min_assessed_value=min_val,
            max_assessed_value=max_val, address=address_clean, parcel_id=parcel_id_clean,
            county_id=county_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.error("Category %s error", category, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
    return {"success": True, **result}


@router.get("/residential")
def list_residential(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="distressScore"),
    sort_dir: str = Query(default="desc"),
    distress_types: Optional[str] = Query(default=None),
    min_assessed_value: Optional[float] = Query(default=None),
    max_assessed_value: Optional[float] = Query(default=None),
    address: Optional[str] = Query(default=None),
    parcel_id: Optional[str] = Query(default=None),
    county_id: Optional[str] = Query(default="hamilton"),
) -> Dict[str, Any]:
    return _category_list("residential", limit, offset, sort_by, sort_dir,
                          distress_types, min_assessed_value, max_assessed_value, address, parcel_id,
                          county_id)


@router.get("/commercial")
def list_commercial(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="distressScore"),
    sort_dir: str = Query(default="desc"),
    distress_types: Optional[str] = Query(default=None),
    min_assessed_value: Optional[float] = Query(default=None),
    max_assessed_value: Optional[float] = Query(default=None),
    address: Optional[str] = Query(default=None),
    parcel_id: Optional[str] = Query(default=None),
    county_id: Optional[str] = Query(default="hamilton"),
) -> Dict[str, Any]:
    return _category_list("commercial", limit, offset, sort_by, sort_dir,
                          distress_types, min_assessed_value, max_assessed_value, address, parcel_id,
                          county_id)


@router.get("/exempt")
def list_exempt(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="distressScore"),
    sort_dir: str = Query(default="desc"),
    distress_types: Optional[str] = Query(default=None),
    min_assessed_value: Optional[float] = Query(default=None),
    max_assessed_value: Optional[float] = Query(default=None),
    address: Optional[str] = Query(default=None),
    parcel_id: Optional[str] = Query(default=None),
    county_id: Optional[str] = Query(default="hamilton"),
) -> Dict[str, Any]:
    return _category_list("exempt", limit, offset, sort_by, sort_dir,
                          distress_types, min_assessed_value, max_assessed_value, address, parcel_id,
                          county_id)


@router.get("/agricultural")
def list_agricultural(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="distressScore"),
    sort_dir: str = Query(default="desc"),
    distress_types: Optional[str] = Query(default=None),
    min_assessed_value: Optional[float] = Query(default=None),
    max_assessed_value: Optional[float] = Query(default=None),
    address: Optional[str] = Query(default=None),
    parcel_id: Optional[str] = Query(default=None),
    county_id: Optional[str] = Query(default="hamilton"),
) -> Dict[str, Any]:
    return _category_list("agricultural", limit, offset, sort_by, sort_dir,
                          distress_types, min_assessed_value, max_assessed_value, address, parcel_id,
                          county_id)


@router.get("/{parcel_id}/analysis")
def get_property_analysis(
    parcel_id: str,
    county_id: Optional[str] = Query(default="hamilton"),
) -> Dict[str, Any]:
    # ── Input sanitization ─────────────────────────────────────────
    parcel_id = sanitize_url_path(parcel_id)
    if not parcel_id:
        raise HTTPException(status_code=400, detail="Invalid parcel_id")
    county_id = validate_county_id(county_id)
    try:
        result = queries.get_property_analysis(parcel_id, county_id=county_id)
    except Exception:
        logger.error("Get property analysis error: %s", parcel_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    if not result:
        raise HTTPException(status_code=404, detail="Property analysis not found")

    return {"success": True, "data": result}


@router.get("/{parcel_id}")
def get_property(
    parcel_id: str,
    county_id: Optional[str] = Query(default="hamilton"),
) -> Dict[str, Any]:
    # ── Input sanitization ─────────────────────────────────────────
    parcel_id = sanitize_url_path(parcel_id)
    if not parcel_id:
        raise HTTPException(status_code=400, detail="Invalid parcel_id")
    county_id = validate_county_id(county_id)
    try:
        result = queries.get_property_by_parcel_id(parcel_id, county_id=county_id, cache=cache)
    except Exception:
        logger.error("Get property error: %s", parcel_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    if not result:
        raise HTTPException(status_code=404, detail="Property not found")

    return {"success": True, "data": result}


# ── Street-level imagery via Mapillary (free, CC-BY-SA, attribution required) ──
import os
import requests

MAPILLARY_TOKEN = os.environ.get("MAPILLARY_ACCESS_TOKEN", "")
MAPILLARY_GRAPH = "https://graph.mapillary.com/images"

import math

MAX_DIST_M = 30        # beyond this the camera is on other frontage
MAX_BEARING_ERR = 35   # camera must point within 35° of the parcel


def bearing_to_parcel(parcel_lat, parcel_lng, cam_lon, cam_lat):
    """Great-circle bearing FROM the camera TO the parcel, degrees [0, 360).

    This is the heading a camera must have to aim at the parcel. Computed
    camera->parcel (point1 = camera), NOT parcel->camera.
    """
    dlon = math.radians(parcel_lng - cam_lon)
    lat1r, lat2r = math.radians(cam_lat), math.radians(parcel_lat)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _img_coords(img):
    g = img.get("computed_geometry") or {}
    coords = g.get("coordinates") or [None, None]
    return coords[0], coords[1]


def _dist_m(img, lat, lng):
    lon2, lat2 = _img_coords(img)
    if lat2 is None or lon2 is None:
        return 1e12
    dy = (lat2 - lat) * 111_320
    dx = (lon2 - lng) * 111_320 * max(0.1, math.cos(math.radians(lat)))
    return (dx * dx + dy * dy) ** 0.5


def _ang_diff(a, b):
    """Angular difference between two headings, [0, 180]."""
    return abs((a - b + 180) % 360 - 180)


def select_best_image(images, lat, lng):
    """Pick the closest Mapillary image that plausibly shows the parcel.

    Rules:
    - within MAX_DIST_M of the parcel centroid
    - flats: camera heading (compass_angle) must aim within MAX_BEARING_ERR
      of the camera->parcel bearing; no heading info means unverifiable aim -> skip
    - panoramas: capture the full 360 sphere, so aim cannot be verified from
      compass_angle and they are accepted on distance alone (the parcel may be
      out of the default thumbnail frame; frontend has a Street View fallback).
    """
    candidates = []
    for img in images:
        d = _dist_m(img, lat, lng)
        if d > MAX_DIST_M:
            continue
        is_pano = img.get("is_pano", False)
        cam = img.get("compass_angle")
        if not is_pano:
            if cam is None:
                continue  # no heading info → can't verify aim → skip (wrong-home risk)
            lon2, lat2 = _img_coords(img)
            if lat2 is None or lon2 is None:
                continue
            need = bearing_to_parcel(lat, lng, lon2, lat2)
            if _ang_diff(float(cam), need) > MAX_BEARING_ERR:
                continue
        candidates.append((d, img))

    if not candidates:
        return None
    return min(candidates, key=lambda t: t[0])[1]


@router.get("/{parcel_id}/street-image")
def get_street_image(
    parcel_id: str,
    county_id: Optional[str] = Query(default="hamilton"),
) -> Dict[str, Any]:
    """Find the closest Mapillary street-level image to a property's coordinates."""
    parcel_id = sanitize_url_path(parcel_id)
    if not parcel_id:
        raise HTTPException(status_code=400, detail="Invalid parcel_id")
    county_id = validate_county_id(county_id or "hamilton")

    if not MAPILLARY_TOKEN:
        return {"success": True, "data": None, "reason": "mapillary_not_configured"}

    try:
        prop = queries.get_property_by_parcel_id(parcel_id, county_id=county_id, cache=cache)
    except Exception:
        logger.error("Street image lookup error: %s", parcel_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    if not prop:
        raise HTTPException(status_code=404, detail="Property not found")

    lat, lng = None, None
    geo = prop.get("geocoords") or {}
    if isinstance(geo, dict):
        lat, lng = geo.get("lat"), geo.get("lng")
    if lat is None:
        lat = prop.get("lat")
    if lng is None:
        lng = prop.get("lng")
    if lat is None or lng is None:
        return {"success": True, "data": None, "reason": "no_coordinates"}

    try:
        # Search a small bbox around the parcel (±0.0005° ≈ ±55m) — tight, to avoid
        # showing a neighbor's house. Better no photo than the wrong home.
        bbox = f"{lng - 0.0005},{lat - 0.0005},{lng + 0.0005},{lat + 0.0005}"
        # NOTE: graph.mapillary.com hangs on HTTP/2 from this host — force HTTP/1.1
        res = requests.get(
            MAPILLARY_GRAPH,
            params={
                "access_token": MAPILLARY_TOKEN,
                "bbox": bbox,
                "fields": "id,computed_geometry,thumb_1024_url,thumb_original_url,is_pano,captured_at,compass_angle",
                "limit": 20,
            },
            timeout=10,
        )
        res.raise_for_status()
        images = res.json().get("data", [])
    except Exception as e:
        logger.warning("Mapillary API error for %s: %s", parcel_id, e)
        return {"success": True, "data": None, "reason": "mapillary_error"}

    if not images:
        return {"success": True, "data": None, "reason": "no_coverage"}

    best = select_best_image(images, lat, lng)
    if best is None:
        return {"success": True, "data": None, "reason": "no_coverage"}

    d = _dist_m(best, lat, lng)

    return {
        "success": True,
        "data": {
            "image_url": best.get("thumb_original_url") or best.get("thumb_1024_url"),
            "captured_at": best.get("captured_at"),
            "distance_m": round(d),
            "is_pano": best.get("is_pano", False),
            "attribution": "© Mapillary (CC-BY-SA)",
        },
    }


CSV_FIELDS = [
    "parcel_id", "address", "city", "postal_code", "property_category",
    "owner_name", "owner_mailing_address",
    "assessed_value", "last_sale_price", "last_sale_date", "year_built", "sqft",
    "lot_size", "bedrooms", "bathrooms",
    "tax_amount", "distress_score", "owner_occupant",
    "pre_foreclosure", "tax_delinquent", "code_violation", "probate", "fsbo", "vacant",
    "lat", "lng", "equity_estimate",
]
